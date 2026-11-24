"""Is a camera reachable? blinkpy does not say.

blinkpy 0.25.5 reads a sync module's `status` only once, at start-up, and when
Blink reports `sync_module_error` its refresh() returns early: the cameras keep
their last values (battery "ok", temperature...), so an offline camera looked
fine. Three signals instead, the first that applies wins:

1. the sync module's `status` in the homescreen (refreshed on every poll);
2. the camera's own `status` in the homescreen;
3. a command (Snapshot, Live View) Blink failed for this camera in the last
   FAILURE_TTL seconds — cleared by the next command that succeeds.

Only "offline" is treated as offline. Every status value Blink sends is
logged at start and whenever it changes (`observe`), so new values can be recognised later.
"""

import logging
import time

from slugify import slugify

_LOGGER = logging.getLogger(__name__)

FAILURE_TTL = 900
_OFFLINE = {"offline"}
_DEVICE_KEYS = ("cameras", "owls", "doorbells")

_failures: dict[str, tuple[float, str]] = {}
_seen_status: dict[str, str | None] = {}


def _serial(cam) -> str:
    return cam.serial or slugify(cam.name)


def note_failure(cam, msg: str) -> None:
    _failures[_serial(cam)] = (time.time(), msg)
    _LOGGER.warning("Camera %s: command failed (%s) — shown offline", cam.name, msg)


def note_success(cam) -> None:
    _failures.pop(_serial(cam), None)


def device_entry(home: dict | None, cam) -> dict | None:
    """The camera's entry in a homescreen dict (cameras / owls / doorbells)."""
    for key in _DEVICE_KEYS:
        for dev in (home or {}).get(key) or []:
            if str(dev.get("id")) == str(cam.camera_id):
                return dev
    return None


def _sync_entry(home: dict | None, sync) -> dict | None:
    sid = getattr(sync, "sync_id", None)
    for mod in (home or {}).get("sync_modules") or []:
        if sid is not None and mod.get("id") == sid:
            return mod
    return None


def _is_offline(value) -> bool:
    return isinstance(value, str) and value.lower() in _OFFLINE


def sync_offline(blink, sync) -> bool:
    entry = _sync_entry(blink.homescreen, sync)
    if entry is not None:
        return _is_offline(entry.get("status"))
    # Not in the homescreen (Mini/doorbell pseudo-sync): blinkpy's own flag.
    return getattr(sync, "available", True) is False


def offline_reason(blink, cam) -> str | None:
    """Why the camera is unreachable, or None if nothing says so."""
    if sync_offline(blink, cam.sync):
        return "sync module offline"
    dev = device_entry(blink.homescreen, cam)
    if dev is not None and _is_offline(dev.get("status")):
        return "camera offline"
    failure = _failures.get(_serial(cam))
    if failure and time.time() - failure[0] < FAILURE_TTL:
        return failure[1]
    return None


def observe(blink) -> None:
    """Log every change of the status Blink reports for modules and cameras."""
    home = blink.homescreen or {}
    for mod in home.get("sync_modules") or []:
        _note(f"Sync module {mod.get('name')}", f"sync:{mod.get('id')}", mod.get("status"))
    for key in _DEVICE_KEYS:
        for dev in home.get(key) or []:
            _note(f"Camera {dev.get('name')}", f"dev:{dev.get('id')}", dev.get("status"))


def _note(label: str, key: str, status) -> None:
    old = _seen_status.get(key, "<unset>")
    if old != status:
        _seen_status[key] = status
        _LOGGER.info("%s: Blink status %r -> %r", label, old, status)
