"""Thumbnails only on request — blink_mqtt/thumbs.py.

Why it bites: blinkpy downloaded every camera's thumbnail on each refresh in which it
changed (and on every refresh with force_cache). Pictures are wanted only on Snapshot;
the motion video path (get_video_clip) must stay untouched.
"""
import asyncio
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "blink-mqtt" / "blink_mqtt" / "thumbs.py"
_spec = importlib.util.spec_from_file_location("thumbs", _PATH)
thumbs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(thumbs)


class Resp:
    status = 200


class Cam:
    def __init__(self):
        self.calls = []
        self._cached_image = b"old"

    async def get_thumbnail(self, url=None):
        self.calls.append(("thumb", url))
        return Resp()

    async def get_video_clip(self, url=None):
        self.calls.append(("video", url))
        return Resp()


def run(coro):
    return asyncio.run(coro)


def test_thumbnail_blocked_until_requested():
    cam = Cam()
    thumbs.gate([cam])
    assert run(cam.get_thumbnail()) is None
    assert cam.calls == []


def test_request_lets_one_picture_through_and_clears_cache():
    cam = Cam()
    thumbs.gate([cam])
    thumbs.request(cam)
    assert cam._cached_image is None            # blinkpy re-downloads even if the URL is the same
    assert run(cam.get_thumbnail()).status == 200
    assert run(cam.get_thumbnail()) is None     # closed again after a successful download
    assert cam.calls == [("thumb", None)]


def test_failed_download_keeps_the_request_open():
    cam = Cam()

    async def failing(url=None):
        cam.calls.append(("thumb", url))
        r = Resp(); r.status = 500
        return r

    cam.get_thumbnail = failing
    thumbs.gate([cam])
    thumbs.request(cam)
    run(cam.get_thumbnail())
    assert cam.want_image is True


def test_video_untouched_and_gate_idempotent():
    cam = Cam()
    thumbs.gate([cam])
    gated = cam.get_thumbnail
    thumbs.gate([cam])
    assert cam.get_thumbnail is gated
    assert run(cam.get_video_clip("clip.mp4")).status == 200
    assert cam.calls == [("video", "clip.mp4")]
