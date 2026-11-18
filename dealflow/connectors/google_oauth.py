"""Sign in with Google, for reading Gmail. OAuth 2.0 for a local app, read-only scope.

Why this rather than an app password: an app password opens the whole mailbox
(read, delete, send) to whoever holds it, and a Workspace admin can switch app
passwords off. Signing in with Google grants only what the consent screen
shows, here "read your email", can be revoked on its own at
myaccount.google.com/permissions, and leaves no password on disk.

The flow (RFC 8252, loopback redirect, with PKCE):

  1. The owner creates an OAuth client in Google Cloud, type "Desktop app"
     (or "Web application" with the redirect URI below registered), and saves
     its ID and secret under Settings > Gmail.
  2. "Sign in with Google" sends the browser to Google with a one-time state
     and a PKCE challenge (S256).
  3. Google sends the browser back to http://127.0.0.1:<port>/oauth/google/callback,
     where the code is exchanged for a refresh token.
  4. The refresh token goes to .env (mode 600) under that account's slot.
     Access tokens are kept in memory only and renewed when they expire.

Nothing here logs or returns a token, a secret or a code in an error message.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..net import explain_tls_failure, ssl_context
from . import ConnectorError

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALLBACK_PATH = "/oauth/google/callback"
PERMISSIONS_URL = "https://myaccount.google.com/permissions"
PENDING_TTL = 600  # seconds a sign-in may take before its state is forgotten
TIMEOUT = 30


class OAuthError(ConnectorError):
    """Signing in, or renewing a sign-in, failed. The message says what to do."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


# -- step 2 and 3: the browser round trip --------------------------------------

_lock = threading.Lock()
_pending: dict[str, dict[str, Any]] = {}


def begin(client_id: str, port: int, slot: int, login_hint: str = "") -> str:
    """Start a sign-in for one account slot. Returns the Google URL to send the browser to."""
    if not client_id:
        raise OAuthError("Save the Google OAuth client ID and secret first (Settings > Gmail).")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)  # 86 characters, inside PKCE's 43..128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    uri = redirect_uri(port)
    with _lock:
        _prune()
        _pending[state] = {"slot": slot, "verifier": verifier, "redirect_uri": uri, "created": time.monotonic()}
    params = {
        "client_id": client_id,
        "redirect_uri": uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",  # a refresh token, so syncing works without the owner present
        "prompt": "consent",  # Google only returns a refresh token on a fresh consent
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def take(state: str) -> dict[str, Any] | None:
    """The pending sign-in for this state, removed so it can be used once. None if unknown or expired."""
    with _lock:
        _prune()
        return _pending.pop(state, None) if state else None


def _prune() -> None:
    cutoff = time.monotonic() - PENDING_TTL
    for key in [key for key, value in _pending.items() if value["created"] < cutoff]:
        del _pending[key]


def exchange(client_id: str, client_secret: str, code: str, pending: dict[str, Any]) -> dict[str, Any]:
    """Trade the code from Google's redirect for tokens. Checks that Gmail read access was granted."""
    if not code:
        raise OAuthError("Google did not return a sign-in code. Press Sign in with Google again.")
    tokens = _post(TOKEN_URL, {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": pending["redirect_uri"],
        "grant_type": "authorization_code",
        "code_verifier": pending["verifier"],
    })
    granted = set(str(tokens.get("scope") or "").split())
    if SCOPE not in granted:
        # Google's consent screen lets the owner untick a permission.
        raise OAuthError(
            "Google sign-in finished without permission to read Gmail. Press Sign in with Google again and "
            "leave “Read your email” ticked on Google's screen.", "scope_not_granted",
        )
    if not tokens.get("refresh_token") or not tokens.get("access_token"):
        raise OAuthError(
            "Google did not hand out a lasting sign-in. Remove Dealflow's access at "
            f"{PERMISSIONS_URL}, then press Sign in with Google again."
        )
    _remember(tokens["refresh_token"], tokens["access_token"], tokens.get("expires_in"))
    return tokens


# -- step 4: access tokens for syncing -------------------------------------------

_cache: dict[str, tuple[str, float]] = {}  # sha256(refresh token) -> (access token, expires at)


def _cache_key(refresh_token: str) -> str:
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()


def _remember(refresh_token: str, access_token: str, expires_in: Any) -> None:
    try:
        lifetime = float(expires_in)
    except (TypeError, ValueError):
        lifetime = 3000.0
    with _lock:
        _cache[_cache_key(refresh_token)] = (access_token, time.monotonic() + lifetime)


def forget(refresh_token: str) -> None:
    with _lock:
        _cache.pop(_cache_key(refresh_token), None)


def access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """A current access token for this sign-in, renewed if it expires within a minute."""
    with _lock:
        cached = _cache.get(_cache_key(refresh_token))
    if cached and cached[1] - time.monotonic() > 60:
        return cached[0]
    if not (client_id and client_secret):
        raise OAuthError("The Google OAuth client ID and secret are not saved (Settings > Gmail), so this sign-in cannot be renewed.")
    tokens = _post(TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    if not tokens.get("access_token"):
        raise OAuthError("Google did not renew the sign-in. Press Sign in with Google again.")
    _remember(refresh_token, tokens["access_token"], tokens.get("expires_in"))
    return tokens["access_token"]


def revoke(token: str) -> bool:
    """Withdraw a sign-in at Google. Best effort: returns False rather than raising."""
    if not token:
        return False
    forget(token)
    try:
        _post(REVOKE_URL, {"token": token}, timeout=10)
        return True
    except OAuthError:
        return False


# -- HTTP ---------------------------------------------------------------------------


def _post(url: str, form: dict[str, str], timeout: float = TIMEOUT) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(form).encode("ascii"), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            body = response.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as error:
        with error:
            raw = error.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        code = str(data.get("error") or "") if isinstance(data, dict) else ""
        raise OAuthError(_explain(code, error.code), code) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        tls = explain_tls_failure(error)
        raise OAuthError(tls or f"Could not reach Google to sign in. Check the internet connection. [{error}]") from None
    except ValueError:
        raise OAuthError("Google sent back something that is not JSON. Try again in a minute.") from None


def _explain(code: str, status: int) -> str:
    if code == "invalid_grant":
        return (
            "Google sign-in for this account has expired or was withdrawn. Press Sign in with Google again. "
            "If this keeps happening every week, the OAuth consent screen is External and in Testing: "
            "publish it, or make it Internal on a Workspace domain."
        )
    if code in ("invalid_client", "unauthorized_client"):
        return (
            "Google rejected the OAuth client. Check the client ID and secret under Settings > Gmail: they must "
            "be from the same OAuth client, of type Desktop app or Web application."
        )
    if code == "redirect_uri_mismatch":
        return "Google refused the return address. Use a Desktop app client, or register the redirect URI shown in Settings > Gmail."
    if code == "access_denied":
        return "Google sign-in was cancelled."
    if code:
        return f"Google refused the sign-in ({code}, HTTP {status})."
    return f"Google refused the sign-in (HTTP {status})."
