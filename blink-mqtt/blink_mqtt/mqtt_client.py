import asyncio
import json
import logging

import paho.mqtt.client as mqtt
from slugify import slugify

from . import camstatus

_LOGGER = logging.getLogger(__name__)

_DISCOVERY = "homeassistant"
_STATE = "blink"
_AVAIL = f"{_STATE}/availability"   # global bridge availability topic


class MQTTClient:
    def __init__(self, host, port, user, password, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.command_queue: asyncio.Queue = asyncio.Queue()
        # Entities stay unavailable until the bridge has a working Blink session.
        self._available = False

        self._client = mqtt.Client()
        if user:
            self._client.username_pw_set(user, password)
        self._client.will_set(_AVAIL, "offline", retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.connect_async(host, port)
        self._client.loop_start()

    # ------------------------------------------------------------------
    # paho callbacks (called from paho thread)
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            _LOGGER.info("MQTT connected")
            # Republish the *current* availability: a reconnect must not bring
            # entities back online while the Blink session is dead.
            client.publish(
                _AVAIL, "online" if self._available else "offline", retain=True
            )
            client.subscribe(f"{_STATE}/sync/+/armed/set")
            client.subscribe(f"{_STATE}/cameras/+/snapshot/trigger")
            client.subscribe(f"{_STATE}/cameras/+/record/trigger")
            client.subscribe(f"{_STATE}/cameras/+/motion/trigger")
        else:
            _LOGGER.error("MQTT connect failed: rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc):
        _LOGGER.warning("MQTT disconnected: rc=%s", rc)

    def _on_message(self, client, userdata, msg):
        self._loop.call_soon_threadsafe(
            self.command_queue.put_nowait,
            (msg.topic, msg.payload.decode(errors="replace")),
        )

    # ------------------------------------------------------------------
    # public helpers
    # ------------------------------------------------------------------

    def _pub(self, topic: str, payload, retain: bool = False, qos: int = 0):
        if isinstance(payload, (bytes, bytearray)):
            self._client.publish(topic, payload, qos=qos, retain=retain)
        else:
            self._client.publish(topic, str(payload), qos=qos, retain=retain)

    def set_available(self, available: bool):
        """Flip the global availability topic used by every discovery config."""
        self._available = available
        self._pub(_AVAIL, "online" if available else "offline", retain=True)

    def publish_discovery(self, blink):
        for cam in blink.cameras.values():
            self._publish_camera_discovery(cam)
        for sync_name, sync in blink.sync.items():
            self._publish_sync_discovery(sync_name, sync)
            self._publish_sync_online_discovery(sync_name, sync)

    def _publish_camera_discovery(self, cam):
        slug = slugify(cam.name)
        serial = cam.serial or slug
        base = f"{_STATE}/cameras/{serial}"
        dev = {
            "identifiers": [f"blink_{serial}"],
            "name": f"Blink {cam.name}",
            "manufacturer": "Blink",
        }
        # Show the camera as "connected via" its sync module in HA.
        nid = getattr(cam.sync, "network_id", None)
        if nid is not None:
            dev["via_device"] = f"blink_sync_{nid}"
        avail = {"topic": _AVAIL, "payload_available": "online", "payload_not_available": "offline"}

        self._pub(
            f"{_DISCOVERY}/binary_sensor/blink_{slug}/motion/config",
            json.dumps({
                "name": "Motion",
                "object_id": f"blink_{slug}_motion",
                "unique_id": f"blink_{serial}_motion",
                "state_topic": f"{base}/motion/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "motion",
                "availability": [avail],
                "device": dev,
            }),
            retain=True,
        )

        # Reachable or not: the sync module or camera reported offline, or a
        # command failed (see camstatus.py). Battery etc. go stale while OFF.
        self._pub(
            f"{_DISCOVERY}/binary_sensor/blink_{slug}/online/config",
            json.dumps({
                "name": "Online",
                "object_id": f"blink_{slug}_online",
                "unique_id": f"blink_{serial}_online",
                "state_topic": f"{base}/online/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "connectivity",
                "entity_category": "diagnostic",
                "availability": [avail],
                "device": dev,
            }),
            retain=True,
        )

        self._pub(
            f"{_DISCOVERY}/binary_sensor/blink_{slug}/armed/config",
            json.dumps({
                "name": "Armed",
                "object_id": f"blink_{slug}_armed",
                "unique_id": f"blink_{serial}_armed",
                "state_topic": f"{base}/armed/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:shield-camera",
                "availability": [avail],
                "device": dev,
            }),
            retain=True,
        )

        self._pub(
            f"{_DISCOVERY}/sensor/blink_{slug}/battery/config",
            json.dumps({
                "name": "Battery",
                "object_id": f"blink_{slug}_battery",
                "unique_id": f"blink_{serial}_battery",
                "state_topic": f"{base}/battery/state",
                "icon": "mdi:battery",
                "availability": [avail],
                "device": dev,
            }),
            retain=True,
        )

        self._pub(
            f"{_DISCOVERY}/sensor/blink_{slug}/temperature/config",
            json.dumps({
                "name": "Temperature",
                "object_id": f"blink_{slug}_temperature",
                "unique_id": f"blink_{serial}_temperature",
                "state_topic": f"{base}/temperature/state",
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "availability": [avail],
                "device": dev,
            }),
            retain=True,
        )

        self._pub(
            f"{_DISCOVERY}/sensor/blink_{slug}/wifi/config",
            json.dumps({
                "name": "WiFi",
                "object_id": f"blink_{slug}_wifi",
                "unique_id": f"blink_{serial}_wifi",
                "state_topic": f"{base}/wifi/state",
                "device_class": "signal_strength",
                "unit_of_measurement": "dBm",
                "availability": [avail],
                "device": dev,
            }),
            retain=True,
        )

        # Record a Live View clip; it is published on {base}/event/clip.
        self._pub(
            f"{_DISCOVERY}/button/blink_{slug}/record/config",
            json.dumps({
                "name": "Record clip",
                "object_id": f"blink_{slug}_record",
                "unique_id": f"blink_{serial}_record",
                "command_topic": f"{base}/record/trigger",
                "payload_press": "PRESS",
                "icon": "mdi:record-rec",
                "availability": [avail],
                "device": dev,
            }),
            retain=True,
        )

        # HA MQTT camera — raw JPEG bytes, no /state suffix
        self._pub(
            f"{_DISCOVERY}/camera/blink_{slug}/config",
            json.dumps({
                "name": "Snapshot",
                "object_id": f"blink_{slug}_snapshot",
                "unique_id": f"blink_{serial}_snapshot",
                "topic": f"{base}/snapshot",
                "availability": [avail],
                "device": dev,
            }),
            retain=True,
        )

    def _publish_sync_discovery(self, sync_name, sync):
        slug = slugify(sync_name)
        nid = str(sync.network_id)
        avail = {"topic": _AVAIL, "payload_available": "online", "payload_not_available": "offline"}
        self._pub(
            f"{_DISCOVERY}/switch/blink_{slug}/arm/config",
            json.dumps({
                "name": "Armed",
                "object_id": f"blink_{slug}_armed",
                "unique_id": f"blink_sync_{nid}_arm",
                "state_topic": f"{_STATE}/sync/{nid}/armed/state",
                "command_topic": f"{_STATE}/sync/{nid}/armed/set",
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:shield-home",
                "availability": [avail],
                "device": {
                    "identifiers": [f"blink_sync_{nid}"],
                    "name": f"Blink {sync_name}",
                    "manufacturer": "Blink",
                },
            }),
            retain=True,
        )

    def _publish_sync_online_discovery(self, sync_name, sync):
        slug = slugify(sync_name)
        nid = str(sync.network_id)
        self._pub(
            # blink_sync_{nid}: a Mini's pseudo-sync has the camera's own name.
            f"{_DISCOVERY}/binary_sensor/blink_sync_{nid}/online/config",
            json.dumps({
                "name": "Online",
                "object_id": f"blink_{slug}_sync_online",
                "unique_id": f"blink_sync_{nid}_online",
                "state_topic": f"{_STATE}/sync/{nid}/online/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "connectivity",
                "entity_category": "diagnostic",
                "availability": [{"topic": _AVAIL, "payload_available": "online",
                                  "payload_not_available": "offline"}],
                "device": {
                    "identifiers": [f"blink_sync_{nid}"],
                    "name": f"Blink {sync_name}",
                    "manufacturer": "Blink",
                },
            }),
            retain=True,
        )

    def publish_camera(self, cam, publish_image: bool = True):
        serial = cam.serial or slugify(cam.name)
        base = f"{_STATE}/cameras/{serial}"

        # Display name for consumers that caption media (e.g. JARVIS Telegram).
        self._pub(f"{base}/name", cam.name, retain=True)
        self._pub(f"{base}/motion/state", "ON" if cam.motion_detected is True else "OFF", retain=True)
        # Use sync module arm state — motion_enabled is an individual camera setting
        # that does not change when the sync module is armed/disarmed.
        sync_arm = getattr(cam.sync, "arm", None)
        self._pub(f"{base}/armed/state", "ON" if sync_arm is True else "OFF", retain=True)
        offline = camstatus.offline_reason(cam.sync.blink, cam)
        self._pub(f"{base}/online/state", "OFF" if offline else "ON", retain=True)

        if cam.battery is not None:
            self._pub(f"{base}/battery/state", cam.battery, retain=True)
        if cam.temperature is not None:
            self._pub(f"{base}/temperature/state", round((cam.temperature - 32) * 5 / 9, 1), retain=True)
        if cam.wifi_strength is not None:
            self._pub(f"{base}/wifi/state", cam.wifi_strength, retain=True)
        if publish_image and cam.image_from_cache:
            self._pub(f"{base}/snapshot", cam.image_from_cache)

    def publish_motion_event(self, cam, clip: bytes):
        """One motion clip as MP4 bytes (~1-3 MB for 10 s), never retained, QoS 1.

        blink/cameras/{serial}/event/clip

        No photo is published with it (removed in 1.4.0): the only image blinkpy
        holds is the camera's home-screen thumbnail (cam.image_from_cache), which
        a motion clip does not refresh — it showed an older scene, not the motion.
        """
        base = f"{_STATE}/cameras/{cam.serial or slugify(cam.name)}/event"
        self._pub(f"{base}/clip", clip, qos=1)

    def publish_sync(self, sync):
        nid = str(sync.network_id)
        # arm can be True/False/None — None means unknown, publish as OFF (safer)
        self._pub(f"{_STATE}/sync/{nid}/armed/state", "ON" if sync.arm is True else "OFF", retain=True)
        offline = camstatus.sync_offline(sync.blink, sync)
        self._pub(f"{_STATE}/sync/{nid}/online/state", "OFF" if offline else "ON", retain=True)

    def stop(self):
        self.set_available(False)
        self._client.loop_stop()
        self._client.disconnect()
