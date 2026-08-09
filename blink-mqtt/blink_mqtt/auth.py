import asyncio
import json
import logging
import time
from collections.abc import Callable
from enum import Enum

import aiohttp
from aiohttp import CookieJar

import blinkpy.api as _bapi
from blinkpy.blinkpy import Blink
from blinkpy.auth import (
    Auth,
    BlinkTwoFARequiredError,
    LoginError,
    TokenRefreshFailed,
    UnauthorizedError,
)
from blinkpy.helpers.constants import OAUTH_USER_AGENT, OAUTH_SIGNIN_URL

_LOGGER = logging.getLogger(__name__)
CREDS_FILE = "/data/blink_credentials.json"

# Blink API errors that mean "this session may be gone" — see note_auth_failure().
AUTH_ERRORS = (TokenRefreshFailed, LoginError, UnauthorizedError)

# Consecutive auth failures tolerated before the session is declared dead.
# oauth_refresh_token() returns None for *any* non-200, so a single failure
# can't tell "token revoked" from "Blink server hiccup" — and dropping the
# session means the user has to redo the whole email+password+OTP login.
_MAX_AUTH_FAILURES = 3

# ── Patch blinkpy v0.25.5 bugs (PRs #1229 and #1231 not yet merged) ──────────
#
# Bug 1 (PR #1231): Blink API returns HTTP 202 for 2FA; blinkpy only checks 412.
# Bug 2 (PR #1229): aiohttp default CookieJar drops OAuth session cookies.


async def _oauth_signin_fixed(auth, email, password, csrf_token):
    headers = {
        "User-Agent": OAUTH_USER_AGENT,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://api.oauth.blink.com",
        "Referer": OAUTH_SIGNIN_URL,
    }
    data = {"username": email, "password": password, "csrf-token": csrf_token}
    response = await auth.session.post(
        OAUTH_SIGNIN_URL, headers=headers, data=data, allow_redirects=False
    )
    _LOGGER.debug("oauth_signin status: %s", response.status)
    if response.status in (412, 202):
        return "2FA_REQUIRED"
    if response.status in (301, 302, 303, 307, 308):
        return "SUCCESS"
    return None


_bapi.oauth_signin = _oauth_signin_fixed


def _apply_token_data(auth: Auth, token_data: dict) -> None:
    """Copy an OAuth v2 token response onto an Auth object.

    Mirrors blinkpy's own Auth._process_token_data() but never re-fetches the
    tier info: host/region/account already come from the saved credentials.
    Updating the expiry matters — a stale expiration_date makes need_refresh()
    fire another refresh on the very next request.
    """
    auth.token = token_data.get("access_token") or auth.token
    if new_rt := token_data.get("refresh_token"):
        auth.refresh_token = new_rt
    expires_in = token_data.get("expires_in", 3600)
    auth.expires_in = expires_in
    auth.expiration_date = time.time() + expires_in
    auth.is_errored = False


# Bug 3: blinkpy refreshes expiring tokens itself, inside Auth.query():
#   query() -> need_refresh() -> refresh_tokens(refresh=True) -> login(refresh=True)
# and login() uses the *legacy v1* grant (OAUTH_CLIENT_ID = "android"). Our
# tokens come from the OAuth v2 PKCE flow (OAUTH_V2_CLIENT_ID = "ios"), so that
# refresh is always rejected -> LoginError -> TokenRefreshFailed, roughly an
# hour after login. Route the refresh through the v2 endpoint instead.
# query() is the only caller of refresh_tokens() in the whole library, so this
# single patch covers the bridge, the web UI, snapshots and arm/disarm.

_orig_refresh_tokens = Auth.refresh_tokens


async def _refresh_tokens_v2(self, refresh=False):
    if not (refresh and self.refresh_token and self.hardware_id):
        return await _orig_refresh_tokens(self, refresh=refresh)

    # The poll loop and a web command can hit query() at the same time, and
    # Blink rotates refresh tokens: a second, concurrent refresh would present
    # an already-consumed token and kill the session.
    lock = self.__dict__.setdefault("_bm_refresh_lock", asyncio.Lock())
    async with lock:
        if not self.need_refresh():
            # Another task refreshed while we waited for the lock.
            return True
        token_data = await _bapi.oauth_refresh_token(
            self, self.refresh_token, self.hardware_id
        )
        if not token_data:
            self.is_errored = True
            # Always carry a message: blinkpy raises TokenRefreshFailed bare,
            # which logs as an empty string.
            raise TokenRefreshFailed("refresh token rejected by Blink")
        _apply_token_data(self, token_data)
        _LOGGER.info("Access token refreshed (OAuth v2)")

    # Persist the rotated refresh token outside the lock — the hook does I/O.
    if hook := getattr(self, "_bm_on_refresh", None):
        await hook()
    return True


Auth.refresh_tokens = _refresh_tokens_v2
# ─────────────────────────────────────────────────────────────────────────────


def _new_session() -> aiohttp.ClientSession:
    """aiohttp session with unsafe CookieJar — required for Blink OAuth flow."""
    return aiohttp.ClientSession(cookie_jar=CookieJar(unsafe=True))


def _make_blink(login_data: dict | None = None) -> Blink:
    """Blink instance with a properly-configured session and optional saved auth."""
    session = _new_session()
    blink = Blink(session=session)
    if login_data:
        for attr in (
            "token", "refresh_token", "host", "region_id",
            "client_id", "account_id", "user_id", "hardware_id",
            "expiration_date", "expires_in",
        ):
            if attr in login_data:
                setattr(blink.auth, attr, login_data[attr])
        blink.auth.data = login_data
        blink.auth.no_prompt = True
    return blink


class AuthState(Enum):
    NOT_AUTHENTICATED = "not_authenticated"
    WAITING_OTP = "waiting_otp"
    AUTHENTICATED = "authenticated"
    ERROR = "error"


class AuthManager:
    def __init__(self):
        self.state = AuthState.NOT_AUTHENTICATED
        self.blink: Blink | None = None
        self.error_msg = ""
        self._auth_failures = 0
        # Set by main(): called when the Blink session is declared dead, to mark
        # the MQTT entities unavailable and stop the bridge.
        self.on_session_dead: Callable[[], None] | None = None

    async def restore(self) -> bool:
        """Restore session using the saved refresh_token — no PKCE, no OTP."""
        try:
            with open(CREDS_FILE) as f:
                saved = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _LOGGER.info("No saved credentials found")
            return False

        refresh_token = saved.get("refresh_token")
        hardware_id = saved.get("hardware_id")
        if not refresh_token or not hardware_id:
            _LOGGER.warning("Saved credentials missing refresh_token/hardware_id")
            return False

        blink = _make_blink(login_data=saved)
        try:
            # Use oauth_refresh_token() directly — no fallback to PKCE/OTP.
            # If the refresh token is expired the user must log in again
            # (email+password+OTP), but that will be rare (tokens last weeks).
            token_data = await _bapi.oauth_refresh_token(
                blink.auth, refresh_token, hardware_id
            )
            if not token_data:
                _LOGGER.info("Refresh token rejected — need fresh login")
                await _close(blink)
                return False

            _apply_token_data(blink.auth, token_data)
            blink.setup_urls()
            await blink.get_homescreen()
            ok = await blink.setup_post_verify()
            if ok:
                # Mirror what blink.start() does to avoid last_refresh=None errors
                blink.last_refresh = int(time.time() - blink.refresh_rate * 1.05)
                self._adopt(blink)
                _LOGGER.info("Session restored via refresh_token")
                await self._save(blink)
                return True
            _LOGGER.warning("setup_post_verify() returned False during restore")
            await _close(blink)
            return False
        except (LoginError, TokenRefreshFailed):
            _LOGGER.info(
                "Refresh token expired or rejected — login again via the web UI"
            )
            await _close(blink)
            return False
        except Exception as e:
            # repr() so exceptions without a message (e.g. TokenRefreshFailed)
            # are still identifiable by type instead of logging an empty string.
            _LOGGER.warning("Restore failed: %s", repr(e))
            await _close(blink)
            return False

    async def start_login(self, email: str, password: str) -> bool:
        """Start PKCE headless login (HTTP-only, no browser)."""
        if self.blink:
            await _close(self.blink)
            self.blink = None

        blink = _make_blink()
        blink.auth.data = {"username": email, "password": password}
        blink.auth.no_prompt = True

        try:
            ok = await blink.start()
            if ok:
                self._adopt(blink)
                await self._save(blink)
                return True
            await _close(blink)
            self.error_msg = "Login failed — check email and password"
            self.state = AuthState.ERROR
            return False
        except BlinkTwoFARequiredError:
            self.blink = blink
            self.state = AuthState.WAITING_OTP
            return False
        except (LoginError, TokenRefreshFailed, Exception) as e:
            await _close(blink)
            self.error_msg = str(e)
            self.state = AuthState.ERROR
            return False

    async def complete_otp(self, otp: str) -> bool:
        """Complete 2FA with the SMS code — finishes the PKCE flow."""
        if not self.blink:
            self.state = AuthState.NOT_AUTHENTICATED
            return False
        try:
            ok = await self.blink.send_2fa_code(otp.strip())
            if ok:
                self._adopt(self.blink)
                await self._save()
                return True
            self.error_msg = "2FA verification failed"
            self.state = AuthState.ERROR
            return False
        except Exception as e:
            self.error_msg = str(e)
            self.state = AuthState.ERROR
            return False

    def reset(self):
        self.blink = None
        self.state = AuthState.NOT_AUTHENTICATED
        self.error_msg = ""
        self._auth_failures = 0

    def _adopt(self, blink: Blink):
        """Mark a freshly authenticated Blink instance as the live session."""
        self.blink = blink
        self.state = AuthState.AUTHENTICATED
        self.error_msg = ""
        self._auth_failures = 0
        # Persist the rotated refresh token every time blinkpy refreshes it,
        # otherwise the saved file keeps a token Blink has already replaced.
        blink.auth._bm_on_refresh = self._save

    def note_auth_success(self):
        self._auth_failures = 0
        if self.state is AuthState.AUTHENTICATED:
            # Clear any transient error still shown on the dashboard.
            self.error_msg = ""

    def note_auth_failure(self, exc: Exception) -> bool:
        """Record an auth failure; drop the session after too many in a row.

        Returns True when the session was declared dead (caller should stop
        using self.blink).
        """
        self._auth_failures += 1
        if self._auth_failures < _MAX_AUTH_FAILURES:
            _LOGGER.warning(
                "Auth failure %s/%s: %s",
                self._auth_failures,
                _MAX_AUTH_FAILURES,
                repr(exc),
            )
            return False

        _LOGGER.error("Session lost after %s auth failures: %s",
                      self._auth_failures, repr(exc))
        blink, self.blink = self.blink, None
        self.state = AuthState.ERROR
        self.error_msg = "Session expired — please sign in again"
        if blink:
            asyncio.create_task(_close(blink))
        if self.on_session_dead:
            self.on_session_dead()
        return True

    async def _save(self, blink: Blink | None = None):
        blink = blink or self.blink
        if not blink:
            return
        try:
            with open(CREDS_FILE, "w") as f:
                json.dump(blink.auth.login_attributes, f)
            _LOGGER.debug("Credentials saved to %s", CREDS_FILE)
        except Exception as e:
            _LOGGER.error("Failed to save credentials: %s", e)


async def _close(blink: Blink):
    try:
        await blink.auth.session.close()
    except Exception:
        pass
