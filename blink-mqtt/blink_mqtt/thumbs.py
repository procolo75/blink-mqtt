"""Thumbnails only on request.

blinkpy downloads a camera's home-screen thumbnail on every refresh in which the
thumbnail URL changes (and, with force_cache, on every refresh at all). They are
not needed: the motion video is what matters, and a fresh picture is one
Snapshot away. So each camera's `get_thumbnail` is wrapped: it hits the network
only when a picture was asked for (`request()`), then closes again.

`get_video_clip` is not touched: motion clips download exactly as before.
"""

import logging

_LOGGER = logging.getLogger(__name__)


def gate(cameras) -> None:
    """Wrap get_thumbnail on every camera not wrapped yet (idempotent)."""
    for cam in cameras:
        if getattr(cam, "_thumb_gated", False):
            continue
        original = cam.get_thumbnail

        async def gated(url=None, _cam=cam, _original=original):
            if not getattr(_cam, "want_image", False):
                return None
            response = await _original(url)
            if response is not None and getattr(response, "status", None) == 200:
                _cam.want_image = False
            return response

        cam.get_thumbnail = gated
        cam.want_image = getattr(cam, "want_image", False)
        cam._thumb_gated = True


def request(cam) -> None:
    """Let the next refresh download this camera's picture, even if its URL did
    not change (a Mini reuses it): blinkpy re-downloads when its cache is empty."""
    cam.want_image = True
    cam._cached_image = None
