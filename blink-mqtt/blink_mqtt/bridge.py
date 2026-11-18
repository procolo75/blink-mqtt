import asyncio
import logging

from slugify import slugify

from . import config
from .auth import AUTH_ERRORS, AuthManager
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

    async def run(self):
        self._running = True
        self._mqtt.set_available(True)
        self._mqtt.publish_discovery(self._blink)
        await asyncio.gather(
            self._poll_loop(),
            self._command_loop(),
        )

    def stop(self):
        self._running = False

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
        while self._running:
            try:
                await self._blink.refresh(force=True)
                for cam in self._blink.cameras.values():
                    new_url = cam.thumbnail
                    old_url = self._thumbnail_cache.get(cam.name)
                    image_changed = new_url != old_url
                    if image_changed:
                        self._thumbnail_cache[cam.name] = new_url
                    # Always publish sensor data; publish image only when it changed
                    # or on the very first run (so HA gets an initial value).
                    self._mqtt.publish_camera(cam, publish_image=(image_changed or first_run))
                    await self._check_motion_clip(cam, first_run)
                for sync in self._blink.sync.values():
                    self._mqtt.publish_sync(sync)
                first_run = False
                self._auth.note_auth_success()
            except AUTH_ERRORS as e:
                if self._note_auth_error(e):
                    return
            except Exception as e:
                # repr() so exceptions without a message stay identifiable.
                _LOGGER.error("Poll error: %s", repr(e))
            await asyncio.sleep(config.POLL_INTERVAL)

    async def _check_motion_clip(self, cam, first_run: bool):
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
            clip = cam.video_from_cache if url == cam.clip else None
            if not _looks_like_mp4(clip):
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
        if not _looks_like_mp4(data):
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

        _LOGGER.debug("Unhandled topic: %s", topic)

    async def _arm_sync(self, nid: str, arm: bool):
        for sync in self._blink.sync.values():
            if str(sync.network_id) == nid:
                _LOGGER.info("Setting sync %s armed=%s", sync.name, arm)
                await sync.async_arm(arm)
                self._mqtt.publish_sync(sync)
                return
        _LOGGER.warning("Sync network_id=%s not found", nid)

    async def _trigger_snapshot(self, serial: str):
        for cam in self._blink.cameras.values():
            if (cam.serial or slugify(cam.name)) == serial:
                _LOGGER.info("Snapshot: %s", cam.name)
                await cam.snap_picture()
                # Force-update thumbnail cache so next publish sends fresh image
                self._thumbnail_cache[cam.name] = None
                self._mqtt.publish_camera(cam, publish_image=True)
                return
        _LOGGER.warning("Camera serial=%s not found", serial)


def _looks_like_mp4(data) -> bool:
    """MP4/ISO-BMFF files carry the 'ftyp' box tag at bytes 4..8."""
    return isinstance(data, (bytes, bytearray)) and data[4:8] == b"ftyp"
