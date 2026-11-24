"""Take a picture and wait until Blink really has the new one.

Before 1.11.0 a Snapshot waited a fixed 8 s and then downloaded whatever
thumbnail the camera had: when the camera was slow (waking up, weak signal) the
URL had not changed yet and the *previous* picture was shown as the new one;
the next Snapshot then showed the picture of the one before. And blinkpy's
snap_picture() throws Blink's answer away, so a camera that never took the
picture (offline) looked like a success.

Now: the snap command is sent directly, its outcome read from Blink
(`/network/{nid}/command/{id}`), then the camera's thumbnail stamp is polled
until it changes. Only then is the picture downloaded. If the stamp never
changes, the Snapshot fails — no old picture is passed off as new.
"""

import asyncio
import logging
import re
import time

from blinkpy import api as blink_api

from . import camstatus

_LOGGER = logging.getLogger(__name__)

COMMAND_TIMEOUT = 30    # s for Blink to report the snap command done
PICTURE_TIMEOUT = 30    # s more for the new thumbnail to appear
POLL_EVERY = 2
_OK_CODE = 908          # Blink's "command succeeded / in progress" code


class SnapshotError(Exception):
    """The camera did not deliver a new picture; the message says why."""


def _stamp(thumb) -> str | None:
    """Comparable stamp of a thumbnail value (epoch number or URL with ts=)."""
    if thumb is None:
        return None
    m = re.search(r"ts=(\d+)", str(thumb))
    return m.group(1) if m else str(thumb)


async def _current_thumb(cam) -> str | None:
    """The camera's thumbnail stamp as Blink has it now (1–2 GETs)."""
    blink = cam.sync.blink
    url = f"{blink.urls.base_url}/api/v3/accounts/{blink.account_id}/homescreen"
    home = await blink_api.http_get(blink, url)
    dev = camstatus.device_entry(home if isinstance(home, dict) else None, cam)
    if dev is not None and dev.get("thumbnail"):
        return _stamp(dev["thumbnail"])
    resp = await blink_api.request_camera_info(blink, cam.network_id, cam.camera_id)
    try:
        return _stamp(resp["camera"][0].get("thumbnail"))
    except (TypeError, KeyError, IndexError):
        return None


async def _wait_command(blink, nid, command_id) -> None:
    deadline = time.monotonic() + COMMAND_TIMEOUT
    while True:
        status = await blink_api.request_command_status(blink, nid, command_id)
        if isinstance(status, dict):
            code = status.get("status_code")
            if code is not None and code != _OK_CODE:
                raise SnapshotError(status.get("status_msg") or f"Blink code {code}")
            if status.get("complete"):
                return
        if time.monotonic() > deadline:
            raise SnapshotError(f"camera did not answer within {COMMAND_TIMEOUT} s")
        await asyncio.sleep(POLL_EVERY)


async def take(cam) -> None:
    """Have `cam` take a picture; return once Blink lists the new thumbnail.

    Raises SnapshotError (and marks the camera offline) when it does not.
    Auth errors propagate.
    """
    blink = cam.sync.blink
    reason = camstatus.offline_reason(blink, cam)
    if reason in ("sync module offline", "camera offline"):
        raise SnapshotError(reason)
    before = await _current_thumb(cam)
    # Not cam.snap_picture(): it discards Blink's answer and its throttle
    # silently drops a second request within 5 s.
    resp = await blink_api.request_camera_action(
        blink, cam.network_id, cam.camera_id, "snap", camera_type=cam.camera_type)
    command_id = resp.get("id") if isinstance(resp, dict) else None
    if not command_id:
        msg = (resp or {}).get("message") if isinstance(resp, dict) else None
        _fail(cam, msg or f"Blink refused the snapshot: {resp}")
    try:
        await _wait_command(blink, resp.get("network_id") or cam.network_id, command_id)
    except SnapshotError as e:
        _fail(cam, str(e))

    deadline = time.monotonic() + PICTURE_TIMEOUT
    while True:
        now = await _current_thumb(cam)
        if now is not None and now != before:
            _LOGGER.info("Snapshot %s: new picture (ts %s -> %s)", cam.name, before, now)
            camstatus.note_success(cam)
            return
        if time.monotonic() > deadline:
            # The command succeeded, so the camera is reachable: not marked
            # offline (a Mini at rest often keeps its old thumbnail).
            raise SnapshotError(f"no new picture from the camera within {PICTURE_TIMEOUT} s")
        await asyncio.sleep(POLL_EVERY)


def _fail(cam, msg: str):
    camstatus.note_failure(cam, msg)
    raise SnapshotError(msg)
