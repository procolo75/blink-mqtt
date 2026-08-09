# Changelog

All notable changes to this add-on are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
