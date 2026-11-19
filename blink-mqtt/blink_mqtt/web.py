import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from blinkpy import api as blink_api
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse, Response,
                               StreamingResponse)
from fastapi.templating import Jinja2Templates
from slugify import slugify

from . import config, settings, syncinfo
from .auth import AUTH_ERRORS, AuthManager, AuthState
from .liveclip import (LIVE_BOUNDARY, LIVE_SECONDS, CameraBusy, history, is_recording,
                       live_mjpeg, mark_own_activity, record_clip)

_LOGGER = logging.getLogger(__name__)

templates = Jinja2Templates(directory="/templates")

_SNAP_WAIT = 8  # seconds to wait after snap_picture() for the camera to upload


def create_app(auth_manager: AuthManager, bridge_event: asyncio.Event) -> FastAPI:
    app = FastAPI()

    def _base(request: Request) -> str:
        return request.headers.get("X-Ingress-Path", "").rstrip("/")

    @asynccontextmanager
    async def _blink_errors(action: str):
        """Turn Blink API failures into a UI message instead of an HTTP 500."""
        try:
            yield
        except AUTH_ERRORS as e:
            _LOGGER.warning("%s failed: %s", action, repr(e))
            if not auth_manager.note_auth_failure(e):
                auth_manager.error_msg = f"{action} failed — session refreshing, retry"
        except Exception as e:
            _LOGGER.error("%s failed: %s", action, repr(e))
            auth_manager.error_msg = f"{action} failed: {e}"

    def _cam_info(cam) -> dict:
        cam_cls = type(cam).__name__   # BlinkCamera / BlinkCameraMini / BlinkDoorbell
        return {
            "name": cam.name,
            "serial": cam.serial or slugify(cam.name),
            "battery": cam.battery,
            "temperature": round((cam.temperature - 32) * 5 / 9, 1) if cam.temperature is not None else None,
            "wifi": cam.wifi_strength,
            "motion": cam.motion_detected is True,
            "cam_type": cam_cls,
            "is_mini": cam_cls == "BlinkCameraMini",
            "recording": is_recording(cam.serial or slugify(cam.name)),
        }

    def _groups() -> list[dict]:
        """Cameras grouped under the sync module they belong to.

        The armed state shown is the sync module's (not per-camera
        motion_enabled, which does not change when the module is armed).
        blinkpy models each Mini as its own pseudo-sync, so a Mini gets its
        own group. Cameras whose sync matches none end up in "Other".
        """
        blink = auth_manager.blink
        if not blink:
            return []
        cams = list(blink.cameras.values())
        groups, placed = [], set()
        for name, sync in blink.sync.items():
            members = [c for c in cams if c.sync is sync]
            placed.update(id(c) for c in members)
            groups.append({
                "name": name,
                "nid": str(sync.network_id),
                "armed": sync.arm is True,   # arm can be True/False/None
                "cameras": [_cam_info(c) for c in members],
                **syncinfo.describe(blink, sync, members),
            })
        orphans = [c for c in cams if id(c) not in placed]
        if orphans:
            groups.append({"name": "Other", "nid": None, "armed": False,
                           "cameras": [_cam_info(c) for c in orphans]})
        return groups

    _SOURCE_LABELS = {"blink": "Blink (polling)", "external": "Alexa / external",
                      "manual": "Manual"}

    def _hms(ts: float | None) -> str | None:
        return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else None

    def _history_rows() -> list[dict]:
        rows = []
        for e in history():
            lead = (e["started"] - e["blink_motion"]) if e["blink_motion"] else None
            rows.append({
                "started": _hms(e["started"]),
                "camera": e["camera"],
                "source": _SOURCE_LABELS.get(e["source"], e["source"]),
                "blink_motion": _hms(e["blink_motion"]),
                "lead": f"{lead:+.0f} s" if lead is not None else None,
                "result": e["result"],
                "duration": f"{e['duration']:.1f} s" if e["duration"] else None,
            })
        return rows

    def _script_yaml(cam_name: str, serial: str) -> str:
        return (f"alias: Blink {cam_name} motion\n"
                f"mode: single\n"
                f"sequence:\n"
                f"  - action: mqtt.publish\n"
                f"    data:\n"
                f"      topic: blink/cameras/{serial}/motion/trigger\n"
                f"      payload: \"ON\"\n")

    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        groups = _groups()
        liveview = [g for g in groups if g.get("recording") == syncinfo.LIVEVIEW]
        for g in liveview:
            for c in g["cameras"]:
                c["script"] = _script_yaml(c["name"], c["serial"])
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "base": _base(request),
                "state": auth_manager.state.value,
                "error": auth_manager.error_msg,
                "groups": groups,
                "liveview_groups": liveview,
                "own_clip_groups": [g for g in groups
                                    if g.get("recording") in (syncinfo.LOCAL, syncinfo.CLOUD)],
                "settings": settings.get(),
                "history": _history_rows(),
                "clip_seconds": settings.get()["clip_seconds"],
                "live_seconds": LIVE_SECONDS,
                "poll_interval": 30,
                "now": int(time.time()),
            },
        )

    @app.post("/login")
    async def login(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
    ):
        ok = await auth_manager.start_login(email, password)
        if ok:
            bridge_event.set()
        return RedirectResponse(url=f"{_base(request)}/", status_code=303)

    @app.post("/otp")
    async def otp(request: Request, code: str = Form(...)):
        ok = await auth_manager.complete_otp(code)
        if ok:
            bridge_event.set()
        return RedirectResponse(url=f"{_base(request)}/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        auth_manager.reset()
        # Stop the bridge and mark the entities unavailable — otherwise it keeps
        # polling with the session the user just dropped.
        mqtt = getattr(request.app.state, "mqtt", None)
        if mqtt:
            mqtt.set_available(False)
        bridge_event.set()
        return RedirectResponse(url=f"{_base(request)}/", status_code=303)

    @app.get("/image/{camera_name:path}")
    async def camera_image(camera_name: str):
        blink = auth_manager.blink
        if blink and camera_name in blink.cameras:
            img = blink.cameras[camera_name].image_from_cache
            if img:
                return Response(content=img, media_type="image/jpeg")
        return Response(status_code=404)

    @app.post("/snapshot/{camera_name:path}")
    async def snapshot(request: Request, camera_name: str):
        blink = auth_manager.blink
        if blink and camera_name in blink.cameras:
            cam = blink.cameras[camera_name]
            async with _blink_errors("Snapshot"):
                old_url = cam.thumbnail
                _LOGGER.info("Snapshot %s — thumbnail before: %s", camera_name, old_url)
                serial = cam.serial or slugify(cam.name)
                mark_own_activity(serial)
                ret = await cam.snap_picture()
                mark_own_activity(serial)
                _LOGGER.info("Snapshot %s — snap_picture() returned: %s", camera_name, ret)
                # wait_for_command() inside snap_picture() handles command completion,
                # but image upload to Blink CDN may still be in progress
                await asyncio.sleep(_SNAP_WAIT)
                # force_cache=True: re-download image even if thumbnail URL didn't change
                # (BlinkCameraMini often reuses the same URL)
                await blink.refresh(force=True, force_cache=True)
                _LOGGER.info("Snapshot %s — thumbnail after: %s", camera_name, cam.thumbnail)
                mqtt = getattr(request.app.state, "mqtt", None)
                if mqtt:
                    mqtt.publish_camera(cam, publish_image=True)
                auth_manager.note_auth_success()
        return RedirectResponse(url=f"{_base(request)}/", status_code=303)

    @app.post("/record/{camera_name:path}")
    async def record(request: Request, camera_name: str):
        """Record a Live View clip and publish it on .../event/clip."""
        blink = auth_manager.blink
        if blink and camera_name in blink.cameras:
            cam = blink.cameras[camera_name]
            async with _blink_errors("Record"):
                clip = await record_clip(cam, cam.serial or slugify(cam.name),
                                         settings.get()["clip_seconds"], "manual")
                if clip is None:
                    auth_manager.error_msg = f"Record {camera_name}: no video (see log)"
                else:
                    mqtt = getattr(request.app.state, "mqtt", None)
                    if mqtt:
                        mqtt.publish_motion_event(cam, clip)
                    auth_manager.note_auth_success()
        return RedirectResponse(url=f"{_base(request)}/", status_code=303)

    def _live_fail(camera_name: str, status: int, msg: str) -> Response:
        _LOGGER.warning("Live view %s: refused (%d %s)", camera_name, status, msg)
        return Response(msg, status_code=status, media_type="text/plain")

    @app.get("/live/{camera_name:path}")
    async def live(camera_name: str):
        """LIVE_SECONDS of Live View as MJPEG parts, read by the page with fetch().

        Not shown with <img src>: HA Ingress (core and Supervisor) keeps only
        the part of Content-Type before ';', so the browser never gets
        `boundary=ffmpeg` and an <img> cannot split the frames. The page knows
        the boundary and parses the parts itself (Content-length of each).

        The generator is primed here (Live View opened, first frame read), so a
        busy camera or a Blink refusal becomes an HTTP status + text the page
        shows. Dropping the connection (Close, page closed) stops Live View.
        """
        _LOGGER.info("Live view %s: requested", camera_name)
        blink = auth_manager.blink
        cam = blink.cameras.get(camera_name) if blink else None
        if cam is None:
            return _live_fail(camera_name, 404, "camera not found by the add-on")
        frames = live_mjpeg(cam, cam.serial or slugify(cam.name))
        try:
            first = await frames.__anext__()
        except CameraBusy:
            return _live_fail(camera_name, 409, "camera busy: a clip is being recorded")
        except StopAsyncIteration:
            return _live_fail(camera_name, 502, "Live View opened but no video arrived")
        except AUTH_ERRORS as e:
            auth_manager.note_auth_failure(e)
            return _live_fail(camera_name, 502, f"Blink session error: {e!r}")
        except Exception as e:
            return _live_fail(camera_name, 502, f"Live View failed: {e}")
        auth_manager.note_auth_success()

        async def body():
            try:
                yield first
                async for chunk in frames:
                    yield chunk
            finally:
                await frames.aclose()

        return StreamingResponse(
            body(), media_type=f"multipart/x-mixed-replace;boundary={LIVE_BOUNDARY}",
            headers={"Cache-Control": "no-store"})

    @app.post("/settings")
    async def save_settings(
        request: Request,
        trigger_mode: str = Form(...),
        motion_poll_interval: int = Form(...),
        clip_seconds: int = Form(...),
    ):
        settings.save({"trigger_mode": trigger_mode,
                       "motion_poll_interval": motion_poll_interval,
                       "clip_seconds": clip_seconds})
        return RedirectResponse(url=f"{_base(request)}/#settings", status_code=303)

    @app.get("/debug/sync/{nid}")
    async def debug_sync(nid: str):
        """Raw Blink data for one sync module, to find where motion shows up.

        Read-only, on demand. Open it right after a motion notification.
        """
        blink = auth_manager.blink
        sync = next((s for s in (blink.sync.values() if blink else [])
                     if str(s.network_id) == nid), None)
        if sync is None:
            return JSONResponse({"error": f"sync network_id={nid} not found"}, status_code=404)

        home = blink.homescreen or {}
        out = {
            "name": sync.name,
            "class": type(sync).__name__,
            "network_id": sync.network_id,
            "local_storage": sync.local_storage,
            "recording": syncinfo.recording_mode(sync, list(sync.cameras.values())),
            "homescreen_sync_module": syncinfo.homescreen_entry(blink, sync),
            "homescreen_devices": {
                key: [d for d in home.get(key) or [] if str(d.get("network_id")) == nid]
                for key in ("cameras", "owls", "doorbells")
            },
        }
        try:
            out["network_events"] = await blink_api.request_sync_events(blink, nid)
        except Exception as e:
            out["network_events"] = f"error: {e!r}"
        try:
            resp = await blink_api.request_videos(blink, time=time.time() - 900, page=1)
            media = (resp or {}).get("media") or []
            out["media_last_15min"] = [m for m in media if str(m.get("network_id")) == nid]
            out["media_last_15min_all_networks"] = len(media)
        except Exception as e:
            out["media_last_15min"] = f"error: {e!r}"
        return JSONResponse(out)

    @app.post("/arm/{nid}/{value}")
    async def arm(request: Request, nid: str, value: str):
        blink = auth_manager.blink
        if blink:
            for sync in blink.sync.values():
                if str(sync.network_id) == nid:
                    async with _blink_errors("Arm"):
                        await sync.async_arm(value == "on")
                        # async_arm() sends the command but does NOT update the local
                        # network_info dict. Force a full refresh so sync.arm reflects
                        # the new state before we redirect and re-render the page.
                        await asyncio.sleep(1.0)
                        await blink.refresh(force=True)
                        mqtt = getattr(request.app.state, "mqtt", None)
                        if mqtt:
                            mqtt.publish_sync(sync)
                        auth_manager.note_auth_success()
                    break
        return RedirectResponse(url=f"{_base(request)}/", status_code=303)

    return app
