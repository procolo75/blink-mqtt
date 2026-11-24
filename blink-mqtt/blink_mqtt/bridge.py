import asyncio
import logging
from datetime import datetime

from slugify import slugify

from . import camstatus, config, settings, snapshot, syncinfo, thumbs
from blinkpy import api as blink_api

from .auth import AUTH_ERRORS, AuthManager
from .liveclip import (is_own_activity, looks_like_mp4, mark_own_activity,
                       note_blink_motion, record_clip)
from .mqtt_client import MQTTClient

_LOGGER = logging.getLogger(__name__)

class Bridge:
    def __init__(self, auth_manager: AuthManager, mqtt: MQTTClient,
                 bridge_event: asyncio.Event):
        self._auth = auth_manager
        self._blink = auth_manager.blink
        self._mqtt = mqtt
        self._bridge_event = bridge_event
        self._running = False
        # Track thumbnail URLs so we only push new images to MQTT when they change.
        self._thumbnail_cache: dict[str, str | None] = {}
        # Clip timestamps already seen in cam.recent_clips, per camera serial.
        self._seen_clips: dict[str, set[str]] = {}
        # Live clip tasks in flight (a reference keeps them from being collected).
        self._tasks: set[asyncio.Task] = set()
        # Homescreen `updated_at` per device id and arm state per network at
        # the previous poll — motion signal for Live View modules
        # (see _check_device_updates).
        self._device_updated: dict[str, str] = {}
        self._prev_armed: dict[str, bool | None] = {}
        # Set to cut the full poll's wait short (e.g. right after arming).
        self._poll_now = asyncio.Event()
        self._fast_poll: bool | None = None
        # Cameras whose Snapshot picture is due: published once downloaded.
        self._snapshot_pending: set[str] = set()

    async def run(self):
        self._running = True
        self._mqtt.set_available(True)
        self._mqtt.publish_discovery(self._blink)
        self._log_sync_modules()
        await asyncio.gather(
            self._poll_loop(),
            self._command_loop(),
            self._motion_loop(),
        )

    def stop(self):
        self._running = False

    def _log_sync_modules(self):
        """Once per start: the raw homescreen entry of every sync module.

        The `type` / `subtype` strings Blink uses per model are not documented;
        this log is where we learn them.
        """
        for mod in (self._blink.homescreen or {}).get("sync_modules") or []:
            _LOGGER.info("Sync module %s: %s", mod.get("name"),
                         {k: v for k, v in mod.items() if k != "name"})

    async def _motion_loop(self):
        """Fast watch of camera `updated_at` while a Live View module is armed.

        The full poll (blink.refresh, every poll_interval) is too slow for a
        motion trigger. This reads only the homescreen, every
        motion_poll_interval seconds, and only while some module in Live View
        mode is armed. Own GET, not api.request_homescreen(): that one is
        throttled to 5 s and returns None when called too soon, which would
        break blink.refresh() running in _poll_loop.
        """
        url = f"{self._blink.urls.base_url}/api/v3/accounts/{self._blink.account_id}/homescreen"
        while self._running:
            # Settings are read every round: a change in the web UI applies at once.
            interval = settings.get()["motion_poll_interval"]
            if settings.blink_watch() and self._liveview_module_armed():
                try:
                    home = await blink_api.http_get(self._blink, url)
                    if isinstance(home, dict) and "cameras" in home:
                        self._check_device_updates(home)
                        self._update_online(home)
                except AUTH_ERRORS as e:
                    if self._note_auth_error(e):
                        return
                except Exception as e:
                    _LOGGER.warning("Motion watch error: %s", repr(e))
                    interval = config.POLL_INTERVAL   # back off
            await asyncio.sleep(interval)

    def _update_online(self, home: dict):
        """Online/offline from the homescreen the motion watch just read.

        Same endpoint and shape blinkpy stores in blink.homescreen, so it is
        kept there: an armed Live View module going offline shows in about
        motion_poll_interval seconds instead of at the next full poll, with no
        extra request to Blink. MQTT is published only for what changed.
        """
        def states():
            return ({c.name: camstatus.offline_reason(self._blink, c)
                     for c in self._blink.cameras.values()},
                    {n: camstatus.sync_offline(self._blink, s)
                     for n, s in self._blink.sync.items()})

        cams_before, syncs_before = states()
        self._blink.homescreen = home
        camstatus.observe(self._blink)
        cams_after, syncs_after = states()
        for cam in self._blink.cameras.values():
            if cams_before[cam.name] != cams_after[cam.name]:
                self._mqtt.publish_camera(cam, publish_image=False)
        for name, sync in self._blink.sync.items():
            if syncs_before[name] != syncs_after[name]:
                self._mqtt.publish_sync(sync)

    def _liveview_module_armed(self) -> bool:
        return any(
            s.arm is True and syncinfo.recording_mode(s, list(s.cameras.values())) == syncinfo.LIVEVIEW
            for s in self._blink.sync.values()
        )

    def _check_device_updates(self, home: dict | None = None):
        """Record a Live View clip on motion of a module that stores nothing.

        For a sync module without USB storage or subscription (e.g. Sync
        Module Core) Blink lists the motion nowhere: no clip in media/changed,
        and /events/network answers "An app update is required". The camera's
        homescreen `updated_at` is the signal: verified on 2026-11-19 it moves
        at every motion notification and stays still otherwise. It also moves
        when the module is armed/disarmed and when the add-on itself wakes the
        camera, so a change counts as motion only if:
          - the camera's module is in Live View mode (syncinfo),
          - the module was armed at the previous poll AND is armed now,
          - the change was not caused by our own Live View / snapshot.
        Latency: up to one poll interval + Live View startup.
        """
        home = home or self._blink.homescreen or {}
        armed_now = {str(s.network_id): s.arm for s in self._blink.sync.values()}
        # Prefer the arm state carried by the same homescreen snapshot.
        for net in home.get("networks") or []:
            if isinstance(net.get("armed"), bool):
                armed_now[str(net.get("id"))] = net["armed"]
        cams = {str(c.camera_id): c for c in self._blink.cameras.values()}

        for dev in home.get("cameras") or []:
            dev_id, new = str(dev.get("id")), dev.get("updated_at")
            if not new:
                continue
            old = self._device_updated.get(dev_id)
            # Only forward moves: the fast watch and the full poll read the
            # homescreen at different moments, an older snapshot must not
            # count as a change (and then the newer one again as another).
            if old is not None and _ts(new) <= _ts(old):
                continue
            self._device_updated[dev_id] = new
            if old is None:
                continue

            nid = str(dev.get("network_id"))
            cam = cams.get(dev_id)
            decision = self._motion_decision(cam, nid, new, armed_now)
            _LOGGER.info("Device update %s: updated_at %s -> %s — %s",
                         dev.get("name"), old, new, decision)
            if cam is not None and decision.startswith(("motion", "ignored (caused by")):
                # Blink's own motion stamp, for the "who came first" table.
                note_blink_motion(cam.serial or slugify(cam.name), _ts(new))
            if decision == "motion, recording" and cam is not None:
                self._spawn(self._record_live_clip(cam.serial or slugify(cam.name),
                                                   "blink", _ts(new)))

        self._prev_armed = armed_now

    def _motion_decision(self, cam, nid: str, updated_at: str, armed_now: dict) -> str:
        if cam is None:
            return "ignored (camera unknown)"
        if syncinfo.recording_mode(cam.sync, list(cam.sync.cameras.values())) != syncinfo.LIVEVIEW:
            return "ignored (module stores its own clips)"
        if not (armed_now.get(nid) is True and self._prev_armed.get(nid) is True):
            return "ignored (arm/disarm or not armed)"
        when = _ts(updated_at)
        if not when:
            return "ignored (bad timestamp)"
        if is_own_activity(cam.serial or slugify(cam.name), when):
            return "ignored (caused by the add-on)"
        if not settings.blink_watch():
            return f"motion, not recorded by Blink watch (trigger mode: {settings.get()['trigger_mode']})"
        return "motion, recording"

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------

    def _note_auth_error(self, e: Exception) -> bool:
        """Report an auth error; shut the bridge down if the session is dead."""
        if not self._auth.note_auth_failure(e):
            return False
        self._running = False
        # Wake _supervise(), which cancels this task and (blink being None now)
        # goes back to waiting for a login from the web UI.
        self._bridge_event.set()
        return True

    # ------------------------------------------------------------------

    async def _poll_loop(self):
        first_run = True
        # refresh(force=True) would also pass force_cache to every camera, and
        # blinkpy then downloads each camera's thumbnail and its latest clip on
        # every poll even when unchanged (~38 MB/h measured at rest). A plain
        # refresh() fetches them only when new; refresh_rate 0 drops blinkpy's
        # own throttle, the pacing is ours.
        self._blink.refresh_rate = 0
        while self._running:
            try:
                if not config.AUTO_THUMBNAILS:
                    thumbs.gate(self._blink.cameras.values())
                # Which video each camera held before this refresh: a clip is taken
                # from blinkpy's cache only if this very refresh downloaded it.
                videos_before = {c.name: c.video_from_cache for c in self._blink.cameras.values()}
                await self._blink.refresh()
                camstatus.observe(self._blink)
                for cam in self._blink.cameras.values():
                    new_url = cam.thumbnail
                    old_url = self._thumbnail_cache.get(cam.name)
                    image_changed = new_url != old_url
                    if image_changed:
                        self._thumbnail_cache[cam.name] = new_url
                    snapshot = cam.name in self._snapshot_pending and cam.image_from_cache
                    if snapshot:
                        self._snapshot_pending.discard(cam.name)
                    # Always publish sensor data; the image when it changed, on the
                    # first run, or when a Snapshot picture arrived. With thumbnails
                    # gated (default) only Snapshot pictures are ever downloaded.
                    self._mqtt.publish_camera(
                        cam, publish_image=bool(image_changed or first_run or snapshot))
                    fresh = cam.video_from_cache is not videos_before.get(cam.name)
                    await self._check_motion_clip(cam, first_run, fresh)
                for sync in self._blink.sync.values():
                    self._mqtt.publish_sync(sync)
                self._check_device_updates()
                first_run = False
                self._auth.note_auth_success()
            except AUTH_ERRORS as e:
                if self._note_auth_error(e):
                    return
            except Exception as e:
                # repr() so exceptions without a message stay identifiable.
                _LOGGER.error("Poll error: %s", repr(e))
            await self._wait_next_poll()

    async def _wait_next_poll(self):
        """poll_interval while an armed module relies on the poll for its clips,
        idle_poll_interval otherwise (status only: battery, temperature, arm)."""
        fast = syncinfo.needs_fast_poll(self._blink.sync.values())
        if fast != self._fast_poll:
            self._fast_poll = fast
            _LOGGER.info("Full poll every %ss (%s)",
                         config.POLL_INTERVAL if fast else config.IDLE_POLL_INTERVAL,
                         "an armed module finds its clips through it" if fast
                         else "no armed USB/Cloud module: status only")
        self._poll_now.clear()
        try:
            await asyncio.wait_for(self._poll_now.wait(),
                                   config.POLL_INTERVAL if fast else config.IDLE_POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass

    async def _check_motion_clip(self, cam, first_run: bool, cache_fresh: bool = True):
        """Publish the clip once for every new entry in cam.recent_clips.

        recent_clips is blinkpy's own list of motion clips from the last hour
        (the one save_recent_clips() uses), filled on every refresh() — also
        with Sync Module USB storage and no subscription, where blinkpy lists a
        clip once its recording has finished and has the sync module upload it.
        It is the reliable signal: last_record / motion_detected are not, e.g.
        for Mini cameras, which blinkpy models as their own sync-less "sync".
        The first poll only seeds: clips from before startup are not re-sent.
        """
        serial  = cam.serial or slugify(cam.name)
        clips   = {c["time"]: c["clip"] for c in (cam.recent_clips or [])
                   if c.get("time") and c.get("clip")}
        seen    = self._seen_clips.get(serial)
        # recent_clips expires after an hour, so `seen` stays bounded.
        self._seen_clips[serial] = set(clips)
        if first_run or seen is None:
            return

        for when in sorted(set(clips) - seen):
            url  = clips[when]
            # blinkpy's cache holds the latest clip only if this refresh filled it;
            # a failed download would leave the previous clip there.
            clip = cam.video_from_cache if url == cam.clip and cache_fresh else None
            if not looks_like_mp4(clip):
                clip = await self._download_clip(cam, url)
            if not clip:
                _LOGGER.warning("Motion clip %s (%s): video not available, nothing published",
                                cam.name, when)
                continue
            self._mqtt.publish_motion_event(cam, clip)
            _LOGGER.info("Motion clip %s (%s): %d KB", cam.name, when, len(clip) // 1024)

    async def _download_clip(self, cam, url: str) -> bytes | None:
        try:
            resp = await cam.get_video_clip(url)
            if resp is None or resp.status != 200:
                _LOGGER.warning("Clip download for %s: HTTP %s", cam.name,
                                getattr(resp, "status", None))
                return None
            data = await resp.read()
        except AUTH_ERRORS:
            raise
        except Exception as e:
            _LOGGER.warning("Clip download failed for %s: %s", cam.name, repr(e))
            return None
        # Never forward an error body (JSON/HTML) as a video.
        if not looks_like_mp4(data):
            _LOGGER.warning("Clip download for %s: not an MP4 (%d bytes)", cam.name, len(data))
            return None
        return data

    async def _command_loop(self):
        while self._running:
            try:
                topic, payload = await asyncio.wait_for(
                    self._mqtt.command_queue.get(), timeout=1.0
                )
                await self._handle_command(topic, payload)
            except asyncio.TimeoutError:
                pass
            except AUTH_ERRORS as e:
                if self._note_auth_error(e):
                    return
            except Exception as e:
                _LOGGER.error("Command error: %s", repr(e))

    async def _handle_command(self, topic: str, payload: str):
        parts = topic.split("/")

        # blink/sync/{nid}/armed/set
        if (len(parts) == 5 and parts[0] == "blink" and parts[1] == "sync"
                and parts[3] == "armed" and parts[4] == "set"):
            await self._arm_sync(parts[2], payload.strip().upper() == "ON")
            return

        # blink/cameras/{serial}/snapshot/trigger
        if (len(parts) == 5 and parts[0] == "blink" and parts[1] == "cameras"
                and parts[3] == "snapshot" and parts[4] == "trigger"):
            await self._trigger_snapshot(parts[2])
            return

        # blink/cameras/{serial}/record/trigger
        if (len(parts) == 5 and parts[0] == "blink" and parts[1] == "cameras"
                and parts[3] == "record" and parts[4] == "trigger"):
            # In its own task: a recording lasts several seconds and must not
            # hold up arm/snapshot commands queued behind it.
            self._spawn(self._record_live_clip(parts[2], "manual"))
            return

        # blink/cameras/{serial}/motion/trigger — automatic start from outside
        # (an Alexa routine running an HA script, an HA automation...).
        if (len(parts) == 5 and parts[0] == "blink" and parts[1] == "cameras"
                and parts[3] == "motion" and parts[4] == "trigger"):
            self._external_motion(parts[2])
            return

        _LOGGER.debug("Unhandled topic: %s", topic)

    async def _arm_sync(self, nid: str, arm: bool):
        for sync in self._blink.sync.values():
            if str(sync.network_id) == nid:
                _LOGGER.info("Setting sync %s armed=%s", sync.name, arm)
                await sync.async_arm(arm)
                self._mqtt.publish_sync(sync)
                # Poll now: the new arm state may switch the poll to fast.
                self._poll_now.set()
                return
        _LOGGER.warning("Sync network_id=%s not found", nid)

    async def _trigger_snapshot(self, serial: str):
        cam = self._camera(serial)
        if cam is None:
            _LOGGER.warning("Camera serial=%s not found", serial)
            return
        _LOGGER.info("Snapshot: %s", cam.name)
        # In its own task: waiting for the new picture takes up to a minute and
        # must not hold up commands queued behind it.
        self._spawn(self._take_snapshot(cam, serial))

    async def _take_snapshot(self, cam, serial: str):
        mark_own_activity(serial)
        try:
            await snapshot.take(cam)
        except snapshot.SnapshotError as e:
            _LOGGER.warning("Snapshot %s: %s — no picture published", cam.name, e)
            self._mqtt.publish_camera(cam, publish_image=False)   # online state
            return
        except AUTH_ERRORS as e:
            self._note_auth_error(e)
            return
        except Exception as e:
            _LOGGER.error("Snapshot %s failed: %s", cam.name, repr(e))
            return
        finally:
            mark_own_activity(serial)
        # Blink lists the new picture: the next poll downloads and publishes it.
        thumbs.request(cam)
        self._snapshot_pending.add(cam.name)
        self._poll_now.set()

    def _camera(self, serial: str):
        return next((c for c in self._blink.cameras.values()
                     if (c.serial or slugify(c.name)) == serial), None)

    def _external_motion(self, serial: str):
        cam = self._camera(serial)
        if cam is None:
            reason = "camera not found"
        elif not settings.external_trigger():
            reason = f"trigger mode is {settings.get()['trigger_mode']}"
        elif syncinfo.recording_mode(cam.sync, list(cam.sync.cameras.values())) != syncinfo.LIVEVIEW:
            reason = "module stores its own clips (USB/cloud)"
        else:
            _LOGGER.info("External motion trigger %s — recording", cam.name)
            self._spawn(self._record_live_clip(serial, "external"))
            return
        _LOGGER.info("External motion trigger %s — ignored (%s)", serial, reason)

    async def _record_live_clip(self, serial: str, source: str,
                                blink_motion: float | None = None):
        cam = self._camera(serial)
        if cam is None:
            _LOGGER.warning("Camera serial=%s not found", serial)
            return
        try:
            clip = await record_clip(cam, serial, settings.get()["clip_seconds"],
                                     source, blink_motion)
        except AUTH_ERRORS as e:
            self._note_auth_error(e)
            return
        except Exception as e:
            _LOGGER.error("Live clip %s failed: %s", cam.name, repr(e))
            return
        if clip:
            self._mqtt.publish_motion_event(cam, clip)


def _ts(iso: str) -> float:
    """Epoch seconds of a Blink ISO timestamp; 0.0 if unparsable."""
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0
