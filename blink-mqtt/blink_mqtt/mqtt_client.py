import asyncio
import json
import logging

import paho.mqtt.client as mqtt
from slugify import slugify

_LOGGER = logging.getLogger(__name__)

_DISCOVERY = "homeassistant"
_STATE = "blink"
_AVAIL = f"{_STATE}/availability"   # global bridge availability topic


class MQTTClient:
    def __init__(self, host, port, user, password, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.command_queue: asyncio.Queue = asyncio.Queue()

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
            client.publish(_AVAIL, "online", retain=True)
            client.subscribe(f"{_STATE}/sync/+/armed/set")
            client.subscribe(f"{_STATE}/cameras/+/snapshot/trigger")
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

    def _pub(self, topic: str, payload, retain: bool = False):
        if isinstance(payload, (bytes, bytearray)):
            self._client.publish(topic, payload, retain=retain)
        else:
            self._client.publish(topic, str(payload), retain=retain)

    def publish_discovery(self, blink):
        for cam in blink.cameras.values():
            self._publish_camera_discovery(cam)
        for sync_name, sync in blink.sync.items():
            self._publish_sync_discovery(sync_name, sync)

    def _publish_camera_discovery(self, cam):
        slug = slugify(cam.name)
        serial = cam.serial or slug
        base = f"{_STATE}/cameras/{serial}"
        dev = {
            "identifiers": [f"blink_{serial}"],
            "name": f"Blink {cam.name}",
            "manufacturer": "Blink",
        }
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

    def publish_camera(self, cam, publish_image: bool = True):
        serial = cam.serial or slugify(cam.name)
        base = f"{_STATE}/cameras/{serial}"

        self._pub(f"{base}/motion/state", "ON" if cam.motion_detected is True else "OFF", retain=True)
        # Use sync module arm state — motion_enabled is an individual camera setting
        # that does not change when the sync module is armed/disarmed.
        sync_arm = getattr(cam.sync, "arm", None)
        self._pub(f"{base}/armed/state", "ON" if sync_arm is True else "OFF", retain=True)

        if cam.battery is not None:
            self._pub(f"{base}/battery/state", cam.battery, retain=True)
        if cam.temperature is not None:
            self._pub(f"{base}/temperature/state", round((cam.temperature - 32) * 5 / 9, 1), retain=True)
        if cam.wifi_strength is not None:
            self._pub(f"{base}/wifi/state", cam.wifi_strength, retain=True)
        if publish_image and cam.image_from_cache:
            self._pub(f"{base}/snapshot", cam.image_from_cache)

    def publish_sync(self, sync):
        nid = str(sync.network_id)
        # arm can be True/False/None — None means unknown, publish as OFF (safer)
        self._pub(f"{_STATE}/sync/{nid}/armed/state", "ON" if sync.arm is True else "OFF", retain=True)

    def stop(self):
        self._pub(_AVAIL, "offline", retain=True)
        self._client.loop_stop()
        self._client.disconnect()
