"""When the full Blink poll has to run every poll_interval — syncinfo.needs_fast_poll.

    pip install pytest && pytest -q tests

Why it bites: the full poll is what finds Local USB / Cloud motion clips, so it must
stay fast while such a module is armed; everything else (Live View modules, disarmed
modules) only needs status, and polling them every 30 s cost ~38 MB/h at rest.
"""
import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "blink-mqtt" / "blink_mqtt" / "syncinfo.py"
_spec = importlib.util.spec_from_file_location("syncinfo", _PATH)
syncinfo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(syncinfo)


class Cam:
    def __init__(self, clips=()):
        self.recent_clips = [{"time": "t", "clip": c} for c in clips]


class Sync:
    def __init__(self, arm, local_storage=False, clips=()):
        self.arm = arm
        self.local_storage = local_storage
        self.cameras = {"cam": Cam(clips)}


class BlinkOwl(Sync):  # blinkpy's pseudo-sync for a Mini: matched by class name
    pass


def fast(*syncs):
    return syncinfo.needs_fast_poll(syncs)


def test_armed_usb_module_needs_fast_poll():
    assert fast(Sync(arm=True, local_storage=True))


def test_armed_cloud_module_needs_fast_poll():
    assert fast(Sync(arm=True, clips=["https://rest-e006.immedia-semi.com/api/v2/media/clip.mp4"]))


def test_disarmed_modules_do_not():
    assert not fast(Sync(arm=False, local_storage=True), Sync(arm=False))


def test_armed_live_view_module_does_not():
    # Live View motion comes from the light homescreen watch, not the full poll.
    assert not fast(Sync(arm=True))


def test_standalone_counts_unless_known_disarmed():
    assert fast(BlinkOwl(arm=None))
    assert not fast(BlinkOwl(arm=False))


def test_any_module_needing_it_wins():
    assert fast(Sync(arm=True), Sync(arm=False), Sync(arm=True, local_storage=True))
