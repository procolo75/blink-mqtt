"""Record a short MP4 clip from a camera's Live View.

Used for sync modules where Blink stores nothing (no USB storage, no
subscription): Live View is the only video Blink hands out there.

blinkpy's BlinkLiveStream connects to Blink's IMMIS server and re-serves the
MPEG-TS payload on a local TCP socket (stream.url). ffmpeg reads that socket
for `seconds` and remuxes it to MP4 without re-encoding. When ffmpeg
disconnects, BlinkLiveStream stops itself (last client gone) and tells Blink
the command is done.

Limits: Live View starts a few seconds after it is requested, so a clip
triggered by motion shows what happens after the trigger; every recording wakes
the camera and costs battery.
"""
import asyncio
import logging
import os
import tempfile
import time
from collections import deque
from json import dumps

from blinkpy import api as blink_api
import ssl

from blinkpy.livestream import BlinkLiveStream

_LOGGER = logging.getLogger(__name__)

# Extra time on top of the clip length for Live View to start and ffmpeg to exit.
_STARTUP_GRACE = 25

# One recording per camera at a time (keyed by serial).
_locks: dict[str, asyncio.Lock] = {}

# Last time (epoch) the add-on itself woke each camera (Live View, snapshot).
# Blink bumps the camera's homescreen `updated_at` for these too, so the
# motion trigger must not mistake them for motion.
_own_activity: dict[str, float] = {}
_OWN_MARGIN = 20   # seconds after our activity still attributed to us


# Last automatic/manual starts, newest last — the web UI's "who came first" table.
# source: "blink" (updated_at watch), "external" (motion/trigger, e.g. Alexa),
# "manual" (Record clip button / record/trigger).
_history: deque = deque(maxlen=10)


def history() -> list[dict]:
    return list(reversed(_history))


_last_blink_motion: dict[str, float] = {}


def note_blink_motion(serial: str, motion_at: float):
    """Attach Blink's motion stamp to a recent start of this camera that has none.

    Lets an external start (Alexa) be compared with when Blink itself saw the
    motion: lead = start - motion_at (negative = started before Blink's stamp).
    """
    _last_blink_motion[serial] = motion_at
    for entry in reversed(_history):
        if entry["serial"] == serial and entry["blink_motion"] is None \
                and abs(entry["started"] - motion_at) <= 30:
            entry["blink_motion"] = motion_at
            return


def mark_own_activity(serial: str):
    _own_activity[serial] = time.time()


def is_own_activity(serial: str, when: float) -> bool:
    """True if a camera update at epoch `when` was caused by the add-on."""
    last = _own_activity.get(serial)
    return last is not None and when <= last + _OWN_MARGIN


class LiveViewError(Exception):
    """Blink refused or did not answer the Live View request."""


# Live View endpoint per blinkpy camera_type. Taken from blinkpy 0.25.9: the
# paths in our pinned 0.25.5 (v5 cameras, v1 owls/doorbells) are now refused by
# Blink with "An app update is required". blinkpy stays pinned at 0.25.5
# because auth.py patches its login internals.
_LIVEVIEW_PATHS = {
    "mini": "/api/v2/accounts/{account}/networks/{network}/owls/{camera}/liveview",
    "doorbell": "/api/v2/accounts/{account}/networks/{network}/doorbells/{camera}/liveview",
    "default": "/api/v6/accounts/{account}/networks/{network}/cameras/{camera}/liveview",
}
_LIVEVIEW_BODY = {"intent": "liveview", "motion_event_start_time": None}


class _LiveStream(BlinkLiveStream):
    """BlinkLiveStream with a receive loop that waits for whole packets.

    blinkpy's recv() (0.25.5, unchanged in 0.25.9) reads each IMMIS packet
    with StreamReader.read(n), which may return fewer than n bytes when a
    packet arrives split across TCP segments — normal on the internet. blinkpy
    takes that as the end of the stream ("Insufficient data for payload") and
    closes it: clips came out 4 s long instead of 10 (verified 2026-11-19).
    Here readexactly() waits for the whole header/payload; the stream ends
    only when Blink closes it or ffmpeg has its seconds. Only recv() differs.
    """

    async def recv(self):
        try:
            while not self.target_reader.at_eof():
                header = await self.target_reader.readexactly(9)
                msgtype = header[0]
                length = int.from_bytes(header[5:9], byteorder="big")
                if length <= 0:
                    continue
                payload = await self.target_reader.readexactly(length)
                # Only msgtype 0x00 carries video, as MPEG-TS packets (0x47).
                if msgtype != 0x00 or payload[0] != 0x47:
                    continue
                for writer in self.clients:
                    if not writer.is_closing():
                        writer.write(payload)
                        await writer.drain()
        except asyncio.IncompleteReadError:
            _LOGGER.debug("Live View stream closed by Blink")
        except ssl.SSLError as e:
            if e.reason != "APPLICATION_DATA_AFTER_CLOSE_NOTIFY":
                _LOGGER.warning("Live View SSL error: %s", e)
        except (ConnectionError, OSError) as e:
            _LOGGER.debug("Live View connection ended: %r", e)
        finally:
            self.target_writer.close()


async def _open_liveview(cam) -> BlinkLiveStream:
    """Request Live View and wrap the answer in blinkpy's BlinkLiveStream.

    Not cam.init_livestream(): in blinkpy 0.25.5 it goes through
    request_camera_action(), which waits (up to 120 s) for the Live View
    command to *complete* before returning — by then the stream is over — and
    a refusal surfaces only as KeyError('server'), hiding Blink's answer.
    """
    blink = cam.sync.blink
    path = _LIVEVIEW_PATHS.get(cam.camera_type or "default", _LIVEVIEW_PATHS["default"])
    url = blink.urls.base_url + path.format(
        account=blink.account_id, network=cam.network_id, camera=cam.camera_id)
    resp = await blink_api.http_post(blink, url, data=dumps(_LIVEVIEW_BODY))
    server = (resp or {}).get("server") if isinstance(resp, dict) else None
    if not server:
        _LOGGER.warning("Live View %s refused, Blink answered: %s", cam.name, resp)
        msg = resp.get("message") if isinstance(resp, dict) else None
        raise LiveViewError(msg or f"no stream server in answer: {resp}")
    if not server.startswith("immis://"):
        raise LiveViewError(f"unsupported stream protocol: {server.split(':', 1)[0]}")
    _LOGGER.info("Live View %s: command %s", cam.name, resp.get("command_id"))
    return _LiveStream(cam, resp)


def looks_like_mp4(data) -> bool:
    """MP4/ISO-BMFF files carry the 'ftyp' box tag at bytes 4..8."""
    return isinstance(data, (bytes, bytearray)) and data[4:8] == b"ftyp"


def is_recording(serial: str) -> bool:
    lock = _locks.get(serial)
    return bool(lock and lock.locked())


async def record_clip(cam, serial: str, seconds: int, source: str = "manual",
                      blink_motion: float | None = None) -> bytes | None:
    """Record `seconds` of Live View from `cam`; MP4 bytes, or None on failure.

    Returns None at once if this camera is already recording.
    Raises LiveViewError if Blink refuses Live View; auth errors propagate.
    """
    now = time.time()
    if blink_motion is None:   # Blink may have stamped the motion just before
        last = _last_blink_motion.get(serial)
        if last is not None and abs(now - last) <= 30:
            blink_motion = last
    entry = {"serial": serial, "camera": cam.name, "source": source,
             "started": now, "blink_motion": blink_motion,
             "result": "recording", "duration": None, "size_kb": None}
    _history.append(entry)
    lock = _locks.setdefault(serial, asyncio.Lock())
    if lock.locked():
        _LOGGER.info("Live clip %s: already recording, skipped (%s)", cam.name, source)
        entry["result"] = "skipped (already recording)"
        return None
    async with lock:
        mark_own_activity(serial)
        try:
            data, duration = await _record(cam, serial, seconds)
        except Exception as e:
            entry["result"] = f"failed: {e}"
            raise
        finally:
            mark_own_activity(serial)
    if data is None:
        entry["result"] = "no video"
    else:
        entry["result"] = "sent"
        entry["duration"] = duration
        entry["size_kb"] = len(data) // 1024
    return data


async def _record(cam, serial: str, seconds: int) -> tuple[bytes | None, float | None]:
    """(MP4 bytes or None, real duration in s or None)."""
    stream = await _open_liveview(cam)
    await stream.start()   # local TCP server on a free port
    feed = asyncio.create_task(stream.feed())
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        await _run_ffmpeg(cam.name, stream.url, seconds, path)
        with open(path, "rb") as f:
            data = f.read()
    finally:
        stream.stop()
        feed.cancel()
        try:
            await feed
        except (asyncio.CancelledError, Exception):
            pass
        try:
            os.remove(path)
        except OSError:
            pass

    if not looks_like_mp4(data):
        _LOGGER.warning("Live clip %s: no video received (%d bytes)", cam.name, len(data))
        return None, None
    actual = await _probe_duration(data)
    if actual is not None and actual < seconds - 1:
        _LOGGER.warning("Live clip %s: stream ended early, %.1f s of %d requested, %d KB",
                        cam.name, actual, seconds, len(data) // 1024)
    else:
        _LOGGER.info("Live clip %s: %s s (requested %d), %d KB", cam.name,
                     f"{actual:.1f}" if actual is not None else "?", seconds, len(data) // 1024)
    return data, actual


# ── Live view in the web UI ───────────────────────────────────────────────
#
# MJPEG (multipart/x-mixed-replace in an <img>), not fragmented MP4 in a
# <video>: the HA iOS app's WebView does not reliably play a progressive MP4
# stream, MJPEG works everywhere. ffmpeg's `mpjpeg` muxer writes the multipart
# parts itself (boundary "ffmpeg"), so the bytes go to the browser as they are.
# Fixed 10 s for every camera: each second of Live View costs battery.

LIVE_SECONDS = 10
LIVE_BOUNDARY = "ffmpeg"


class CameraBusy(Exception):
    """The camera is already in Live View (clip recording or another viewer)."""


async def live_mjpeg(cam, serial: str):
    """Async generator of MJPEG multipart bytes: LIVE_SECONDS of Live View.

    Shares the per-camera lock with record_clip(): one Live View per camera, so
    a motion clip that fires meanwhile is skipped. Raises CameraBusy, or
    LiveViewError / auth errors, on the first iteration — the caller primes it
    to turn them into an HTTP status. Live View is stopped when the stream
    ends, the viewer goes away (generator closed or cancelled) or the hard
    ceiling of LIVE_SECONDS + _STARTUP_GRACE is reached.
    """
    lock = _locks.setdefault(serial, asyncio.Lock())
    if lock.locked():
        raise CameraBusy(cam.name)
    async with lock:
        mark_own_activity(serial)
        started = time.monotonic()
        reason, sent = "stream ended", 0
        stream = feed = proc = None

        def _hard_stop():
            # Fires even if the consumer abandoned the generator suspended at
            # `yield` (closed connection not yet collected): the camera never
            # stays in Live View past the ceiling.
            if proc is not None and proc.returncode is None:
                proc.kill()
            if stream is not None:
                stream.stop()

        killer = asyncio.get_running_loop().call_later(
            LIVE_SECONDS + _STARTUP_GRACE, _hard_stop)
        try:
            stream = await _open_liveview(cam)
            await stream.start()
            feed = asyncio.create_task(stream.feed())
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                # small probe: the first frame should not wait for 5 s of analysis
                "-probesize", "200000", "-analyzeduration", "1000000",
                "-i", stream.url, "-t", str(LIVE_SECONDS), "-an",
                "-vf", "fps=5,scale=640:-2", "-q:v", "7",
                "-f", "mpjpeg", "-boundary_tag", LIVE_BOUNDARY, "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            deadline = started + LIVE_SECONDS + _STARTUP_GRACE
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    reason = "time limit"
                    break
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(65536), left)
                except asyncio.TimeoutError:
                    reason = "time limit"
                    break
                if not chunk:
                    break
                sent += len(chunk)
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            reason = "closed by viewer"
            raise
        except Exception as e:
            reason = f"error: {e!r}"
            raise
        finally:
            killer.cancel()
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            if stream is not None:
                stream.stop()
            if feed is not None:
                feed.cancel()
                try:
                    await feed
                except (asyncio.CancelledError, Exception):
                    pass
            mark_own_activity(serial)
            _LOGGER.info("Live view %s: ended after %.0f s (%s), %d KB sent",
                         cam.name, time.monotonic() - started, reason, sent // 1024)


async def _probe_duration(data: bytes) -> float | None:
    """Real duration of an MP4 via ffprobe (reads it from stdin); None if unknown."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", "-i", "pipe:0",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(data), 15)
        return float(out.strip())
    except (OSError, ValueError, asyncio.TimeoutError):
        return None


async def _run_ffmpeg(name: str, url: str, seconds: int, path: str):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", url, "-t", str(seconds),
        "-c", "copy", "-movflags", "+faststart", "-f", "mp4", path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), seconds + _STARTUP_GRACE)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        _LOGGER.warning("Live clip %s: ffmpeg timed out", name)
        return
    if proc.returncode != 0:
        _LOGGER.warning("Live clip %s: ffmpeg exit %s: %s", name, proc.returncode,
                        err.decode(errors="replace").strip()[-300:])
