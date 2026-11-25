"""Give USB clips with a camera name blinkpy does not know to the right camera.

blinkpy (0.25.5) reads a Local USB module's manifest in
update_local_storage_manifest() and keeps a clip only if its `camera_name` is
in the module's `_names_table` (alphanumeric camera name -> name). Any other
clip is skipped silently: it never reaches recent_clips, so no motion clip is
published and nothing in the log says why.

The sync module does not always use the name the camera has in the app.
Verified 2026-11-25: a Video Doorbell named "Soggiorno" in the app stores its
clips as "VideoDoorbell13EQ" (its factory name); a camera may also appear with
another letter case. `watch()` wraps each module's poll_local_storage_manifest()
and, before blinkpy reads the answer, renames such a clip to its camera:
  1. same name ignoring case and punctuation ("soggiorno" -> "Soggiorno");
  2. a factory doorbell name ("VideoDoorbell…", "Doorbell…") when the module
     has exactly one doorbell.
Only new clips matter: blinkpy publishes nothing older than its last manifest
read, so old clips of a removed camera with the same name stay unpublished.
No extra request to Blink — the manifest blinkpy fetched is the one inspected.

Log: `USB manifest <sync>: … camera names […] — known to blinkpy […] —
renamed {…} — dropped {…}` whenever the set of names changes, and a WARNING
for every new clip still dropped (old ones at start-up are only counted).
"""

import logging
import re

_LOGGER = logging.getLogger(__name__)

_DOORBELL_FACTORY = re.compile(r"^(video)?doorbell", re.IGNORECASE)


def watch(syncs) -> None:
    """Wrap poll_local_storage_manifest on every sync not wrapped yet (idempotent)."""
    for sync in syncs:
        if getattr(sync, "_manifest_watched", False) or not hasattr(sync, "_names_table"):
            continue
        original = sync.poll_local_storage_manifest

        async def watched(*args, _sync=sync, _original=original, **kwargs):
            response = await _original(*args, **kwargs)
            try:
                inspect(_sync, response)
            except Exception as e:   # must never break the poll
                _LOGGER.debug("Manifest inspection failed for %s: %r", _sync.name, e)
            return response

        sync.poll_local_storage_manifest = watched
        sync._manifest_watched = True
        sync._manifest_names = None
        sync._manifest_dropped = None   # clip ids already reported; None = not seeded


def _fold(name: str) -> str:
    return re.sub(r"\W+", "", name).lower()


def resolve(sync, name) -> str | None:
    """The `_names_table` key a manifest camera name belongs to, or None."""
    known = sync._names_table
    if name in known:
        return name
    if not isinstance(name, str):
        return None
    folded = [k for k in known if _fold(k) == _fold(name)]
    if len(folded) == 1:
        return folded[0]
    if _DOORBELL_FACTORY.match(name):
        bells = [k for k, cam_name in known.items()
                 if getattr(sync.cameras.get(cam_name), "camera_type", "") == "doorbell"]
        if len(bells) == 1:
            return bells[0]
    return None


def inspect(sync, response) -> None:
    """Rename resolvable clips in place (blinkpy reads them next); log the rest."""
    clips = response.get("clips") if isinstance(response, dict) else None
    if not isinstance(clips, list):
        return   # the first poll answers with a request id only
    names = sorted({str(c.get("camera_name")) for c in clips})
    renamed, dropped = {}, []
    for c in clips:
        name = c.get("camera_name")
        target = resolve(sync, name)
        if target is None:
            dropped.append(c)
        elif target != name:
            renamed[f"{name} -> {target}"] = renamed.get(f"{name} -> {target}", 0) + 1
            c["camera_name"] = target
    if names != sync._manifest_names:
        sync._manifest_names = names
        per_name = {}
        for c in dropped:
            per_name[str(c.get("camera_name"))] = per_name.get(str(c.get("camera_name")), 0) + 1
        _LOGGER.info("USB manifest %s: %d clips, camera names %s — known to blinkpy %s"
                     " — renamed %s — dropped %s", sync.name, len(clips), names,
                     sorted(sync._names_table), renamed or "none", per_name or "none")
    first = sync._manifest_dropped is None
    if first:
        sync._manifest_dropped = set()
    for clip in dropped:
        cid = clip.get("id")
        if cid in sync._manifest_dropped:
            continue
        sync._manifest_dropped.add(cid)
        if not first:
            _LOGGER.warning("USB clip %s of '%s' (%s) dropped by blinkpy: no camera of %s "
                            "has that name (clip fields: %s)", cid, clip.get("camera_name"),
                            clip.get("created_at"), sync.name, sorted(clip))
