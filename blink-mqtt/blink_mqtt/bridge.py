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
