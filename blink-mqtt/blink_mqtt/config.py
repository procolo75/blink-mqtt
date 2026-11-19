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
# Full poll when no armed module needs it to find clips: status only.
# Home-screen thumbnails on every change (old behaviour). Off: only Snapshot pictures.
AUTO_THUMBNAILS = bool(_cfg.get("auto_thumbnails", False))
IDLE_POLL_INTERVAL = max(POLL_INTERVAL, int(_cfg.get("idle_poll_interval", 300)))
