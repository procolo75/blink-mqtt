"""Live View settings, edited in the web UI (Settings tab) and kept in /data.

They apply ONLY to sync modules in Live View mode (no USB storage, no
subscription — see syncinfo.py). Modules with USB storage record their own
clips from the moment of motion and are never touched by these settings.

trigger_mode — what starts a Live View clip automatically on motion:
  blink    — the add-on watches the camera's `updated_at` on Blink every
             `motion_poll_interval` seconds while the module is armed.
  external — the add-on does not poll; something else publishes
             blink/cameras/{serial}/motion/trigger (an Alexa routine running a
             Home Assistant script, an HA automation...).
  both     — both of the above; the first one wins, the other is skipped.
  off      — no automatic clip.
The manual "Record clip" button (blink/cameras/{serial}/record/trigger)
works in every mode.
"""
import json
import logging
import os

_LOGGER = logging.getLogger(__name__)

_FILE = "/data/blink_settings.json"

MODES = ("blink", "external", "both", "off")

DEFAULTS = {
    "trigger_mode": "blink",
    "motion_poll_interval": 5,   # seconds, 5..60
    "clip_seconds": 10,          # seconds, 5..60
}

_current: dict = {}


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default


def _clean(raw: dict) -> dict:
    mode = raw.get("trigger_mode")
    return {
        "trigger_mode": mode if mode in MODES else DEFAULTS["trigger_mode"],
        "motion_poll_interval": _clamp(raw.get("motion_poll_interval"), 5, 60,
                                       DEFAULTS["motion_poll_interval"]),
        "clip_seconds": _clamp(raw.get("clip_seconds"), 5, 60, DEFAULTS["clip_seconds"]),
    }


def load() -> dict:
    global _current
    try:
        with open(_FILE) as f:
            _current = _clean(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _current = dict(DEFAULTS)
    return _current


def get() -> dict:
    return _current or load()


def save(raw: dict) -> dict:
    """Validate, apply at once and persist. Returns the stored settings."""
    global _current
    _current = _clean(raw)
    try:
        tmp = _FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_current, f)
        os.replace(tmp, _FILE)
    except OSError as e:
        _LOGGER.error("Saving settings failed: %s", e)
    _LOGGER.info("Settings: %s", _current)
    return _current


def blink_watch() -> bool:
    return get()["trigger_mode"] in ("blink", "both")


def external_trigger() -> bool:
    return get()["trigger_mode"] in ("external", "both")
