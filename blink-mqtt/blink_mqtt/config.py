import json

_OPTIONS_FILE = "/data/options.json"


def _load():
    try:
        with open(_OPTIONS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_cfg = _load()

MQTT_HOST = _cfg.get("mqtt_host", "core-mosquitto")
MQTT_PORT = int(_cfg.get("mqtt_port", 1883))
MQTT_USER = _cfg.get("mqtt_user", "")
MQTT_PASSWORD = _cfg.get("mqtt_password", "")
POLL_INTERVAL = int(_cfg.get("poll_interval", 30))
