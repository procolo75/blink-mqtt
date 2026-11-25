"""Snapshot waits for Blink's new picture; offline modules/cameras are seen — snapshot.py, camstatus.py.

    pip install pytest && pytest -q tests

Why it bites (2026-11-24): with the sync module offline the camera still showed
battery "ok", and Snapshot waited a fixed 8 s then downloaded whatever thumbnail
Blink had: the old picture, and the new one only on the next Snapshot.
"""
import asyncio
import sys
import types
from pathlib import Path

# Stub the add-on's third-party imports, then load blink_mqtt as a package.
# setdefault: another test module may have stubbed them first; share its objects.
_blinkpy = sys.modules.setdefault("blinkpy", types.ModuleType("blinkpy"))
_api = sys.modules.setdefault("blinkpy.api", types.ModuleType("blinkpy.api"))
_blinkpy.api = _api
_slug = types.ModuleType("slugify")
_slug.slugify = lambda s: s.lower().replace(" ", "-")
sys.modules.setdefault("slugify", _slug)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "blink-mqtt"))

from blink_mqtt import camstatus, snapshot  # noqa: E402

snapshot.POLL_EVERY = 0
snapshot.PICTURE_TIMEOUT = 0.05
snapshot.COMMAND_TIMEOUT = 0.05


class Fake:
    """Blink as seen by snapshot.py: homescreen thumbnails + command status."""

    def __init__(self, thumbs, command=None, sync_status="online"):
        self.thumbs = list(thumbs)        # stamp returned by each homescreen read
        self.command = command or {"complete": True, "status_code": 908}
        self.sync_status = sync_status
        self.snaps = 0

    def install(self, cam):
        async def http_get(blink, url):
            stamp = self.thumbs.pop(0) if len(self.thumbs) > 1 else self.thumbs[0]
            return {"cameras": [{"id": 7, "thumbnail": stamp, "status": "done"}]}

        async def action(blink, nid, cid, act, camera_type=""):
            self.snaps += 1
            return {"id": 99, "network_id": nid}

        async def status(blink, nid, command_id):
            return self.command

        _api.http_get = http_get
        _api.request_camera_action = action
        _api.request_command_status = status
        cam.sync.blink.homescreen = {
            "sync_modules": [{"id": 1, "status": self.sync_status}],
            "cameras": [{"id": 7, "status": "done"}]}


class Sync:
    sync_id = 1
    network_id = "815900"


class Blink:
    account_id = 1
    homescreen = {}
    urls = types.SimpleNamespace(base_url="https://x")


def make_cam():
    sync = Sync()
    sync.blink = Blink()
    return types.SimpleNamespace(name="Outdoor 4", serial="S1", camera_id="7",
                                 network_id="815900", camera_type="", sync=sync)


def run(coro):
    return asyncio.run(coro)


def test_waits_for_the_new_thumbnail():
    cam = make_cam()
    Fake(["1795116376", "1795116376", "1795116376", "1795548033"]).install(cam)
    run(snapshot.take(cam))          # returns only once the stamp changed


def test_unchanged_thumbnail_fails_instead_of_showing_old_picture():
    cam = make_cam()
    Fake(["1795116376"]).install(cam)
    try:
        run(snapshot.take(cam))
    except snapshot.SnapshotError as e:
        assert "no new picture" in str(e)
    else:
        raise AssertionError("old picture accepted as new")
    # The command worked: the camera is reachable, not marked offline.
    assert camstatus.offline_reason(cam.sync.blink, cam) is None


def test_failed_command_marks_camera_offline():
    cam = make_cam()
    Fake(["1"], command={"complete": True, "status_code": 902,
                         "status_msg": "Camera is offline"}).install(cam)
    try:
        run(snapshot.take(cam))
    except snapshot.SnapshotError as e:
        assert "offline" in str(e)
    assert camstatus.offline_reason(cam.sync.blink, cam) == "Camera is offline"
    camstatus.note_success(cam)
    assert camstatus.offline_reason(cam.sync.blink, cam) is None


def test_offline_sync_module_blocks_snapshot_and_shows_offline():
    cam = make_cam()
    fake = Fake(["1"], sync_status="offline")
    fake.install(cam)
    assert camstatus.offline_reason(cam.sync.blink, cam) == "sync module offline"
    try:
        run(snapshot.take(cam))
    except snapshot.SnapshotError as e:
        assert str(e) == "sync module offline"
    assert fake.snaps == 0           # no command sent to a module Blink says is offline


def test_stamp_from_url_or_number():
    url = ("https://rest-e006.immedia-semi.com/api/v3/media/accounts/1/networks/2/"
           "sedona/3/thumbnail/thumbnail.jpg?ts=1795116376&ext=")
    assert snapshot._stamp(url) == snapshot._stamp(1795116376) == "1795116376"
