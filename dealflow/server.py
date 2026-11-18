"""Local web server: a JSON API plus the single-page UI. Binds to loopback only.

Because this serves private messages on localhost, every request is checked:
  * the Host header must be this server (blocks DNS-rebinding);
  * anything that changes state needs an X-Dealflow header and a same-origin
    Origin, which a page on another site cannot send (blocks CSRF);
  * a strict Content-Security-Policy means message text can never run as script.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from . import queries, settings_store
from .app import App
from .config import MAX_ACCOUNTS, account_keys, read_env_file
from .connectors import ConnectorError, google_oauth, linkedin
from .connectors.gmail_api import profile_address
from .connectors.instagram import Instagram
from .connectors.protonmail import ProtonMail
from .ingest import merge_contacts, thread_from_payload, upsert_thread
from .triage import engine, jev, writer
from .triage.rubric import TIERS
from .util import dumps, now, now_iso, to_iso

WEB = Path(__file__).resolve().parent / "web"
STATIC = {
    "/": "index.html", "/app.js": "app.js", "/app.css": "app.css",
    "/theme.js": "theme.js", "/favicon.svg": "favicon.svg",
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
MAX_BODY = 20 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def make_handler(app: App):
    allowed_hosts = {f"127.0.0.1:{app.settings.port}", f"localhost:{app.settings.port}"}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "Dealflow"
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:  # keep the terminal quiet
            return

        # -- plumbing -------------------------------------------------------

        def _send(self, status: int, body: bytes, content_type: str, cache: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "Request body too large")
            if length == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "Body must be JSON") from None
            if not isinstance(data, dict):
                raise ApiError(400, "Body must be a JSON object")
            return data

        def _guard(self, mutating: bool) -> None:
            if (self.headers.get("Host") or "") not in allowed_hosts:
                raise ApiError(403, "Unexpected Host header")
            origin = self.headers.get("Origin")
            if origin and origin not in allowed_origins:
                raise ApiError(403, "Cross-origin requests are not allowed")
            if mutating and self.headers.get("X-Dealflow") != "1":
                raise ApiError(403, "Missing X-Dealflow header")

        def _dispatch(self, method: str) -> None:
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"
            try:
                self._guard(mutating=method != "GET")
                if method == "GET" and path in STATIC:
                    return self._static(STATIC[path])
                params = dict(parse_qsl(parts.query))
                if method == "GET" and path == google_oauth.CALLBACK_PATH:
                    # Google sends the browser back here. The one-time state ties it to a
                    # sign-in the owner started from the Settings page.
                    finish_google_sign_in(app, params)
                    return self._redirect("/#/settings/gmail")
                for verb, pattern, handler in ROUTES:
                    match = pattern.fullmatch(path) if verb == method else None
                    if match:
                        body = self._body() if method != "GET" else {}
                        return self._json(handler(app, match, params, body))
                raise ApiError(404, "Not found")
            except ApiError as error:
                self._json({"error": str(error)}, error.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:  # noqa: BLE001 - never leak a stack trace to the page
                self._json({"error": f"{type(error).__name__}: {error}"}, 500)
            finally:
                app.db.close()  # one thread per request: release its connection

        def _static(self, name: str) -> None:
            file = WEB / name
            if not file.is_file():
                raise ApiError(404, "Not found")
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in ("application/javascript", "image/svg+xml"):
                content_type += "; charset=utf-8"
            self._send(200, file.read_bytes(), content_type, cache=True)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch("PATCH")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

    return Handler


# -- handlers -------------------------------------------------------------------


def _thread_or_404(app: App, thread_id: int) -> Any:
    row = app.db.one("SELECT * FROM threads WHERE id=?", (thread_id,))
    if row is None:
        raise ApiError(404, "Thread not found")
    return row


def _event(app: App, thread_id: int, kind: str, detail: dict[str, Any]) -> None:
    app.db.run(
        "INSERT INTO events(thread_id, kind, detail, created_at) VALUES(?,?,?,?)",
        (thread_id, kind, dumps(detail), now_iso()),
    )


def bootstrap(app: App, _m, _p, _b) -> dict[str, Any]:
    return {
        **app.rubric.public(),
        "stages": queries.STAGES,
        "views": [{"id": key, "label": spec["label"]} for key, spec in queries.VIEWS.items()],
        "channels": [
            {"id": "instagram", "label": "Instagram"},
            {"id": "linkedin", "label": "LinkedIn"},
            {"id": "email", "label": "Email"},
        ],
        "stances": [
            {"id": "interested", "label": "Interested"},
            {"id": "more_info", "label": "Ask for details"},
            {"id": "pass", "label": "Polite pass"},
        ],
        "writer": bool(app.settings.writer_model and app.settings.jev_ready),
        # Tells the page this server keeps read/unread state and answers connector filters.
        "read_state": True,
    }


def _gmail_addresses(app: App) -> list[str]:
    return [account.address for account in app.settings.gmail_accounts]


def list_threads(app: App, _m, params, _b) -> dict[str, Any]:
    return queries.list_threads(app.db, app.rubric, params, gmail_accounts=_gmail_addresses(app))


def get_thread(app: App, match, _p, _b) -> dict[str, Any]:
    detail = queries.thread_detail(app.db, app.rubric, int(match.group(1)))
    if detail is None:
        raise ApiError(404, "Thread not found")
    return detail


def _apply_patch(app: App, thread_id: int, body: dict[str, Any]) -> None:
    row = _thread_or_404(app, thread_id)
    with app.db.tx():
        if "stage" in body:
            stage = body["stage"]
            if stage not in queries.STAGE_IDS:
                raise ApiError(400, f"Unknown stage {stage!r}")
            if stage != row["stage"]:
                app.db.run(
                    "UPDATE threads SET stage=?, snoozed_until=NULL, updated_at=? WHERE id=?",
                    (stage, now_iso(), thread_id),
                )
                _event(app, thread_id, "stage", {"from": row["stage"], "to": stage})
        if "starred" in body:
            app.db.run("UPDATE threads SET starred=? WHERE id=?", (int(bool(body["starred"])), thread_id))
        if "unread" in body:
            # Dealflow's own read marker. It is never written back to the mailbox or account.
            app.db.run("UPDATE threads SET unread=? WHERE id=?", (int(bool(body["unread"])), thread_id))
        if "snooze_days" in body:
            days = body["snooze_days"]
            if days in (None, 0):
                app.db.run("UPDATE threads SET snoozed_until=NULL WHERE id=?", (thread_id,))
            else:
                try:
                    until = to_iso(now() + timedelta(days=float(days)))
                except (TypeError, ValueError):
                    raise ApiError(400, "snooze_days must be a number") from None
                app.db.run("UPDATE threads SET snoozed_until=? WHERE id=?", (until, thread_id))
                _event(app, thread_id, "snoozed", {"until": until})
        if "category" in body or "priority" in body:
            if app.db.one("SELECT 1 FROM triage WHERE thread_id=?", (thread_id,)) is None:
                app.db.run(
                    "INSERT INTO triage(thread_id, content_hash, source, triaged_at) VALUES(?,?,?,?)",
                    (thread_id, row["content_hash"], "manual", now_iso()),
                )
        if "category" in body:
            category = body["category"] or None
            if category is not None and category not in app.rubric.by_id:
                raise ApiError(400, f"Unknown category {category!r}")
            app.db.run("UPDATE triage SET category_override=? WHERE thread_id=?", (category, thread_id))
            _event(app, thread_id, "category", {"to": category})
        if "priority" in body:
            priority = body["priority"] or None
            if priority is not None and priority not in TIERS:
                raise ApiError(400, f"Unknown priority {priority!r}")
            app.db.run("UPDATE triage SET priority_override=? WHERE thread_id=?", (priority, thread_id))
            _event(app, thread_id, "priority", {"to": priority})


def patch_thread(app: App, match, _p, body) -> dict[str, Any]:
    thread_id = int(match.group(1))
    _apply_patch(app, thread_id, body)
    return get_thread(app, match, {}, {})


def bulk(app: App, _m, _p, body) -> dict[str, Any]:
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ApiError(400, "ids must be a non-empty list")
    patch = {k: body[k] for k in ("stage", "starred", "unread", "snooze_days", "category", "priority") if k in body}
    if not patch:
        raise ApiError(400, "Nothing to change")
    for thread_id in ids[:500]:
        _apply_patch(app, int(thread_id), patch)
    return {"updated": len(ids[:500]), "counts": queries.view_counts(app.db)}


def add_note(app: App, match, _p, body) -> dict[str, Any]:
    thread_id = int(match.group(1))
    _thread_or_404(app, thread_id)
    text = str(body.get("body") or "").strip()
    if not text:
        raise ApiError(400, "Note is empty")
    app.db.run("INSERT INTO notes(thread_id, body, created_at) VALUES(?,?,?)", (thread_id, text[:5000], now_iso()))
    return get_thread(app, match, {}, {})


def delete_note(app: App, match, _p, _b) -> dict[str, Any]:
    app.db.run("DELETE FROM notes WHERE id=?", (int(match.group(1)),))
    return {"ok": True}


def retriage(app: App, match, _p, _b) -> dict[str, Any]:
    thread_id = int(match.group(1))
    _thread_or_404(app, thread_id)
    app.reload_rubric()
    try:
        engine.triage_thread(app.db, app.settings, app.rubric, thread_id)
    except engine.TriageHalt as halt:
        raise ApiError(502, str(halt)) from None
    except Exception as error:  # noqa: BLE001
        raise ApiError(502, f"Triage failed: {error}") from None
    return get_thread(app, match, {}, {})


def draft(app: App, match, _p, body) -> dict[str, Any]:
    thread_id = int(match.group(1))
    _thread_or_404(app, thread_id)
    try:
        text = engine.draft_for_thread(app.db, app.settings, thread_id, str(body.get("stance") or "more_info"))
    except writer.WriterError as error:
        raise ApiError(502, str(error)) from None
    return {"draft_reply": text}


def start_sync(app: App, _m, _p, body) -> dict[str, Any]:
    target = body.get("connector") or "all"
    if target == "all":
        # Pressed by the owner, so LinkedIn's browser sync joins in if they connected it. The timer never does this.
        started = app.start_job("Syncing all channels", lambda progress: app.sync_all(progress, on_demand=True))
    elif target in ("instagram", "gmail", "email", "linkedin"):
        def work(progress: Callable[[str], None]) -> dict[str, Any]:
            result = {target: app.sync(target, progress)}
            if app.settings.auto_triage:
                result["triage"] = engine.run_pending(app.db, app.settings, app.rubric, progress=progress)
            return result
        names = {"instagram": "Instagram", "gmail": "Gmail", "email": "Proton Mail", "linkedin": "LinkedIn"}
        started = app.start_job(f"Syncing {names[target]}", work)
    else:
        raise ApiError(400, "Unknown source to sync")
    return {"started": started}


def linkedin_browser_sign_in(app: App, _m, _p, _b) -> dict[str, Any]:
    """Open LinkedIn's own sign-in page in the owner's browser and wait for them to finish there."""
    started = app.start_job("Waiting for you to sign in to LinkedIn", lambda progress: app.linkedin_sign_in(progress))
    return {"started": started}


def linkedin_browser_disconnect(app: App, _m, _p, _b) -> dict[str, Any]:
    if app.job["running"]:
        raise ApiError(409, "Something is still running. Try again when it has finished.")
    try:
        app.linkedin_disconnect()
    except ConnectorError as error:
        raise ApiError(409, str(error)) from None
    return _settings_payload(app)


def start_triage(app: App, _m, _p, body) -> dict[str, Any]:
    force = bool(body.get("force"))
    started = app.start_job(
        "Re-sorting everything" if force else "Sorting new messages",
        lambda progress: app.run_triage(force=force, progress=progress),
    )
    return {"started": started}


def status(app: App, _m, _p, _b) -> dict[str, Any]:
    return {
        **app.status(),
        "counts": queries.view_counts(app.db),
        "accounts": app.known_accounts(),
        "nav": queries.nav_counts(app.db, _gmail_addresses(app)),
    }


def ingest(app: App, match, _p, body) -> dict[str, Any]:
    channel = match.group(1)
    if channel == "linkedin":
        stats = linkedin.ingest_payload(app.db, body)
    else:
        if channel not in ("instagram", "email"):
            raise ApiError(400, f"Unknown channel {channel!r}")
        stats = {"threads": 0, "new_messages": 0}
        for item in body.get("threads") or []:
            result = upsert_thread(app.db, thread_from_payload(channel, item))
            stats["threads"] += 1
            stats["new_messages"] += result.new_messages
    app.record_push(channel, stats)
    if app.settings.auto_triage and stats.get("new_messages"):
        app.start_job("Sorting new messages", lambda progress: app.run_triage(progress=progress))
    return stats


def merge(app: App, _m, _p, body) -> dict[str, Any]:
    try:
        merge_contacts(app.db, int(body["keep"]), int(body["drop"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ApiError(400, f"Could not merge: {error}") from None
    return {"ok": True}


def clear_demo(app: App, _m, _p, _b) -> dict[str, Any]:
    from .demo import clear

    return {"removed": clear(app.db)}


def seed_demo(app: App, _m, _p, _b) -> dict[str, Any]:
    from .demo import seed

    stats = seed(app.db)
    app.run_triage()
    return stats


# -- settings ---------------------------------------------------------------------


def _settings_payload(app: App) -> dict[str, Any]:
    result, app.google_result = getattr(app, "google_result", None), None  # shown once, after the redirect
    return {
        **settings_store.describe(app.settings),
        "rubric": settings_store.describe_rubric(app.settings.rubric_path),
        "status": {**app.status(), "accounts": app.known_accounts()},
        "google": {
            "ready": app.settings.google_ready,
            "redirect_uri": google_oauth.redirect_uri(app.settings.port),
            "scope": google_oauth.SCOPE,
            "result": result,
        },
    }


def get_settings(app: App, _m, _p, _b) -> dict[str, Any]:
    return _settings_payload(app)


def save_settings(app: App, _m, _p, body) -> dict[str, Any]:
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        raise ApiError(400, "Nothing to save")
    try:
        clean = settings_store.validate(values)
        stored = read_env_file(app.settings.env_path)
        # A Google sign-in being removed (Remove account) is withdrawn at Google too.
        withdrawn = [
            stored[key] for key, value in clean.items()
            if value is None and stored.get(key)
            and (settings_store.account_field(key) or {}).get("role") == "refresh_token"
        ]
        settings_store.write_env(app.settings.env_path, clean)
    except settings_store.SettingsError as error:
        raise ApiError(400, str(error)) from None
    except OSError as error:
        raise ApiError(500, f"Could not write {app.settings.env_path}: {error}") from None
    restart_needed = app.reload_settings()
    for token in withdrawn:
        threading.Thread(target=google_oauth.revoke, args=(token,), name="google-revoke", daemon=True).start()
    return {**_settings_payload(app), "restart_needed": restart_needed}


# -- Sign in with Google (Gmail, read-only) -------------------------------------------


def _gmail_slots(app: App) -> list[dict[str, Any]]:
    return settings_store.describe(app.settings)["accounts"]["gmail"]["slots"]


def start_google_sign_in(app: App, _m, _p, body) -> dict[str, Any]:
    """Step 2: the Google URL for one account slot. The page sends the browser there."""
    s = app.settings
    if not s.google_ready:
        raise ApiError(400, "Save the Google OAuth client ID and secret first (under “For every Gmail account”).")
    try:
        slot = int(body.get("slot") or 1)
    except (TypeError, ValueError):
        raise ApiError(400, "slot must be a number") from None
    if not 1 <= slot <= MAX_ACCOUNTS:
        raise ApiError(400, f"slot must be between 1 and {MAX_ACCOUNTS}")
    keys = account_keys("gmail", slot)
    if any(settings_store._locked(keys[role]) for role in ("address", "refresh_token", "password")):
        raise ApiError(400, "This account is set by environment variables, so it cannot be changed here.")
    entry = next((e for e in _gmail_slots(app) if e["slot"] == slot), None)
    hint = (entry or {}).get("values", {}).get("address", "")
    return {"url": google_oauth.begin(s.google_client_id, s.port, slot, hint)}


def finish_google_sign_in(app: App, params: dict[str, str]) -> dict[str, Any]:
    """Step 3: Google's redirect. Leaves its outcome for the Settings page to show once."""
    pending = google_oauth.take(params.get("state", ""))
    if pending is None:
        result: dict[str, Any] = {"slot": None, "ok": False, "message": (
            "That sign-in has expired or was already used. Press Sign in with Google again."
        )}
    else:
        result = {"slot": pending["slot"], **_complete_google_sign_in(app, pending, params)}
    app.google_result = result
    return result


def _complete_google_sign_in(app: App, pending: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    s = app.settings
    if params.get("error"):
        if params["error"] == "access_denied":
            return {"ok": False, "message": "Google sign-in was cancelled. Nothing was changed."}
        return {"ok": False, "message": f"Google refused the sign-in ({params['error'][:60]}). Nothing was changed."}
    try:
        tokens = google_oauth.exchange(s.google_client_id, s.google_client_secret, params.get("code", ""), pending)
        address = profile_address(tokens["access_token"])
    except ConnectorError as error:
        return {"ok": False, "message": str(error)}

    slot = pending["slot"]
    slots = _gmail_slots(app)
    entry = next((e for e in slots if e["slot"] == slot), None)
    current = (entry or {}).get("values", {}).get("address", "").lower()
    used_elsewhere = next((e for e in slots if e["slot"] != slot and e["values"].get("address", "").lower() == address), None)
    if used_elsewhere:
        # Same Google account, same OAuth client: one grant. Keep it, the other slot uses it.
        name = used_elsewhere["values"].get("label") or f"account {used_elsewhere['slot']}"
        return {"ok": False, "message": f"{address} is already added as {name}. Each address can only be added once."}
    if current and current != address:
        google_oauth.revoke(tokens["refresh_token"])  # no slot uses this grant
        return {"ok": False, "message": (
            f"You signed in as {address}, but this account is {current}. Sign in with {current}, "
            f"or press Add another account for {address}."
        )}

    keys = account_keys("gmail", slot)
    had_password = bool(entry and entry["secrets"].get("password", {}).get("set"))
    try:
        settings_store.write_env(s.env_path, {
            keys["address"]: address,
            keys["refresh_token"]: tokens["refresh_token"],
            keys["password"]: None,  # not needed any more: one secret fewer on disk
        })
    except OSError as error:
        return {"ok": False, "message": f"Signed in, but could not write {s.env_path}: {error}"}
    app.reload_settings()
    message = f"Signed in with Google as {address}. Dealflow can read this mailbox and nothing else."
    if had_password:
        message += (" The app password for it was removed from .env; delete it at "
                    "myaccount.google.com/apppasswords too.")
    return {"ok": True, "message": message + " Press Sync now to pull messages."}


def save_rubric(app: App, _m, _p, body) -> dict[str, Any]:
    try:
        settings_store.update_rubric(app.settings.rubric_path, body)
        app.reload_rubric()
    except settings_store.SettingsError as error:
        raise ApiError(400, str(error)) from None
    except (OSError, ValueError) as error:
        raise ApiError(500, f"Could not update {app.settings.rubric_path.name}: {error}") from None
    return _settings_payload(app)


def test_connection(app: App, _m, _p, body) -> dict[str, Any]:
    """Try the saved settings for one section and say, in plain words, what happened."""
    target = body.get("target")
    s = app.settings
    try:
        if target == "sorting":
            if not s.jev_ready:
                return {"ok": False, "message": "No OpenRouter API key is saved yet."}
            result = jev.self_test(s.openrouter_api_key, s.jev_model, s.openrouter_base_url)
            cost = result["cost"]
            message = f"{result['model']} answered a test question" + (f" (cost ${cost:.6f})." if isinstance(cost, (int, float)) else ".")
            if s.writer_model:
                try:
                    writer._chat(s, [{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=8)
                    message += f" Writer model {s.writer_model} replied too."
                except writer.WriterError as error:
                    return {"ok": False, "message": f"{message} But the writer model {s.writer_model} failed: {error}"}
            return {"ok": True, "message": message}
        if target == "instagram":
            try:
                slot = int(body.get("slot") or 1)
            except (TypeError, ValueError):
                raise ApiError(400, "slot must be a number") from None
            account = next((a for a in s.instagram_accounts if a.slot == slot), None)
            if account is None:
                return {"ok": False, "message": "No access token is saved for this account yet."}
            info = Instagram(s, app.db, account).check()
            # The same account in two slots would sync twice and fight over one cursor.
            for other in s.instagram_accounts:
                if other.slot != slot and app.db.get_state(other.namespace, "account_id") == info["account_id"]:
                    other_name = other.label or f"account {other.slot}"
                    return {"ok": False, "message": (
                        f"This token works, but it is the same Instagram account (@{info['username']}) as {other_name}. "
                        "Each account can only be added once: remove one of the two."
                    )}
            if info["sample_conversations"] == 0:
                return {"ok": False, "message": (
                    f"Signed in as @{info['username']}, but Instagram returned no conversations. Check that "
                    "'Allow access to messages' is on in the Instagram app. If it is, your Meta app can only see "
                    "messages from people who have a role on it: it needs Advanced Access to the messaging permission."
                )}
            via = (" using Unified Login" if info.get("route") == "unified"
                   else f" through the Facebook Page “{info['page']}”" if info.get("route") == "facebook" and info.get("page") else "")
            return {"ok": True, "message": f"Signed in as @{info['username']}{via}. Conversations are readable."}
        if target == "gmail":
            try:
                slot = int(body.get("slot") or 1)
            except (TypeError, ValueError):
                raise ApiError(400, "slot must be a number") from None
            account = next((a for a in s.gmail_accounts if a.slot == slot), None)
            if account is None:
                return {"ok": False, "message": "Press Sign in with Google, or save the address and an app password first."}
            for other in s.gmail_accounts:
                if other.slot != slot and other.address == account.address:
                    return {"ok": False, "message": (
                        f"{account.address} is already added as {other.label or f'account {other.slot}'}. "
                        "Each address can only be added once: remove one of the two."
                    )}
            info = app.gmail_connector(account).check()
            if info["missing_mailboxes"]:
                return {"ok": False, "message": (
                    f"Signed in to {info['address']}, but these mailboxes do not exist: "
                    f"{', '.join(info['missing_mailboxes'])}. Available: {', '.join(info['mailboxes'][:25])}."
                )}
            count = info["inbox_messages"]
            holds = f"The inbox holds {count:,} message{'' if count == 1 else 's'}."
            if info.get("via") == "google":
                sent = " Sent mail is read too." if s.gmail_sync_sent else ""
                return {"ok": True, "message": f"Signed in with Google to {info['address']}, read-only. {holds}{sent}"}
            sent = f" Sent mail is read from “{info['sent_mailbox']}”." if s.gmail_sync_sent and info["sent_mailbox"] else ""
            return {"ok": True, "message": f"Signed in to {info['address']}. {holds}{sent}"}
        if target == "email":
            if not s.proton_ready:
                return {"ok": False, "message": "Username and Bridge password are not both saved yet."}
            info = ProtonMail(s, app.db).check()
            wanted = [m for m in [*s.proton_mailboxes, s.proton_sent_mailbox] if m]
            missing = [m for m in wanted if m not in info["mailboxes"]]
            if missing:
                return {"ok": False, "message": (
                    f"Connected to Bridge, but these mailboxes do not exist: {', '.join(missing)}. "
                    f"Available: {', '.join(info['mailboxes'][:20])}."
                )}
            return {"ok": True, "message": f"Connected to Bridge with a pinned certificate. Found {', '.join(wanted)}."
                    if info["pinned_cert"] else "Connected to Bridge, but without certificate verification."}
    except (ConnectorError, jev.JevError) as error:
        return {"ok": False, "message": str(error)}
    raise ApiError(400, "Nothing to test for that section")


def linkedin_import(app: App, _m, _p, body) -> dict[str, Any]:
    text = body.get("csv")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "Choose LinkedIn's messages.csv first")
    try:
        since_days = max(1, min(int(body.get("since_days") or 365), 36500))
        stats = app.import_linkedin_export(text, since_days)
    except ValueError as error:
        raise ApiError(400, str(error)) from None
    if app.settings.auto_triage and stats.get("new_messages"):
        app.start_job("Sorting imported conversations", lambda progress: app.run_triage(progress=progress))
    return stats


ROUTES: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = [
    ("GET", re.compile(r"/api/bootstrap"), bootstrap),
    ("GET", re.compile(r"/api/threads"), list_threads),
    ("GET", re.compile(r"/api/threads/(\d+)"), get_thread),
    ("PATCH", re.compile(r"/api/threads/(\d+)"), patch_thread),
    ("POST", re.compile(r"/api/threads/(\d+)/notes"), add_note),
    ("DELETE", re.compile(r"/api/notes/(\d+)"), delete_note),
    ("POST", re.compile(r"/api/threads/(\d+)/retriage"), retriage),
    ("POST", re.compile(r"/api/threads/(\d+)/draft"), draft),
    ("POST", re.compile(r"/api/bulk"), bulk),
    ("POST", re.compile(r"/api/sync"), start_sync),
    ("POST", re.compile(r"/api/triage"), start_triage),
    ("GET", re.compile(r"/api/status"), status),
    ("POST", re.compile(r"/api/ingest/([a-z]+)"), ingest),
    ("POST", re.compile(r"/api/contacts/merge"), merge),
    ("POST", re.compile(r"/api/demo/clear"), clear_demo),
    ("POST", re.compile(r"/api/demo/seed"), seed_demo),
    ("GET", re.compile(r"/api/settings"), get_settings),
    ("POST", re.compile(r"/api/settings"), save_settings),
    ("POST", re.compile(r"/api/settings/rubric"), save_rubric),
    ("POST", re.compile(r"/api/settings/test"), test_connection),
    ("POST", re.compile(r"/api/gmail/google/start"), start_google_sign_in),
    ("POST", re.compile(r"/api/linkedin/import"), linkedin_import),
    ("POST", re.compile(r"/api/linkedin/browser/signin"), linkedin_browser_sign_in),
    ("POST", re.compile(r"/api/linkedin/browser/disconnect"), linkedin_browser_disconnect),
]


def serve(app: App) -> None:
    host = app.settings.host
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(
            f"Refusing to bind to {host}: this app serves private messages with no login. Use 127.0.0.1."
        )
    server = ThreadingHTTPServer((host, app.settings.port), make_handler(app))
    server.daemon_threads = True
    app.start_scheduler()
    print(f"Dealflow Triage is running at http://127.0.0.1:{app.settings.port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        server.server_close()
