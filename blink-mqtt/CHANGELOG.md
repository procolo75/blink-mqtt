# Changelog

All notable changes to this add-on are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.11.0] - 2026-11-24

### Fixed
- **An offline sync module or camera was shown as OK.** blinkpy reads a sync module's status
  only at start-up, and when Blink reports a sync module error it skips the refresh: the
  cameras kept their last battery "ok", temperature and WiFi. The status now comes from
  Blink's homescreen (refreshed on every poll) for both the sync module and the camera, plus
  any Snapshot/Live View command Blink failed in the last 15 minutes. Every status value
  Blink sends is logged at start and on change (`Blink status 'online' -> 'offline'`).
  Detection delay: the next full poll (`poll_interval` 30 s for an armed USB/Cloud module,
  `idle_poll_interval` 300 s otherwise); about `motion_poll_interval` (5 s) for an armed Live
  View module, whose motion watch already reads the homescreen — no extra request to Blink.
- **Snapshot showed the previous picture, and the new one only on the next Snapshot.** It
  waited a fixed 8 s and then downloaded whatever thumbnail Blink had; a slow camera had not
  uploaded yet. Now the snap command's outcome is read from Blink and the camera's thumbnail
  stamp is polled until it changes (up to ~1 min); only then is the picture downloaded and
  published. If it never changes, the web UI says so and nothing is published to MQTT.
  A command Blink fails (e.g. camera offline) marks the camera offline.
- Errors of web UI actions (Snapshot, Record) are shown once on the dashboard, not left there.
- **The web UI always said "Refresh in 30 s".** The figure was fixed in the page. It now shows the
  real Blink poll cadence (`poll_interval` while an armed USB/Cloud module needs it,
  `idle_poll_interval` otherwise) and the page reloads at that same pace. The reload reads only
  from the add-on, never from Blink.

### Added
- **Online** connectivity sensor per camera (`blink/cameras/{serial}/online/state`) and per
  sync module (`blink/sync/{nid}/online/state`), retained, auto-discovered in HA.
- **OFFLINE** badges in the web UI on sync modules and cameras, with the reason; battery,
  temperature and WiFi are flagged as stale while offline.

## [1.10.0] - 2026-11-19

### Fixed
- **About 38 MB an hour of Blink traffic at rest (~27 GB a month).** The full poll called
  `blink.refresh(force=True)`, and blinkpy passes `force` on to every camera as
  `force_cache`. Every 30 s it then re-downloaded each camera's thumbnail and its latest
  motion clip, changed or not. The poll now calls a plain `refresh()`, with blinkpy's own
  throttle off (`refresh_rate = 0`), so thumbnails and clips are fetched only when new. The
  images published to MQTT are unchanged: they were already sent only when the thumbnail changed.
- **A failed clip download could publish the previous clip.** blinkpy's cache is now used only if
  the same refresh filled it; otherwise the clip is downloaded on its own, as for older clips.

### Changed
- **The full poll runs every `poll_interval` (30 s) only when it is needed.** That is while an
  **armed** module finds its motion clips through it: Local USB, Cloud, or a standalone
  Mini/doorbell. Otherwise it runs every `idle_poll_interval` (new option, default 300 s) and
  only refreshes status: Live View modules are watched by the light homescreen check,
  and a disarmed module records nothing. Arming from Home Assistant triggers a poll at once,
  and the log notes each switch of cadence.
- **Pictures only on Snapshot.** Home-screen thumbnails are no longer downloaded when Blink
  changes them: the Snapshot button (HA or web UI) takes a picture and downloads just that
  one, Mini cameras included. The new option `auto_thumbnails` (default `false`) brings back the
  old behaviour. Motion videos are not affected: the gate wraps blinkpy's `get_thumbnail` only.
- The web UI Snapshot no longer calls `refresh(force_cache=True)`, which re-downloaded every
  camera's thumbnail and clip, just to update one picture.

## [1.9.2] - 2026-11-19

### Fixed
- **Live never showed a frame through Home Assistant.** HA Ingress (both the
  core proxy and the Supervisor's) keeps only the part of `Content-Type`
  before `;`, so the browser received `multipart/x-mixed-replace` without
  `boundary=ffmpeg` and the `<img>` could not split the frames. The add-on
  side was fine (`Live View <camera>: command …` in the log). The page now
  reads `/live/<camera>` with `fetch()` and splits the MJPEG parts itself
  (each carries a `Content-length`), showing every JPEG in the card. Tested
  behind a proxy that strips the boundary like Ingress: 50 of 50 frames.
- Errors now show the real HTTP status and the add-on's reason
  (`Live failed (409): camera busy…`), read from the response itself; the
  `/live-outcome` endpoint of 1.9.1 is gone. Close aborts the request, which
  stops Live View on the add-on.

## [1.9.1] - 2026-11-19

### Fixed
- **"Camera busy or Live View refused" with nothing in the log.** The page
  showed one generic message for any failure of the Live image, and a camera
  not found (404) or busy (409) was answered without a log line, so the cause
  was invisible. Now every Live request is logged on arrival
  (`Live view <camera>: requested`) and every refusal with its reason; when
  the image fails, the page asks the add-on (`GET /live-outcome/<camera>`) and
  shows the real reason — or says that the request never reached the add-on.

## [1.9.0] - 2026-11-19

### Added
- **Live button on every camera card** in the web UI: shows the camera's
  Live View in the card for **10 s**, then stops by itself (battery) — same
  limit for every camera. A countdown shows the time left; **Close** stops
  it earlier. Served as **MJPEG** (`GET /live/<camera>`,
  `multipart/x-mixed-replace`, 640 px, ~5 fps, no audio) because the HA iOS
  app does not reliably play a progressive MP4 stream; MJPEG works in every
  browser and in the HA apps. ffmpeg's `mpjpeg` muxer writes the stream.
- Live View is stopped when the 10 s are over, when the page or the card is
  closed, and in any case after 35 s (hard timer, also when the viewer's
  connection is abandoned). Log: `Live view <camera>: ended after 10 s
  (stream ended), 450 KB sent`.

### Notes
- One Live View per camera: the live view shares the lock of Live View clip
  recording. A motion clip that fires while someone is watching is skipped
  (`skipped (already recording)`); Live on a camera that is recording shows
  "Camera busy".
- The live view is marked as the add-on's own activity, so the `updated_at`
  motion watch does not take it for motion.
- The page auto-refresh waits while a live view is open.

## [1.8.0] - 2026-11-19

### Added
- **Live View settings tab** in the web UI. Trigger mode, motion polling
  interval and clip length are edited there, apply at once (no restart) and
  are kept in `/data/blink_settings.json`. They touch only sync modules in
  Live View mode; the tab lists which ones.
- **Trigger mode** for the automatic Live View clip: `blink` (watch the
  camera's `updated_at`, as in 1.7.0), `external`, `both` (the first one
  wins, the other is logged as skipped) or `off`. The manual Record clip
  button works in every mode.
- **External motion trigger** `blink/cameras/{serial}/motion/trigger`: starts
  a Live View clip from outside, e.g. an Alexa routine (Blink SmartHome skill
  → motion detected) running a Home Assistant script. Ignored, with the
  reason logged, when the mode does not allow it or the module stores its
  own clips. The tab shows the ready-to-paste HA script for each camera and
  the Alexa setup steps.
- **Clip history** in the settings tab: the last 10 Live View clips with
  source, Blink's motion stamp and the lead over it (negative = started
  before Blink stamped the motion), result and length. In memory only.

### Removed
- Add-on options `live_clip_seconds`, `live_clip_on_motion` and
  `motion_poll_interval`: replaced by the settings tab. Defaults are the same
  (mode `blink`, 5 s, 10 s); a value changed in the old options is not
  carried over.

## [1.7.1] - 2026-11-19

### Fixed
- **Live View clips shorter than requested (4 s instead of 10).** blinkpy's
  `BlinkLiveStream.recv()` (0.25.5, unchanged in 0.25.9) reads each IMMIS
  packet with `read(n)`, which returns fewer bytes when a packet arrives split
  across TCP segments — normal on the internet. blinkpy took that as the end
  of the stream (`Insufficient data for payload: 114 bytes, expected 1316`)
  and closed it, so every clip stopped at a random point. `liveclip._LiveStream`
  overrides only `recv()`, reading with `readexactly()`; blinkpy stays pinned.
  Tested with a server that splits every packet: blinkpy forwarded 0 of
  156 416 bytes, the new reader all of them (12 s of video).

### Changed
- The `Live clip` log line reports the real duration measured with ffprobe:
  `Live clip <camera>: 10.0 s (requested 10), 380 KB`. A clip more than 1 s
  shorter than requested is logged as a warning (`stream ended early`).

## [1.7.0] - 2026-11-19

### Added
- **Automatic Live View clip on motion** for sync modules in Live View mode
  (no USB storage, no subscription). The motion signal is the camera's
  homescreen `updated_at`: checked against the Blink app on 2026-11-19, it
  moved at every motion notification (09:33, 09:34, 09:37) and stayed still
  for 13 minutes without motion. It also moves on arm/disarm and when the
  add-on wakes the camera, so a change counts as motion only if the module is
  in Live View mode, was armed at the previous poll **and** is armed now, and
  the change does not fall within 20 s of our own Live View or snapshot.
  The clip is published on `blink/cameras/{serial}/event/clip` as usual.
  USB modules are untouched.
- **Fast motion watch.** The full poll (`poll_interval`, 30 s) was too slow
  to start a useful clip. While a Live View module is armed, the add-on reads
  only the homescreen every `motion_poll_interval` seconds (default 5) with its
  own GET — blinkpy's `request_homescreen()` is throttled to 5 s and returns
  None when called too soon, which would break `blink.refresh()`. The arm
  state is taken from the same homescreen snapshot when present. `updated_at`
  only counts when it moves forward, so the two readers never double-trigger.
  Latency: up to `motion_poll_interval` + Live View startup.
- **New options** `live_clip_on_motion` (default `true`) and
  `motion_poll_interval` (5–60, default 5).

### Changed
- `live_clip_seconds` default 15 → **10** (less battery).
- The `Device update …` log line now ends with the decision taken
  (`motion, recording` or `ignored (reason)`).

## [1.6.3] - 2026-11-19

### Added
- **Motion probe for sync modules without storage.** For a Sync Module Core
  without subscription Blink lists no motion anywhere (no clip in
  `media/changed`, `/events/network` answers "An app update is required"); the
  only field seen changing at a motion notification is the camera's homescreen
  `updated_at`. Every change is now logged on each poll:
  `Device update <camera>: updated_at <old> -> <new> (armed=…, status=…)`.
  Diagnostic only — nothing is triggered yet. It also changes on our own Live
  View, logged as `Live View <camera>: command …`.

## [1.6.2] - 2026-11-19

### Fixed
- **"Record failed: An app update is required".** Blink no longer accepts the
  Live View endpoints of blinkpy 0.25.5. The request now uses the ones from
  blinkpy 0.25.9: `/api/v6/.../cameras/{id}/liveview` (v2 for Mini and
  doorbell), body `{"intent": "liveview", "motion_event_start_time": null}`.
  blinkpy itself stays pinned at 0.25.5 (auth.py patches its login); its
  `livestream.py` is identical in 0.25.9.

## [1.6.1] - 2026-11-19

### Fixed
- **"Record failed: server".** Live View was opened through blinkpy's
  `cam.init_livestream()`, which in 0.25.5 waits up to 120 s for the Live View
  command to *complete* before returning (the stream is over by then) and turns
  any refusal into `KeyError('server')`, hiding Blink's answer. The add-on now
  sends the Live View request itself (same endpoints as blinkpy) and wraps the
  answer in `BlinkLiveStream` right away. When Blink refuses, its full answer is
  logged (`Live View <camera> refused, Blink answered: …`) and its message is
  shown in the web UI.

## [1.6.0] - 2026-11-19

### Added
- **Live View clip recording.** For a sync module where Blink stores no clip
  (no USB storage, no subscription — e.g. Sync Module Core/1), the add-on can
  record the clip itself: it opens Live View (blinkpy `BlinkLiveStream`),
  ffmpeg remuxes `live_clip_seconds` of it to MP4, and the clip is published
  on `blink/cameras/{serial}/event/clip`, the same topic as USB clips, so
  JARVIS forwards it to Telegram unchanged. Triggers: MQTT
  `blink/cameras/{serial}/record/trigger`, a new HA button **Record clip**, and
  a **Record clip** button on each camera card. One recording per camera at a
  time. New module `liveclip.py`; ffmpeg added to the image.
  Not automatic yet: the motion signal for those modules is still to be found.
- **New option `live_clip_seconds`** (5–60, default 15).
- **Sync module info in the web UI**: recording mode (Local USB / Cloud /
  Live View / Standalone), model, firmware, USB capability. The mode rule
  lives in the new `syncinfo.py`.
- **Diagnostics** per sync module: `GET /debug/sync/{network_id}` returns the
  raw Blink data (homescreen entry and devices, network events, last 15 min of
  clips) to find where motion of a storage-less module shows up.
- The homescreen entry of each sync module is logged once at bridge start.

## [1.5.0] - 2026-11-19

### Changed
- **Web UI groups cameras by sync module.** Each sync module is now its own
  section — name, ARMED/DISARMED badge and Arm/Disarm button in the header, its
  cameras below — replacing the separate "Sync Modules" card and the flat camera
  grid. Mini cameras (their own pseudo-sync in blinkpy) get their own section;
  a camera matching no sync module is listed under "Other". The per-camera
  ARMED badge is gone (it repeated the section's).
- **MQTT discovery:** each camera device now declares `via_device` = its sync
  module, so Home Assistant shows it as "connected via Blink <sync>".

## [1.4.0] - 2026-11-18

### Removed
- **`blink/cameras/{serial}/event/snapshot` is no longer published.** It carried
  the camera's home-screen thumbnail (`cam.image_from_cache`), which a motion
  clip does not refresh, so it showed an older scene rather than the motion.
  A motion event is now only `blink/cameras/{serial}/event/clip`. When the clip
  download fails nothing is published and a warning is logged. The regular
  `blink/cameras/{serial}/snapshot` camera image for Home Assistant is unchanged.

## [1.3.1] - 2026-11-14

### Fixed
- **Motion events were never published.** 1.3.0 detected a new clip from
  `cam.last_record` + `cam.motion_detected`, which blinkpy does not keep in step —
  notably for Mini cameras, which it models as their own sync-less module — so a
  clip found on the USB storage produced no event. Detection now follows
  `cam.recent_clips`, blinkpy's own list of the last hour's clips (the one
  `save_recent_clips()` uses), and each new entry is downloaded from its own URL.
  The log line is now `Motion clip <name> (<time>): <n> KB`; a failed download
  logs the HTTP status or why the body was rejected.

## [1.3.0] - 2026-11-14

### Added
- **Motion events with photo and video.** For every new motion clip the add-on
  publishes `blink/cameras/{serial}/event/snapshot` (JPEG) and then
  `blink/cameras/{serial}/event/clip` (MP4), non-retained, QoS 1. Works with
  Sync Module USB local storage (no subscription): the clip is the one blinkpy
  already downloads during the poll; if that cache is empty it is fetched once,
  and an error body is never published as a video (MP4 `ftyp` check). Clips
  recorded before startup are not re-sent.
- `blink/cameras/{serial}/name` (retained) — camera display name, used by
  consumers to caption media.

## [1.2.0] - 2026-08-09

### Fixed
- **Token refresh at runtime.** About an hour after login, every request started
  failing with `Login endpoint failed. Try again later.` — snapshot and arm
  returned `Internal Server Error` and the poll loop logged `Poll error:` with
  an empty message. Cause: blinkpy renews the expiring access token by itself
  inside `Auth.query()`, and its `refresh_tokens()` goes through the **legacy
  v1 grant** (`client_id = "android"`), while this add-on obtains its tokens
  from the **OAuth v2** PKCE flow (`client_id = "ios"`). A v2 refresh token is
  never valid for the v1 client, so that refresh could not succeed. The add-on
  now patches `Auth.refresh_tokens()` to use the v2 `oauth_refresh_token()`
  endpoint, guarded by a lock so the poll loop and a web command can't consume
  the same (rotated) refresh token twice. The 1.1.0 fix only covered startup,
  which is why the failure came back one hour later.
- **No more HTTP 500 in the web UI.** `/snapshot` and `/arm` catch Blink API
  errors and show a message on the dashboard instead of letting the exception
  reach uvicorn.
- **No more empty log lines.** All error paths log with `repr()`, and the
  add-on's own `TokenRefreshFailed` always carries a message.
- **Rotated refresh tokens are persisted.** Every successful refresh rewrites
  `/data/blink_credentials.json`, so a restart doesn't fall back to a token
  Blink has already replaced.

### Changed
- **Entities go `unavailable` when the session dies.** After 3 consecutive
  authentication failures (≈90 s) the add-on stops the bridge, publishes
  `offline` on `blink/availability` — so the Blink entities in Home Assistant
  become unavailable — and the web UI shows the login form with a clear
  message. A single failure no longer costs the user a new OTP login.
- **blinkpy pinned to 0.25.5.** The runtime patches depend on internals of that
  exact release; an unpinned rebuild could silently stop applying them.

## [1.1.1] - 2026-07-19

### Fixed
- **Add-on repository structure.** Added a root `repository.yaml` and moved the
  add-on into a `blink-mqtt/` subfolder so Home Assistant can discover it when
  the GitHub repo is added as an add-on repository. Previously the add-on files
  lived at the repo root with no `repository.yaml`, so HA showed no add-on to
  install.

## [1.1.0] - 2026-07-19

### Fixed
- **Credential restore on startup.** After refreshing the OAuth v2 token,
  `restore()` now also updates the stored token expiry (`expiration_date` /
  `expires_in`). Previously the stale, already-expired expiry from the saved
  credentials file caused blinkpy's `need_refresh()` to fire a second refresh
  via the legacy password-grant login flow, which fails in the headless add-on
  with `Login endpoint failed. Try again later.` — making even a *valid*
  refresh token fail to restore.
- **Readable restore errors.** An expired/rejected refresh token now logs a
  clear message (`Refresh token expired or rejected — login again via the web
  UI`) instead of an empty `Restore failed:` line; other failures are logged
  with `repr()` so exceptions without a message stay identifiable by type.

## [1.0.0] - initial release

### Added
- MQTT auto-discovery for Blink cameras and sync modules.
- Per-camera motion, armed, battery, temperature, WiFi, and snapshot entities.
- Per-sync-module arm/disarm switch.
- Web UI (HA Ingress) for login, 2FA OTP, and a live dashboard.
- Credential persistence via `/data/blink_credentials.json`.
- Runtime patches for blinkpy 0.25.x (HTTP 202 as 2FA trigger; unsafe
  `CookieJar` for OAuth session cookies).

[1.2.0]: https://github.com/procolo75/blink-mqtt/releases/tag/v1.2.0
[1.1.1]: https://github.com/procolo75/blink-mqtt/releases/tag/v1.1.1
[1.1.0]: https://github.com/procolo75/blink-mqtt/releases/tag/v1.1.0
[1.0.0]: https://github.com/procolo75/blink-mqtt/releases/tag/v1.0.0
