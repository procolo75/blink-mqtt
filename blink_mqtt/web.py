import asyncio
import logging
import time

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from slugify import slugify

from .auth import AuthManager, AuthState

_LOGGER = logging.getLogger(__name__)

templates = Jinja2Templates(directory="/templates")

_SNAP_WAIT = 8  # seconds to wait after snap_picture() for the camera to upload


def create_app(auth_manager: AuthManager, bridge_event: asyncio.Event) -> FastAPI:
    app = FastAPI()

    def _base(request: Request) -> str:
        return request.headers.get("X-Ingress-Path", "").rstrip("/")

    def _cam_list():
        if not auth_manager.blink:
            return []
        cams = []
        for cam in auth_manager.blink.cameras.values():
            cam_cls = type(cam).__name__   # BlinkCamera / BlinkCameraMini / BlinkDoorbell
            cams.append({
                "name": cam.name,
                "serial": cam.serial or slugify(cam.name),
                "battery": cam.battery,
                "temperature": round((cam.temperature - 32) * 5 / 9, 1) if cam.temperature is not None else None,
                "wifi": cam.wifi_strength,
                "motion": cam.motion_detected is True,
                # Use sync module arm state (not per-camera motion_enabled).
                # motion_enabled is an individual camera setting that doesn't
                # change when you arm/disarm the sync module.
                "armed": getattr(cam.sync, "arm", None) is True,
                "cam_type": cam_cls,
                "is_mini": cam_cls == "BlinkCameraMini",
            })
        return cams

    def _sync_list():
        if not auth_manager.blink:
            return []
        return [
            {
                "name": name,
                "nid": str(sync.network_id),
                # arm can be True/False/None
                "armed": sync.arm is True,
            }
            for name, sync in auth_manager.blink.sync.items()
        ]

    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "base": _base(request),
                "state": auth_manager.state.value,
                "error": auth_manager.error_msg,
                "cameras": _cam_list(),
                "syncs": _sync_list(),
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
            old_url = cam.thumbnail
            _LOGGER.info("Snapshot %s — thumbnail before: %s", camera_name, old_url)
            ret = await cam.snap_picture()
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
        return RedirectResponse(url=f"{_base(request)}/", status_code=303)

    @app.post("/arm/{nid}/{value}")
    async def arm(request: Request, nid: str, value: str):
        blink = auth_manager.blink
        if blink:
            for sync in blink.sync.values():
                if str(sync.network_id) == nid:
                    await sync.async_arm(value == "on")
                    # async_arm() sends the command but does NOT update the local
                    # network_info dict. Force a full refresh so sync.arm reflects
                    # the new state before we redirect and re-render the page.
                    await asyncio.sleep(1.0)
                    await blink.refresh(force=True)
                    mqtt = getattr(request.app.state, "mqtt", None)
                    if mqtt:
                        mqtt.publish_sync(sync)
                    break
        return RedirectResponse(url=f"{_base(request)}/", status_code=303)

    return app
