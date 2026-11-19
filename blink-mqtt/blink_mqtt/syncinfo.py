"""What kind of sync module this is and where its motion clips come from.

Recording mode, decided in this order:
  LOCAL    — USB storage active. Blink's own flag: homescreen
             `local_storage_status == "active"`, exposed by blinkpy as
             `sync.local_storage`.
  CLOUD    — no USB, but Blink lists clips with cloud URLs for its cameras in
             `media/changed` (cam.recent_clips) → there is a subscription.
             No direct flag exists, so this is inferred from clips seen.
  LIVEVIEW — neither: Blink stores nothing; the add-on records the clip itself
             from Live View (see liveclip.py). This is the add-on's fallback,
             not a Blink setting.
  STANDALONE — blinkpy's pseudo-sync for a Mini (BlinkOwl) or doorbell (BlinkLotus).
"""

LOCAL = "Local USB"
CLOUD = "Cloud"
LIVEVIEW = "Live View"
STANDALONE = "Standalone"

_PSEUDO_SYNCS = {"BlinkOwl", "BlinkLotus"}


def homescreen_entry(blink, sync) -> dict | None:
    """The sync module's entry in the homescreen `sync_modules` list."""
    try:
        for mod in blink.homescreen.get("sync_modules") or []:
            if mod.get("id") == sync.sync_id:
                return mod
    except AttributeError:
        pass
    return None


def is_pseudo_sync(sync) -> bool:
    return type(sync).__name__ in _PSEUDO_SYNCS


def recording_mode(sync, cameras) -> str:
    if is_pseudo_sync(sync):
        return STANDALONE
    if sync.local_storage:
        return LOCAL
    for cam in cameras:
        for clip in cam.recent_clips or []:
            url = clip.get("clip") or ""
            if url and "local_storage" not in url:
                return CLOUD
    return LIVEVIEW


def needs_fast_poll(syncs) -> bool:
    """True when the full poll is what discovers new motion clips.

    Only an armed module records, and only Local USB / Cloud clips are found by
    the full poll (cam.recent_clips); a Live View module is watched by the light
    homescreen check instead. Standalone cameras (Mini, doorbell) carry no
    reliable arm state, so they count unless known disarmed.
    """
    for sync in syncs:
        mode = recording_mode(sync, list(sync.cameras.values()))
        if mode in (LOCAL, CLOUD) and sync.arm is True:
            return True
        if mode == STANDALONE and sync.arm is not False:
            return True
    return False


def describe(blink, sync, cameras) -> dict:
    """Fields shown in the web UI for one sync module."""
    entry = homescreen_entry(blink, sync) or {}
    model = " / ".join(str(entry[k]) for k in ("type", "subtype") if entry.get(k))
    usb = entry.get("local_storage_compatible")
    return {
        "model": model or None,
        "fw": entry.get("fw_version") or getattr(sync, "version", None),
        # True / False, None when unknown (pseudo-syncs have no entry)
        "usb_capable": usb if isinstance(usb, bool) else None,
        "recording": recording_mode(sync, cameras),
    }
