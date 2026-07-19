# Changelog

All notable changes to this add-on are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.1.0]: https://github.com/procolo75/blink-mqtt/releases/tag/v1.1.0
[1.0.0]: https://github.com/procolo75/blink-mqtt/releases/tag/v1.0.0
