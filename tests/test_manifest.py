"""USB clips with a name blinkpy does not know go to the right camera — manifest.py.

    pip install pytest && pytest -q tests

Why it bites (2026-11-25): a Video Doorbell named "Soggiorno" in the Blink app saved
its clips on the USB stick as "VideoDoorbell13EQ". blinkpy keeps a manifest clip only
if its camera_name is one of the module's cameras, silently: no clip ever reached
Telegram and the log said nothing.
"""
import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "blink-mqtt"))

from blink_mqtt import manifest  # noqa: E402


class Sync:
    name = "Casa"

    def __init__(self, answers, doorbells=1):
        self._names_table = {"Soggiorno": "Soggiorno", "cucina": "cucina"}
        self.cameras = {"Soggiorno": SimpleNamespace(camera_type="doorbell"),
                        "cucina": SimpleNamespace(camera_type="")}
        if doorbells == 2:
            self._names_table["Porta"] = "Porta"
            self.cameras["Porta"] = SimpleNamespace(camera_type="doorbell")
        self.answers = list(answers)

    async def poll_local_storage_manifest(self, manifest_request_id=None, max_retries=4):
        return self.answers.pop(0)


def clip(cid, name):
    return {"id": cid, "camera_name": name, "created_at": "2026-11-25T09:00:00+00:00"}


def run(sync, n):
    return [asyncio.run(sync.poll_local_storage_manifest()) for _ in range(n)]


def test_factory_doorbell_name_and_case_are_renamed():
    sync = Sync([{"clips": [clip(1, "VideoDoorbell13EQ"), clip(2, "soggiorno"),
                            clip(3, "cucina")]}])
    manifest.watch([sync])
    manifest.watch([sync])   # idempotent
    (resp,) = run(sync, 1)
    assert [c["camera_name"] for c in resp["clips"]] == ["Soggiorno", "Soggiorno", "cucina"]


def test_factory_doorbell_name_is_ambiguous_with_two_doorbells():
    sync = Sync([{"clips": [clip(1, "VideoDoorbell13EQ")]}], doorbells=2)
    manifest.watch([sync])
    (resp,) = run(sync, 1)
    assert resp["clips"][0]["camera_name"] == "VideoDoorbell13EQ"   # left to blinkpy: dropped


def test_dropped_clips_are_logged_only_when_new(caplog):
    old = [clip(1, "cucina"), clip(2, "Vecchia")]
    sync = Sync([{"id": 9}, {"clips": old}, {"clips": old + [clip(3, "Garage")]}])
    manifest.watch([sync])
    with caplog.at_level(logging.INFO, logger="blink_mqtt.manifest"):
        run(sync, 3)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(warnings) == 1 and "'Garage'" in warnings[0]   # old clip 2 only counted
    assert "{'Vecchia': 1}" in infos[0]
    assert len(infos) == 2   # the name set changed once
