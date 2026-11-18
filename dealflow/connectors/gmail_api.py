"""Gmail through the Gmail API, for accounts signed in with Google. Read-only.

The other way in (gmail.py) is IMAP with an app password. Over IMAP Google
only accepts OAuth with its full-access scope (https://mail.google.com/), so a
read-only sign-in has to use the REST API instead. That is all this file is:
the same mailboxes, the same "Primary tab only" and sent-mail settings, and
the same parsing, threading, de-duplication and Noise filing as the IMAP
connectors (it reuses ImapMail._from_message and _store).

What it asks Google for:

  * the scope https://www.googleapis.com/auth/gmail.readonly, nothing else;
  * messages.list per label (INBOX, SENT, any label named in GMAIL_MAILBOXES),
    then messages.get with format=full: headers and text parts come inline,
    attachments stay on Google's side (only their names are read);
  * on later syncs only mail newer than the last one seen ("after:"), with a
    short overlap so nothing that arrives late is missed. Message-ID
    de-duplication makes the overlap harmless.

Nothing is ever marked as read, labelled, moved or sent: the scope does not
allow it.
"""

from __future__ import annotations

import base64
import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from typing import Any

from ..config import GmailAccount, Settings
from ..db import DB
from ..ingest import remember_account
from ..net import explain_tls_failure, ssl_context
from . import ConnectorError, NotConfigured, google_oauth
from .gmail import Gmail
from .imap import NAME, Progress

API = "https://gmail.googleapis.com/gmail/v1/users/me"
API_LIBRARY_URL = "https://console.cloud.google.com/apis/library/gmail.googleapis.com"
WORKERS = 4  # parallel messages.get calls
# Gmail allows 15,000 quota units per user per minute and messages.get costs 5:
# 20 requests a second is 6,000 a minute, leaving room for other apps that read
# the same mailbox through the same Google Cloud project.
REQUESTS_PER_SECOND = 20.0
ATTEMPTS = 6  # per request, when Google says to slow down
BACKOFF = 2.0  # seconds, doubled per attempt, unless Google sends Retry-After
RATE_REASONS = ("rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED")
OVERLAP = 900  # seconds re-read on each sync, for mail that is stamped a little late
KNOWN_LIMIT = 5000  # message ids remembered as already read, per label
PAGE = 500
TIMEOUT = 30


class _Pacer:
    """Spaces requests out across all worker threads, and pauses them all when Google pushes back."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            start = max(time.monotonic(), self.next_at)
            self.next_at = start + self.interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def hold(self, seconds: float) -> None:
        with self.lock:
            self.next_at = max(self.next_at, time.monotonic() + seconds)


class GmailApi(Gmail):
    progress_name = "Gmail"

    def __init__(self, settings: Settings, db: DB, account: GmailAccount, prefilter: dict[str, Any] | None = None):
        super().__init__(settings, db, account, prefilter)
        self.extra_search = ()  # IMAP's X-GM-RAW; here "category:primary" goes into the search query
        self.refresh_token = account.refresh_token
        self._token_lock = threading.Lock()
        self._pacer = _Pacer(REQUESTS_PER_SECOND)
        self._stop = threading.Event()  # set when one fetch fails for good: the others stop early

    # -- auth and HTTP ------------------------------------------------------------

    def _connect(self):  # pragma: no cover - this connector has no IMAP session
        raise ConnectorError("This Gmail account is signed in with Google and is read through the Gmail API, not IMAP.")

    def _access_token(self, fresh: bool = False) -> str:
        if not self.refresh_token:
            raise NotConfigured(f"{self.display} is not signed in with Google")
        if not self.settings.google_ready:
            raise NotConfigured(
                "Sign in with Google needs the OAuth client ID and secret, which are no longer saved (Settings > Gmail)."
            )
        with self._token_lock:
            if fresh:
                google_oauth.forget(self.refresh_token)
            try:
                return google_oauth.access_token(
                    self.settings.google_client_id, self.settings.google_client_secret, self.refresh_token
                )
            except google_oauth.OAuthError as error:
                raise ConnectorError(f"{self.account}: {error}") from None

    def _get(self, path: str, params: dict[str, Any] | None = None, attempts: int = ATTEMPTS) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
        url = f"{API}/{path}" + (f"?{query}" if query else "")
        renewed = False
        for attempt in range(attempts):
            self._pacer.wait()
            request = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self._access_token(fresh=False)}", "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT, context=ssl_context()) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as http_error:
                retry_after = http_error.headers.get("Retry-After") if http_error.headers else None
                status, reason, message = _api_error(http_error)
                if status == 401 and not renewed:
                    renewed = True
                    self._access_token(fresh=True)  # the access token expired early or was revoked: renew once
                    continue
                if status == 429 or status >= 500 or reason in RATE_REASONS:
                    if attempt + 1 < attempts and not self._stop.is_set():
                        self._pacer.hold(_retry_delay(retry_after, attempt))  # every worker waits, not just this one
                        continue
                raise ConnectorError(_explain(status, reason, message, self.account)) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as net_error:
                tls = explain_tls_failure(net_error)
                if tls:
                    raise ConnectorError(tls) from None
                if attempt + 1 < attempts:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ConnectorError(f"Could not reach Gmail. Check the internet connection. [{net_error}]") from None
        raise ConnectorError("Gmail kept refusing the request. Try again in a few minutes.")

    # -- who and where -----------------------------------------------------------

    def _profile(self) -> dict[str, Any]:
        """Which mailbox the sign-in opens. It must be this account's address."""
        profile = self._get("profile")
        address = str(profile.get("emailAddress") or "").lower()
        if self.account and address and address != self.account:
            raise ConnectorError(
                f"This sign-in opens {address}, but the account is set up as {self.account}. "
                f"Press Sign in with Google and choose {self.account}."
            )
        return profile

    def _labels(self) -> list[dict[str, Any]]:
        return list(self._get("labels").get("labels") or [])

    @staticmethod
    def _resolve(labels: list[dict[str, Any]], name: str) -> str | None:
        """A mailbox name from GMAIL_MAILBOXES as a label id. INBOX and SENT are their own ids."""
        wanted = name.strip().casefold()
        for label in labels:
            if str(label.get("id", "")).casefold() == wanted or str(label.get("name", "")).casefold() == wanted:
                return str(label["id"])
        return None

    def _boxes(self, conn: Any = None) -> list[tuple[str, str]]:
        boxes = [(name, "in") for name in self.mailboxes]
        if self.settings.gmail_sync_sent:
            boxes.append(("SENT", "out"))
        return boxes

    # -- sync ---------------------------------------------------------------------

    def sync(self, progress: Progress | None = None) -> dict[str, Any]:
        say = progress or (lambda _message: None)
        self._profile()
        remember_account(self.db, NAME, self.account, self.account, self.gmail.label)
        labels = self._labels()
        stats: dict[str, Any] = {"fetched": 0, "new_messages": 0, "new_threads": 0, "automated": 0, "mailboxes": {}}
        collected: list[dict[str, Any]] = []
        updates: dict[str, Any] = {}
        unfinished: list[str] = []
        for box, direction in self._boxes():
            label_id = self._resolve(labels, box)
            if label_id is None:
                stats["mailboxes"][box] = "skipped: no label of that name in this account"
                continue
            try:
                items, state, failure = self._read_label(label_id, box, direction, say)
            except ConnectorError as error:
                stats["mailboxes"][box] = f"skipped: {error}"
                continue
            collected.extend(items)
            updates.update(state)
            if failure:
                stats["mailboxes"][box] = f"{len(items)} read; the rest on the next sync ({failure})"
                unfinished.append(box)
            else:
                stats["mailboxes"][box] = len(items)

        stats["fetched"] = len(collected)
        collected.sort(key=lambda item: item["sent_at"])  # oldest first, so replies find what they answer
        for item in collected:
            result = self._store(item)
            stats["new_messages"] += result.new_messages
            stats["new_threads"] += int(result.created)
            stats["automated"] += int(bool(item["automated"]) and result.new_messages > 0)
        # Positions advance only after everything is safely stored.
        for key, value in updates.items():
            self.db.set_state(self.ns, key, value)
        if unfinished:
            stats["unfinished"] = unfinished
        return stats

    def _read_label(self, label_id: str, box: str, direction: str, say: Progress):
        """(items, state to save once they are stored, why it stopped early or "") for one label.

        A label read in full moves its position ("after") to the newest message.
        One cut short (Google kept refusing) keeps its position, remembers which
        messages it did read, and the next sync lists the same span and reads
        only the rest.
        """
        after = int(self.db.get_state(self.ns, f"api_after:{label_id}", "0") or 0)
        partial_newest = int(self.db.get_state(self.ns, f"api_partial_newest:{label_id}", "0") or 0)
        known = list(filter(None, (self.db.get_state(self.ns, f"api_recent:{label_id}", "") or "").split(",")))
        known_set = set(known)
        terms = [f"after:{max(0, after - OVERLAP)}"] if after else [f"newer_than:{max(1, self.lookback_days)}d"]
        if direction == "in" and self.settings.gmail_primary_only:
            terms.append("category:primary")

        ids: list[str] = []
        page_token = ""
        while len(ids) < self.max_messages:
            page = self._get("messages", {
                "labelIds": label_id, "q": " ".join(terms), "maxResults": min(PAGE, self.max_messages - len(ids)),
                "pageToken": page_token,
            })
            ids.extend(str(m["id"]) for m in page.get("messages") or [] if m.get("id"))
            page_token = page.get("nextPageToken") or ""
            if not page_token:
                break
        ids = [message_id for message_id in dict.fromkeys(ids) if message_id not in known_set][: self.max_messages]

        fetched: list[dict[str, Any]] = []
        failure = ""
        self._stop.clear()
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for done, (message, error) in enumerate(pool.map(self._fetch, ids), start=1):
                if message:
                    fetched.append(message)
                if error and not failure:
                    failure = error
                if len(ids) > 20 and (done % 20 == 0 or done == len(ids)):
                    say(f"{self.progress_name}: {box} {done}/{len(ids)}")

        newest = max(after, partial_newest)
        items: list[dict[str, Any]] = []
        for message in fetched:
            newest = max(newest, _internal_seconds(message))
            item = self._item(message, direction)
            if item:
                items.append(item)
        read_now = [str(m["id"]) for m in fetched]
        if failure:
            # Keep the position; remember what was read so the next sync skips it.
            remembered = list(dict.fromkeys(read_now + known))[:KNOWN_LIMIT]
            return items, {
                f"api_recent:{label_id}": ",".join(remembered),
                f"api_partial_newest:{label_id}": newest,
            }, failure
        # Read in full. Messages inside the next overlap window were read now: skip them next time.
        recent = [str(m["id"]) for m in fetched if _internal_seconds(m) >= newest - OVERLAP]
        recent += [message_id for message_id in known if message_id not in recent]
        state: dict[str, Any] = {f"api_recent:{label_id}": ",".join(recent[:KNOWN_LIMIT]), f"api_partial_newest:{label_id}": ""}
        if newest:
            state[f"api_after:{label_id}"] = newest
        return items, state, ""

    def _fetch(self, message_id: str) -> tuple[dict[str, Any] | None, str]:
        """(message, "") or (None, why it could not be read). Never raises, so one failure keeps the rest."""
        if self._stop.is_set():
            return None, "stopped after an earlier error"
        try:
            return self._get(f"messages/{urllib.parse.quote(message_id, safe='')}", {
                "format": "full", "fields": "id,threadId,labelIds,internalDate,payload",
            }), ""
        except ConnectorError as error:
            if "was not found" in str(error):  # deleted between list and get
                return None, ""
            self._stop.set()  # Google keeps refusing: don't burn the remaining attempts on every message
            return None, str(error)

    def _item(self, message: dict[str, Any], direction: str) -> dict[str, Any] | None:
        labels = set(message.get("labelIds") or [])
        if "DRAFT" in labels:
            return None
        msg = mime_from_payload(message.get("payload") or {})
        return self._from_message(msg, "UNREAD" not in labels, direction)

    # -- diagnostics -----------------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """Renew the sign-in and look around, without reading any message."""
        profile = self._profile()
        remember_account(self.db, NAME, self.account, self.account, self.gmail.label)
        labels = self._labels()
        missing = [name for name in self.mailboxes if self._resolve(labels, name) is None]
        inbox = self._get("labels/INBOX")
        return {
            "address": str(profile.get("emailAddress") or self.account).lower(),
            "inbox_messages": int(inbox.get("messagesTotal") or 0),
            "sent_mailbox": "Sent",
            "gmail_extensions": True,
            "missing_mailboxes": missing,
            "mailboxes": sorted(str(label.get("name", "")) for label in labels),
            "via": "google",
        }


def profile_address(access_token: str) -> str:
    """The address a fresh sign-in opens, asked of Gmail itself (the only scope granted is Gmail's)."""
    request = urllib.request.Request(f"{API}/profile", headers={
        "Authorization": f"Bearer {access_token}", "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=ssl_context()) as response:
            address = str(json.loads(response.read().decode("utf-8")).get("emailAddress") or "").lower()
    except urllib.error.HTTPError as http_error:
        status, reason, message = _api_error(http_error)
        raise ConnectorError(_explain(status, reason, message, "this account")) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as net_error:
        raise ConnectorError(explain_tls_failure(net_error) or f"Could not reach Gmail. [{net_error}]") from None
    if "@" not in address:
        raise ConnectorError("Gmail did not say which address this sign-in belongs to.")
    return address


# -- Gmail's JSON message as an email.message.EmailMessage --------------------------


def mime_from_payload(part: dict[str, Any]) -> EmailMessage:
    """Rebuild the MIME tree from a format=full payload, so the IMAP parser can read it unchanged.

    Text parts arrive inline as base64url; they are stored re-encoded as plain
    base64 so get_content() decodes them with their declared charset.
    Attachments arrive without data (an attachmentId): their part keeps its
    headers, so the file name is known, and an empty body.
    """
    msg = EmailMessage()
    for header in part.get("headers") or []:
        name, value = str(header.get("name") or ""), str(header.get("value") or "")
        if not name:
            continue
        if name.lower() == "content-transfer-encoding":
            continue  # replaced below: the data is re-encoded
        try:
            msg[name] = re.sub(r"[\r\n]+\s*", " ", value)
        except Exception:  # noqa: BLE001 - a malformed or repeated unique header: keep the first
            pass
    mime_type = str(part.get("mimeType") or "")
    if mime_type and not msg.get("Content-Type"):
        try:
            msg["Content-Type"] = mime_type
        except Exception:  # noqa: BLE001
            pass
    children = part.get("parts") or []
    if children or mime_type.startswith("multipart/"):
        for child in children:
            msg.attach(mime_from_payload(child))
        return msg
    data = (part.get("body") or {}).get("data")
    if data:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        msg["Content-Transfer-Encoding"] = "base64"
        msg.set_payload(base64.encodebytes(raw).decode("ascii"))
    else:
        msg.set_payload("")
    return msg


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    """How long to pause after Google says to slow down: its Retry-After if given, else exponential with jitter."""
    try:
        return max(0.0, min(120.0, float(retry_after or "")))
    except ValueError:
        return min(64.0, BACKOFF * (2**attempt)) * random.uniform(1.0, 1.25)


def _internal_seconds(message: dict[str, Any]) -> int:
    try:
        return int(message.get("internalDate") or 0) // 1000
    except (TypeError, ValueError):
        return 0


def _api_error(error: urllib.error.HTTPError) -> tuple[int, str, str]:
    with error:
        raw = error.read().decode("utf-8", "replace")
    try:
        body = json.loads(raw).get("error") or {}
    except (ValueError, AttributeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    reasons = [str(item.get("reason") or "") for item in body.get("errors") or [] if isinstance(item, dict)]
    details = [str(item.get("reason") or "") for item in body.get("details") or [] if isinstance(item, dict)]
    reason = next((r for r in reasons + details if r), str(body.get("status") or ""))
    return error.code, reason, str(body.get("message") or "")[:300]


def _explain(status: int, reason: str, message: str, address: str) -> str:
    if reason in ("accessNotConfigured", "SERVICE_DISABLED") or "has not been used in project" in message:
        return (
            "The Gmail API is switched off in the Google Cloud project of your OAuth client. Open "
            f"{API_LIBRARY_URL}, choose that project, press Enable, wait a minute and test again."
        )
    if status == 403 and reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "PERMISSION_DENIED"):
        return (
            f"The Google sign-in for {address} cannot read Gmail. Press Sign in with Google again and leave "
            "“Read your email” ticked."
        )
    if status == 401:
        return f"Google no longer accepts the sign-in for {address}. Press Sign in with Google again."
    if status == 404:
        return "Gmail says that item was not found."
    if status == 400 and reason == "failedPrecondition":
        return f"{address} has no Gmail mailbox (Gmail may be switched off for this Workspace account)."
    return f"Gmail refused the request (HTTP {status}{', ' + reason if reason else ''}). {message}".strip()
