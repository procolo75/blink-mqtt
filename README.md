# Blink MQTT — Home Assistant App

A Home Assistant app that bridges **Blink cameras** to **MQTT** with full **Home Assistant auto-discovery**.

Authentication is handled through a built-in web UI (HA Ingress). Once authenticated, the app polls the Blink API periodically and publishes camera state to MQTT, making every camera and sync module available in Home Assistant automatically — no manual entity configuration needed.

---

## Features

- **MQTT auto-discovery** — cameras, sensors, and switches appear in HA automatically
- **Motion detection** binary sensor per camera
- **Armed/Disarmed** state per camera (from the parent sync module)
- **Battery** state sensor per camera (`ok` / `low`)
- **Temperature** sensor per camera (°C)
- **WiFi signal** sensor per camera (dBm)
- **Snapshot** camera entity per camera: the picture taken by the Snapshot button (automatic home-screen thumbnails are off by default to save data, see `auto_thumbnails`)
- **Arm/Disarm** switch per sync module (camera devices are linked to their sync module in HA via `via_device`)
- **Snapshot trigger** command per camera (via MQTT or web UI)
- **Online** connectivity sensor per camera and per sync module (OFF when Blink reports the sync module or camera offline, or a Snapshot/Live View command to it failed)
- **Motion clips over MQTT** — every new motion clip is published as MP4 on `blink/cameras/{serial}/event/clip`, also without a Blink subscription (USB local storage)
- **Live view in the web UI** — a **Live** button on each camera card shows 10 s of Live View (MJPEG, works in the HA iOS/Android apps too), then stops by itself to save battery
- **Live View clip recording** — for sync modules where Blink stores nothing (no USB, no subscription, e.g. Sync Module Core): the app records the clip itself from Live View, manually (HA button, MQTT, web UI) or **automatically on motion**
- **Motion triggers for Live View clips** — Blink polling (`updated_at` watch, every 5 s while armed), an **external trigger** (Alexa routine → HA script → MQTT), both, or off — chosen in the web UI
- **Recording mode per sync module** shown in the web UI: Local USB / Cloud / Live View / Standalone
- **Live View settings tab** — trigger mode, polling interval, clip length (applied at once, no restart), Alexa setup guide with ready-to-paste HA scripts, history of the last 10 Live View clips
- **Credential persistence** — refresh token saved to `/data/blink_credentials.json` and kept up to date on every renewal; no re-login needed on restart until the token expires
- **Web UI** (HA Ingress) for login, 2FA OTP, and dashboard — cameras grouped by sync module, with Arm/Disarm per module
- Supports **BlinkCamera**, **BlinkCameraMini**, and **BlinkDoorbell**

---

## Requirements

- Home Assistant OS or Supervised
- **Mosquitto broker** app (or any MQTT broker reachable from the app)
- A Blink account with at least one sync module and camera

---

## Installation

1. In Home Assistant, go to **Settings → Apps → Install App**
2. Click the three-dot menu (⋮) → **Repositories**
3. Add the repository URL `https://github.com/procolo75/blink-mqtt` and click **Add**
4. Find **Blink MQTT** in the list and click **Install**

---

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `mqtt_host` | string | `core-mosquitto` | MQTT broker hostname |
| `mqtt_port` | int | `1883` | MQTT broker port |
| `mqtt_user` | string | _(empty)_ | MQTT username |
| `mqtt_password` | string | _(empty)_ | MQTT password |
| `poll_interval` | int (10–300) | `30` | Full poll interval in seconds while an **armed** module finds its motion clips through it (Local USB, Cloud, standalone Mini/doorbell) |
| `auto_thumbnails` | bool | `false` | Download each camera's home-screen thumbnail whenever Blink changes it (pre-1.10 behaviour). Off: pictures only on **Snapshot**. Motion videos are not affected either way |
| `idle_poll_interval` | int (30–3600) | `300` | Full poll interval otherwise (Live View modules, everything disarmed): status only — battery, temperature, arm state |

Example `options` in the app UI:

```yaml
mqtt_host: core-mosquitto
mqtt_port: 1883
mqtt_user: homeassistant
mqtt_password: yourpassword
poll_interval: 30
idle_poll_interval: 300
auto_thumbnails: false
```

**Data usage.** The full poll downloads a motion clip only when it is new, and a picture only
when you press **Snapshot** (unless `auto_thumbnails` is on). Until 1.9.x every poll
re-downloaded every camera's thumbnail and latest clip (blinkpy `force_cache`): about **38 MB an
hour at rest**, ~27 GB a month, which matters on a mobile data SIM.

Live View clip settings (trigger mode, motion polling interval, clip length)
are not app options: they are edited in the web UI **Live View settings**
tab, apply at once and are kept in `/data/blink_settings.json`.

A new option shows up in the app UI only after **updating** to a higher version, not after a Rebuild.

---

## How motion clips are obtained — recording modes

Blink only hands out motion video when something stores it. The app
decides, **per sync module**, where the clip of a motion comes from, and shows
it as a chip in the web UI:

| Mode | When | Where the clip comes from | Delay after motion | Cost |
|---|---|---|---|---|
| **Local USB** | Blink reports USB storage `active` on the module (`local_storage_status`) | The module records from the moment of motion; blinkpy lists the clip once recording has finished and the app downloads it | recording length + up to `poll_interval` | none |
| **Cloud** | No USB, but Blink lists cloud clips for its cameras (inferred: there is no direct "subscription" flag) | Blink cloud clip, downloaded like a USB one | as above | Blink plan |
| **Live View** | Neither of the above: Blink stores nothing (e.g. **Sync Module Core/1** without a plan) | The app opens **Live View** and records it to MP4 itself | trigger latency + ~5 s Live View startup | battery: every clip wakes the camera |
| **Standalone** | A Mini camera (blinkpy models it as its own pseudo-sync) | — | — | — |

Whatever the mode, the clip is published on the **same topic**,
`blink/cameras/{serial}/event/clip`, so consumers (e.g. JARVIS → Telegram)
need no change.

> **Note on Cloud.** It is inferred from the clips Blink returned recently
> (`recent_clips`). A module with a plan but no motion in the last hour may
> temporarily show as **Live View**.

> **Important — a Live View clip starts after the motion.** It shows what
> happens *after* the trigger, never the moment itself. Only a module with
> USB storage (or a plan) records the moment of motion.

---

## First-time Setup

1. Start the app and open the **Web UI** (via the Ingress panel)
2. Enter your **Blink account email and password**
3. Blink will send an **SMS code** to your phone — enter it in the OTP form
4. Once authenticated, the app starts polling and publishes all entities to MQTT

On subsequent restarts the saved refresh token is used automatically. No OTP required unless the token expires (typically after several weeks of inactivity).

---

## MQTT Topics

All topics use the `blink` prefix. State topics are **retained** unless noted otherwise.

### Camera topics

| Topic | Payload | Notes |
|---|---|---|
| `blink/cameras/{serial}/motion/state` | `ON` / `OFF` | Motion detected |
| `blink/cameras/{serial}/armed/state` | `ON` / `OFF` | Sync module arm state |
| `blink/cameras/{serial}/battery/state` | `ok` / `low` | Battery status |
| `blink/cameras/{serial}/temperature/state` | float (°C) | Ambient temperature |
| `blink/cameras/{serial}/wifi/state` | int (dBm) | WiFi signal strength |
| `blink/cameras/{serial}/snapshot` | JPEG bytes | Camera image (not retained) |
| `blink/cameras/{serial}/online/state` | `ON` / `OFF` | Camera reachable (retained) |
| `blink/sync/{nid}/online/state` | `ON` / `OFF` | Sync module online per Blink (retained) |
| `blink/cameras/{serial}/name` | string | Camera display name (retained) |

### Motion event topics

Published once per **new motion clip**, in this order, never retained, QoS 1.
Works with Sync Module USB local storage — no Blink subscription needed: blinkpy
lists a clip once its recording has finished and has the sync module upload it
temporarily, so the event arrives a few tens of seconds after the motion
(recording length + poll interval). Clips recorded before the app started
are never re-sent. JARVIS uses this topic to forward the video to Telegram.

| Topic | Payload | Notes |
|---|---|---|
| `blink/cameras/{serial}/event/clip` | MP4 bytes | The recorded clip (~1–3 MB for 10 s); nothing is published if the download failed |

### Camera command topics

| Topic | Payload | Action |
|---|---|---|
| `blink/cameras/{serial}/snapshot/trigger` | any | Trigger a new snapshot |
| `blink/cameras/{serial}/record/trigger` | any | Record a Live View clip (length from the settings tab) and publish it on `event/clip` |
| `blink/cameras/{serial}/motion/trigger` | any | External motion signal (e.g. Alexa → HA script): records a Live View clip if the trigger mode is `external` or `both` and the module is in Live View mode |

#### Live View clips

For a sync module where Blink stores nothing (e.g. Sync Module Core/1 without a
subscription) Live View is the only video Blink gives out. `record/trigger`
opens it, ffmpeg remuxes the configured clip length of it to MP4 and the clip is
published on `blink/cameras/{serial}/event/clip` — the same topic as recorded
motion clips, so consumers (JARVIS → Telegram) need no change. Live View starts
a few seconds after the request, so a clip triggered by motion shows what
happens after it; each recording wakes the camera and costs battery. One
recording per camera at a time: a trigger while recording is ignored.

**Trigger mode** — chosen in the web UI **Live View settings** tab. It
applies only to modules in **Live View** mode; USB and Cloud modules record
their own clips and are never touched.

| Mode | What starts the clip | Pros | Cons |
|---|---|---|---|
| `blink` (default) | The app watches Blink (`updated_at`, see below) every *motion polling interval* while the module is armed | Nothing to set up | ~3600 / interval extra requests to Blink per hour while armed (720 at 5 s); latency up to the interval |
| `external` | Only `blink/cameras/{serial}/motion/trigger` (Alexa routine, HA automation…) | Often faster; no extra polling | Needs the Alexa / HA setup below |
| `both` | Whichever comes first; the other is logged as *skipped (already recording)* | Best latency, fallback if one fails | Both costs |
| `off` | Nothing automatic | — | — |

The manual **Record clip** button and `record/trigger` work in every mode.

Settings in the tab:

| Setting | Range | Default | Notes |
|---|---|---|---|
| Trigger mode | `blink` / `external` / `both` / `off` | `blink` | |
| Motion polling interval | 5–60 s | 5 | Only used by `blink` / `both`, and only while a Live View module is armed |
| Clip length | 5–60 s | 10 | Longer = more battery |

They apply at once (no restart) and are stored in `/data/blink_settings.json`.

**External trigger — Alexa setup.** Blink's own Alexa skill exposes each camera
as a motion sensor, and Alexa often learns about a motion before the app's
polling does. The chain is: *Alexa routine (camera motion) → HA script exposed
to Alexa → MQTT `motion/trigger` → app records*.

1. **Link Blink to Alexa:** Alexa app → More → Skills & Games → *Blink
   SmartHome* → Enable → sign in with the Blink account → Discover devices.
   ⚠ Linking may start a **Blink plan trial** by itself. Check the Blink app
   and cancel it if you don't want it: while a plan is active Blink stores
   cloud clips, the module switches to **Cloud** mode and the app forwards
   those clips instead (up to an hour after cancelling — restart the app to
   switch back at once).
2. **Create one HA script per camera** (Settings → Automations & scenes →
   Scripts → Add → ⋮ → Edit in YAML). The settings tab shows the YAML ready
   to copy for every camera in a Live View module:

   ```yaml
   alias: Blink Front door motion
   mode: single
   sequence:
     - action: mqtt.publish
       data:
         topic: blink/cameras/<serial>/motion/trigger
         payload: "ON"
   ```
3. **Expose the script to Alexa** (Nabu Casa): Settings → Voice assistants →
   Expose → Expose entities → the script → Amazon Alexa. Then Alexa app →
   Devices → + → Discover devices. It appears in Alexa as a **scene**.
4. **Alexa routine:** Alexa app → More → Routines → + → *When this happens*:
   Smart Home → the camera → Motion detected → *Add action*: Smart Home →
   Scenes → the script.
5. Set the trigger mode to `external` or `both`.

An external trigger is ignored — with the reason logged
(`External motion trigger <serial> — ignored (...)`) — when the camera is
unknown, the trigger mode does not allow it, or the module stores its own
clips (USB/Cloud).

**Clip history.** The settings tab lists the last 10 Live View clips: start
time, camera, source (*Blink (polling)*, *Alexa / external*, *Manual*),
Blink's motion stamp, **lead** = clip start − Blink's stamp (negative = the
clip started before Blink stamped the motion, i.e. Alexa was faster), result
(`sent`, `skipped (already recording)`, `no video`, `failed: …`) and real length. Use it to choose
between `blink`, `external` and `both`. Kept in memory, cleared on restart.

**Automatic on motion** (mode `blink` or `both`). Blink lists the
motion of such a module nowhere (no clip, `/events/network` is deprecated), but
it bumps the camera's homescreen `updated_at` at every motion notification. It
also bumps it on arm/disarm and when the app wakes the camera, so a change
counts as motion only if the module is in **Live View** mode, was armed at the
previous poll and still is, and the change is not within 20 s of the app's
own Live View or snapshot. Every change is logged with the decision:
`Device update <camera>: updated_at <old> -> <new> — motion, recording` or
`— ignored (<reason>)`. While such a module is armed, the signal is read every
motion polling interval (settings tab, default 5 s; homescreen only, not the
full poll), so the latency is up to that interval plus the Live View startup. Nothing
extra is polled while no Live View module is armed.

### Sync module topics

| Topic | Payload | Notes |
|---|---|---|
| `blink/sync/{network_id}/armed/state` | `ON` / `OFF` | Current arm state (retained) |
| `blink/sync/{network_id}/armed/set` | `ON` / `OFF` | Arm or disarm the sync module |

### Availability topic

| Topic | Payload |
|---|---|
| `blink/availability` | `online` / `offline` |

`offline` is sent as a Last Will Testament (LWT) so HA marks all entities unavailable if the app stops unexpectedly. It is also published when the app is not authenticated, after a logout, and when the Blink session is lost and a new login is required.

---

## Home Assistant Entities (auto-discovery)

For each camera the following entities are created automatically under a single **Blink {camera name}** device:

| Entity | Type | Device class |
|---|---|---|
| Motion | `binary_sensor` | `motion` |
| Armed | `binary_sensor` | — |
| Battery | `sensor` | — |
| Temperature | `sensor` | `temperature` |
| WiFi | `sensor` | `signal_strength` |
| Snapshot | `camera` | — |
| Record clip | `button` | — |

For each sync module:

| Entity | Type | Notes |
|---|---|---|
| Armed | `switch` | Arm/disarm the entire network |

---

## Camera Types

| Type | Notes |
|---|---|
| **BlinkCamera** | Standard outdoor/indoor camera. Full snapshot control. |
| **BlinkCameraMini** | Compact plug-in camera. Snapshot image updates only when a motion event has occurred; the web UI shows a warning for these cameras. |
| **BlinkDoorbell** | Doorbell camera. Treated like a standard camera. |

---

## Web Dashboard

The built-in dashboard is accessible via **Home Assistant Ingress** (no port forwarding needed).

- **Camera cards** — snapshot thumbnail, motion/armed status badges, battery, temperature, WiFi, serial number
- **One section per sync module** — its cameras, arm state with one-click Arm/Disarm, and chips for:
  - **Recording mode**, decided in this order: **Local USB** (Blink reports USB storage `active`), **Cloud** (no USB, but Blink lists cloud clips for its cameras → subscription; inferred, no direct flag exists), **Live View** (neither: Blink stores nothing, the app records from Live View). Mini cameras are blinkpy pseudo-syncs: **Standalone**.
  - **Model** (Blink's `type`/`subtype` string), **firmware**, **USB yes/no** (does the hardware support local storage).
  - **Diagnostics** link — raw Blink data for that module (homescreen entry, network events, clips of the last 15 min). Open it right after a motion notification.
- **Record clip button** — records a Live View clip and publishes it on `event/clip`
- **Live View settings tab** — trigger mode, motion polling interval, clip length, Alexa setup with per-camera HA scripts, history of the last 10 Live View clips (source, lead over Blink's motion stamp, result, length)
- **Live button** — shows the camera's Live View in place of the thumbnail for **10 s** (every camera, to save battery), with a countdown and a **Close** button. MJPEG, 640 px, ~5 fps, no audio: chosen because it plays everywhere, including the HA iOS app, where a streamed MP4 does not reliably play. The first frame arrives after ~5 s (Live View startup). Not recorded and not published on MQTT — use **Record clip** for that
- **Snapshot button** — triggers a new image capture, waits until Blink lists the new picture (up to ~1 min) and shows it; if no new picture arrives it says so instead of showing the old one
- **Online / OFFLINE badges** — on each camera and sync module; while offline, battery/temperature/WiFi are the last values Blink had
- **Auto-refresh** — the header shows the current Blink poll cadence (`poll_interval` while an armed USB/Cloud module needs it, `idle_poll_interval` otherwise) and the page reloads at that pace; the reload reads from the app only, not from Blink

The **Serial** field on each camera card contains the camera's unique identifier. This is the value to use when configuring MQTT-based integrations such as [DRADIS](https://github.com/procolo75/dradis) (`cameras/{serial}/motion`).

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  blink-mqtt app                                  │
│                                                     │
│  ┌─────────────┐   asyncio    ┌──────────────────┐  │
│  │  AuthManager│ ──────────── │  Bridge           │  │
│  │  (state     │              │  _poll_loop()     │  │
│  │   machine)  │              │  _command_loop()  │  │
│  └─────────────┘              └────────┬─────────┘  │
│                                        │             │
│  ┌─────────────┐               ┌───────▼──────────┐  │
│  │  FastAPI    │               │  MQTTClient      │  │
│  │  Web UI     │ ──publish──►  │  (paho-mqtt)     │  │
│  │  (Ingress)  │               └──────────────────┘  │
│  └─────────────┘                       │             │
└───────────────────────────────────────┼─────────────┘
                                        │
                            ┌───────────▼───────────┐
                            │   MQTT Broker          │
                            │   (Mosquitto)          │
                            └───────────────────────┘
                                        │
                            ┌───────────▼───────────┐
                            │   Home Assistant       │
                            │   (auto-discovery)     │
                            └───────────────────────┘
```

- **AuthManager** — state machine (`NOT_AUTHENTICATED → WAITING_OTP → AUTHENTICATED`, plus `ERROR` when a session is lost); persists credentials to `/data/blink_credentials.json` and owns the token-refresh patch
- **Bridge** — runs concurrent async loops: the full Blink poll (`poll_interval` while an armed USB/Cloud module needs it to find clips, `idle_poll_interval` otherwise; arming from HA triggers a poll at once), the fast motion watch for armed Live View modules (`_motion_loop`), and MQTT command processing
- **liveclip** — opens Live View, records the MP4 with ffmpeg, keeps the per-camera lock, the "own activity" marks (so the app's own Live View/snapshot is not taken for motion) and the clip history
- **syncinfo** — decides each module's recording mode (Local USB / Cloud / Live View / Standalone)
- **settings** — Live View settings edited in the web UI, persisted in `/data/blink_settings.json`
- **MQTTClient** — paho-mqtt wrapper; publishes HA discovery configs and state topics; routes incoming commands to the bridge via an asyncio queue
- **FastAPI web UI** — served on port 8765 via HA Ingress; handles login/OTP/logout, snapshot/arm/record actions, the Live View settings tab and `/debug/sync/{network_id}` diagnostics

---

## Technical Notes

**blinkpy patches** — `blinkpy` is pinned to **0.25.5** because three upstream issues are patched at runtime against that exact release:
- HTTP `202` is now accepted as a 2FA trigger (in addition to `412`) — [blinkpy PR #1231](https://github.com/fronzbot/blinkpy/pull/1231)
- `CookieJar(unsafe=True)` is required to preserve OAuth session cookies — [blinkpy PR #1229](https://github.com/fronzbot/blinkpy/pull/1229)
- `Auth.refresh_tokens()` is redirected to the **OAuth v2** endpoint (see below)

**Token refresh** — The app authenticates through the OAuth v2 PKCE flow (`client_id = "ios"`). blinkpy, however, renews the expiring access token by itself inside `Auth.query()`, and its `refresh_tokens()` uses the **legacy v1 grant** (`client_id = "android"`): a v2 refresh token is never valid for the v1 client, so about an hour after login every request failed with `Login endpoint failed. Try again later.` The app therefore patches `Auth.refresh_tokens()` to call `oauth_refresh_token()` (v2) instead, serialised by a lock — the poll loop and a web command can reach `query()` at the same time, and Blink rotates refresh tokens, so two concurrent refreshes would present an already-consumed token. Every successful refresh rewrites `/data/blink_credentials.json`, keeping the saved token in sync with the rotation.

The same v2 endpoint is used to restore the session on restart (no PKCE flow, no OTP). Refresh tokens last several weeks; when one is genuinely revoked or expired the app stops the bridge, marks the entities unavailable and shows the login form with a clear message — see below.

**Session loss handling** — A single failed refresh can mean either a revoked token or a Blink server hiccup, so the app tolerates **3 consecutive authentication failures** (≈90 s at the default poll interval) before declaring the session dead. Until then it keeps retrying; after that it stops the bridge, publishes `offline` on `blink/availability` and switches the web UI to the login form. Blink API errors in the web UI (snapshot, arm/disarm) are reported as a message on the dashboard — they never surface as an `Internal Server Error`.

**Thumbnail caching** — Snapshot images are only re-published to MQTT when the thumbnail URL changes (or on the first poll), preventing unnecessary MQTT traffic.

**Live View** — The Live View request is sent by the app itself, not through
blinkpy's `cam.init_livestream()`: in 0.25.5 that call waits up to 120 s for
the command to complete and hides Blink's refusal in `KeyError('server')`. The
endpoints are those of blinkpy 0.25.9 (`/api/v6/.../cameras/{id}/liveview`, v2
for Mini and doorbell): the 0.25.5 ones are now refused with *"An app update is
required"*. The stream is read through blinkpy's `BlinkLiveStream` with one
override: `recv()` uses `readexactly()`, because the original treated a packet
split across TCP segments as the end of the stream and cut clips short.
ffmpeg remuxes the MPEG-TS to MP4 without re-encoding; the real length is
measured with ffprobe and logged (`Live clip <camera>: 10.0 s (requested 10),
380 KB`), with a warning when it is more than 1 s short. One recording per
camera at a time.

**Live view (web UI)** — `GET /live/<camera>` opens Live View with the same
code as clip recording and pipes it through ffmpeg's `mpjpeg` muxer
(`multipart/x-mixed-replace`, boundary `ffmpeg`); HA Ingress streams it
unbuffered. It is **not** shown with `<img src>`: Ingress (core and
Supervisor) keeps only the part of `Content-Type` before `;`, so the browser
never learns the boundary. The page reads the response with `fetch()`, splits
the parts by their `Content-length` and shows each JPEG — this works in every
browser and in the HA apps, and lets the page show the real HTTP status and
error text. The request opens Live
View before answering, so a busy camera returns `409` and a Blink refusal
`502` instead of a broken image. It shares the per-camera lock with clip
recording (one Live View per camera at a time) and is marked as the app's
own activity, so the motion watch ignores it. Live View is stopped after
10 s of video, when the viewer closes it, and in any case after 35 s by a
timer that does not depend on the viewer's connection.

**Motion signal for Live View modules** — Blink records nothing for a module
without storage (no clip, `/events/network` is deprecated). The only field
that moves at a motion notification is the camera's homescreen `updated_at`.
The fast watch reads only the homescreen with its own GET (blinkpy's
`request_homescreen()` is throttled to 5 s and would break `blink.refresh()`),
and `updated_at` only counts when it moves forward, so the full poll and the
fast watch never trigger twice.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| A camera or sync module shows **OFFLINE** | Blink reports it offline (homescreen `status`), or the last Snapshot/Live View command to it failed. The log has `Blink status 'online' -> 'offline'` or `command failed (…)`. Detected at the next full poll (30 s for an armed USB/Cloud module, `idle_poll_interval` 300 s otherwise), in about 5 s for an armed Live View module (its motion watch already reads the homescreen), at once when a command fails. It clears the same way, or at the next successful command. |
| Snapshot says "no new picture from the camera within 30 s" | Blink accepted the command but the camera uploaded nothing new (slow wake-up, weak signal; a Mini at rest often keeps its old picture). The old picture is **not** published; press Snapshot again. |
| A camera or a whole sync module is missing from the web UI and HA | The sync module is **off or offline**: Blink does not list its cameras. Power it on; the camera comes back at the next poll. |
| A new app option does not show up | Options appear only after **updating** to a higher version, not after a Rebuild. For a local app: Install App → ⋮ → Check for updates. |
| No automatic Live View clip | Check that the module chip says **Live View**, the module is **armed**, and the trigger mode is not `off`. Every `updated_at` change is logged with the decision (`Device update … — ignored (<reason>)`). |
| A module with a Blink plan shows **Live View** | Cloud is inferred from recent clips; with no motion in the last hour it falls back to Live View. |
| A Live View module suddenly shows **Cloud** | A Blink plan (often a trial started by the Alexa skill) is active. Cancel it in the Blink app if unwanted and restart blink-mqtt. |
| `Record failed: An app update is required` | Blink changed the Live View endpoint again; open an issue with the blink-mqtt log. |
| Live shows "Live failed (409): camera busy" | A Live View clip is being recorded for that camera (or another viewer is watching). Retry after it ends. |
| Live shows "Live failed (502): …" | Blink refused Live View or sent no video (camera offline, sync module off, weak WiFi). The log has `Live view <camera>: refused (…)`. |
| Live shows "Live View ended without video" | Blink opened Live View but sent no picture (camera asleep, weak WiFi). The log has `Live view <camera>: ended after … 0 KB sent`. |
| A motion clip was skipped while watching Live | Expected: one Live View per camera, the clip finds the camera busy (`skipped (already recording)`). |
| `stream ended early` warning | Blink closed Live View before the requested length (weak WiFi, camera busy). The shorter clip is still published. |
| Login form appears after weeks | The refresh token expired or was revoked: log in again with the OTP. |

---

## License

MIT
