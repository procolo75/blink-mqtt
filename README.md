# Blink MQTT — Home Assistant Add-on

A Home Assistant add-on that bridges **Blink cameras** to **MQTT** with full **Home Assistant auto-discovery**.

Authentication is handled through a built-in web UI (HA Ingress). Once authenticated, the add-on polls the Blink API periodically and publishes camera state to MQTT, making every camera and sync module available in Home Assistant automatically — no manual entity configuration needed.

---

## Features

- **MQTT auto-discovery** — cameras, sensors, and switches appear in HA automatically
- **Motion detection** binary sensor per camera
- **Armed/Disarmed** state per camera (from the parent sync module)
- **Battery** state sensor per camera (`ok` / `low`)
- **Temperature** sensor per camera (°C)
- **WiFi signal** sensor per camera (dBm)
- **Snapshot** camera entity per camera (live JPEG image)
- **Arm/Disarm** switch per sync module
- **Snapshot trigger** command per camera (via MQTT or web UI)
- **Credential persistence** — refresh token saved to `/data/blink_credentials.json`; no re-login needed on restart until the token expires
- **Web UI** (HA Ingress) for login, 2FA OTP, and dashboard
- Supports **BlinkCamera**, **BlinkCameraMini**, and **BlinkDoorbell**

---

## Requirements

- Home Assistant OS or Supervised
- **Mosquitto broker** add-on (or any MQTT broker reachable from the add-on)
- A Blink account with at least one sync module and camera

---

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu (⋮) → **Repositories**
3. Add this repository URL and click **Add**
4. Find **Blink MQTT** in the list and click **Install**

---

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `mqtt_host` | string | `core-mosquitto` | MQTT broker hostname |
| `mqtt_port` | int | `1883` | MQTT broker port |
| `mqtt_user` | string | _(empty)_ | MQTT username |
| `mqtt_password` | string | _(empty)_ | MQTT password |
| `poll_interval` | int (10–300) | `30` | Polling interval in seconds |

Example `options` in the add-on UI:

```yaml
mqtt_host: core-mosquitto
mqtt_port: 1883
mqtt_user: homeassistant
mqtt_password: yourpassword
poll_interval: 30
```

---

## First-time Setup

1. Start the add-on and open the **Web UI** (via the Ingress panel)
2. Enter your **Blink account email and password**
3. Blink will send an **SMS code** to your phone — enter it in the OTP form
4. Once authenticated, the add-on starts polling and publishes all entities to MQTT

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

### Camera command topics

| Topic | Payload | Action |
|---|---|---|
| `blink/cameras/{serial}/snapshot/trigger` | any | Trigger a new snapshot |

### Sync module topics

| Topic | Payload | Notes |
|---|---|---|
| `blink/sync/{network_id}/armed/state` | `ON` / `OFF` | Current arm state (retained) |
| `blink/sync/{network_id}/armed/set` | `ON` / `OFF` | Arm or disarm the sync module |

### Availability topic

| Topic | Payload |
|---|---|
| `blink/availability` | `online` / `offline` |

`offline` is sent as a Last Will Testament (LWT) so HA marks all entities unavailable if the add-on stops unexpectedly.

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
- **Sync module cards** — current arm state with one-click Arm/Disarm button
- **Snapshot button** — triggers a new image capture and refreshes the thumbnail
- **Auto-refresh** — page reloads automatically every `poll_interval` seconds when authenticated

The **Serial** field on each camera card contains the camera's unique identifier. This is the value to use when configuring MQTT-based integrations such as [DRADIS](https://github.com/procolo75/dradis) (`cameras/{serial}/motion`).

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  blink-mqtt add-on                                  │
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

- **AuthManager** — state machine (`NOT_AUTHENTICATED → WAITING_OTP → AUTHENTICATED`); persists credentials to `/data/blink_credentials.json`
- **Bridge** — runs two concurrent async loops: polling Blink API and processing MQTT commands
- **MQTTClient** — paho-mqtt wrapper; publishes HA discovery configs and state topics; routes incoming commands to the bridge via an asyncio queue
- **FastAPI web UI** — served on port 8765 via HA Ingress; handles login/OTP/logout and exposes snapshot/arm actions

---

## Technical Notes

**blinkpy 0.25.x patches** — Two upstream bugs are patched at runtime:
- HTTP `202` is now accepted as a 2FA trigger (in addition to `412`) — [blinkpy PR #1231](https://github.com/fronzbot/blinkpy/pull/1231)
- `CookieJar(unsafe=True)` is required to preserve OAuth session cookies — [blinkpy PR #1229](https://github.com/fronzbot/blinkpy/pull/1229)

**Token refresh** — On restart, the add-on calls `oauth_refresh_token()` directly with the saved `refresh_token` and `hardware_id` (no PKCE flow, no OTP). It also refreshes the stored token expiry (`expiration_date` / `expires_in`) so blinkpy's `need_refresh()` doesn't fall back to the legacy password-grant login flow — which would otherwise fail in the headless add-on with `Login endpoint failed. Try again later.` The token is valid for several weeks; when it genuinely expires the add-on logs a clear message and a fresh login is required via the web UI.

**Thumbnail caching** — Snapshot images are only re-published to MQTT when the thumbnail URL changes (or on the first poll), preventing unnecessary MQTT traffic.

---

## License

MIT
