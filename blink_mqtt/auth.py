import json
import logging
import time
from enum import Enum

import aiohttp
from aiohttp import CookieJar

import blinkpy.api as _bapi
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth, BlinkTwoFARequiredError, LoginError, TokenRefreshFailed
from blinkpy.helpers.constants import OAUTH_USER_AGENT, OAUTH_SIGNIN_URL

_LOGGER = logging.getLogger(__name__)
CREDS_FILE = "/data/blink_credentials.json"

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

            blink.auth.token = token_data.get("access_token") or blink.auth.token
            if new_rt := token_data.get("refresh_token"):
                blink.auth.refresh_token = new_rt
            blink.setup_urls()
            await blink.get_homescreen()
            ok = await blink.setup_post_verify()
            if ok:
                # Mirror what blink.start() does to avoid last_refresh=None errors
                blink.last_refresh = int(time.time() - blink.refresh_rate * 1.05)
                self.blink = blink
                self.state = AuthState.AUTHENTICATED
                _LOGGER.info("Session restored via refresh_token")
                await self._save()
                return True
            _LOGGER.warning("setup_post_verify() returned False during restore")
            await _close(blink)
            return False
        except Exception as e:
            _LOGGER.warning("Restore failed: %s", e)
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
                self.blink = blink
                self.state = AuthState.AUTHENTICATED
                self.error_msg = ""
                await self._save()
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
                self.state = AuthState.AUTHENTICATED
                self.error_msg = ""
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

    async def _save(self):
        try:
            with open(CREDS_FILE, "w") as f:
                json.dump(self.blink.auth.login_attributes, f)
            _LOGGER.debug("Credentials saved to %s", CREDS_FILE)
        except Exception as e:
            _LOGGER.error("Failed to save credentials: %s", e)


async def _close(blink: Blink):
    try:
        await blink.auth.session.close()
    except Exception:
        pass
