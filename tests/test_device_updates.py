"""Motion watch of Live View modules sees doorbells and Minis too — bridge._check_device_updates.

    pip install pytest && pytest -q tests

Why it bites (2026-11-25): a Video Doorbell added to a storage-less sync module is a
camera of that module in blinkpy, but Blink lists it under homescreen `doorbells`, not
`cameras`. Only `cameras` was watched, so its motion never started a Live View clip:
the manual Record clip worked, nothing reached Telegram on motion.
"""
import sys
import types
from pathlib import Path

# Stub the add-on's third-party imports and the modules bridge.py pulls in.
for name in ("blinkpy", "blinkpy.api", "blinkpy.livestream", "slugify",
             "paho", "paho.mqtt", "paho.mqtt.client"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["blinkpy"].api = sys.modules["blinkpy.api"]
sys.modules["blinkpy.livestream"].BlinkLiveStream = getattr(
    sys.modules["blinkpy.livestream"], "BlinkLiveStream", object)
sys.modules["slugify"].slugify = lambda s: s.lower().replace(" ", "-")
_auth = types.ModuleType("blink_mqtt.auth")
_auth.AUTH_ERRORS = (RuntimeError,)
_auth.AuthManager = object
sys.modules.setdefault("blink_mqtt.auth", _auth)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "blink-mqtt"))

from blink_mqtt import bridge, settings, syncinfo  # noqa: E402


class Sync:
    network_id = 7
    arm = True
    local_storage = False

    def __init__(self):
        self.cameras = {}


class Cam:
    def __init__(self, sync, name, cid, camera_type):
        self.sync, self.name, self.camera_id, self.camera_type = sync, name, cid, camera_type
        self.serial = name
        self.recent_clips = []
        sync.cameras[name] = self


def make_bridge(cams):
    b = bridge.Bridge.__new__(bridge.Bridge)
    b._blink = types.SimpleNamespace(cameras={c.name: c for c in cams},
                                     sync={"s": cams[0].sync})
    b._device_updated, b._prev_armed = {}, {}
    b.recorded = []
    b._spawn = lambda coro: (b.recorded.append(coro.cr_frame.f_locals["serial"]), coro.close())
    return b


def home(cam_ts, bell_ts):
    net = [{"id": 7, "armed": True}]
    return {"networks": net,
            # same id 5 in both lists: ids are unique only within a list
            "cameras": [{"id": 5, "network_id": 7, "name": "Cucina", "updated_at": cam_ts}],
            "doorbells": [{"id": 5, "network_id": 7, "name": "Soggiorno", "updated_at": bell_ts}]}


def test_doorbell_motion_records(monkeypatch):
    monkeypatch.setattr(settings, "blink_watch", lambda: True)
    sync = Sync()
    cams = [Cam(sync, "Cucina", 5, ""), Cam(sync, "Soggiorno", 5, "doorbell")]
    assert syncinfo.recording_mode(sync, cams) == syncinfo.LIVEVIEW
    b = make_bridge(cams)
    t0, t1 = "2026-11-25T09:00:00+00:00", "2026-11-25T09:15:00+00:00"
    b._check_device_updates(home(t0, t0))       # seeds, armed now
    b._check_device_updates(home(t0, t1))       # only the doorbell moved
    assert b.recorded == ["Soggiorno"]
    b._check_device_updates(home(t1, t1))       # now the camera, same id
    assert b.recorded == ["Soggiorno", "Cucina"]
