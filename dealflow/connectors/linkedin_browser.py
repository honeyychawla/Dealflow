"""LinkedIn DMs through the owner's own browser. Runs only when they press Sync.

LinkedIn has no inbox API for individuals. This starts a Chromium-family
browser the owner already has, on a profile folder that belongs to Dealflow and
where they signed in to LinkedIn by hand, loads the messaging page as they
would, and reads the conversation data that page itself asks LinkedIn for.
The result goes through the same door as the other LinkedIn collectors
(linkedin.ingest_payload), so repeat syncs never duplicate anything.

What it never does
  * type, click, send, react, accept, or change anything on LinkedIn;
  * see or store the password: the owner types it into LinkedIn's own page;
  * ask LinkedIn for anything itself: it only listens to what the page loads;
  * try to get past a login page, a security check or a CAPTCHA. It stops and
    says "LinkedIn needs sign-in";
  * run on the timer. Only a press of Sync starts it.

Opening a conversation marks it read on LinkedIn (and can show the sender a
read receipt). So by default only conversations the owner has already read are
opened, a few per sync; for unread ones the inbox list already carries the
newest message in full, and that is what gets stored.

LinkedIn's User Agreement prohibits automated access, with no exception for
your own inbox, and an account that trips its checks can be restricted. A
handful of page loads per manual sync, in a normal browser, with a pause
between them, is the least risky shape this can take. Whether to use it is the
owner's decision.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote, unquote, urlsplit

from ..config import Settings
from ..db import DB
from ..util import norm_iso, now_iso
from . import ConnectorError, NotConfigured, cdp, linkedin

NAME = "linkedin"
LINKEDIN = "https://www.linkedin.com"
LIST_PATH = "/messaging/thread/new/"  # the inbox list beside an empty composer: no conversation gets opened
SIGNED_OUT_PATHS = ("/login", "/uas/", "/checkpoint", "/authwall", "/signup", "/m/login")
PAUSE = (2.0, 5.0)  # seconds between page loads: unhurried, like a person reading
LIST_TIMEOUT = 40.0  # how long the inbox list may take to load
THREAD_TIMEOUT = 25.0  # and one conversation
MAX_OPEN = 15
CONVERSATION_RE = re.compile(r"urn:li:msg_conversation:\((urn:li:[A-Za-z_]+:[^,()]+),([^()]+)\)")
THREAD_RE = re.compile(r"urn:li:messagingThread:([^,()\s]+)")

NO_BROWSER = (
    "No Chrome, Chromium, Brave or Edge was found on this computer. Install one, or enter its path under "
    "Settings > LinkedIn > Advanced."
)
NOT_CONNECTED = "LinkedIn is not connected yet. Open Settings > LinkedIn and press Connect LinkedIn."
NEEDS_SIGN_IN = (
    "LinkedIn needs sign-in: it showed its login or security-check page instead of your messages. "
    "Open Settings > LinkedIn and press Sign in again."
)
NOT_RECOGNISED = (
    "LinkedIn's messaging page loaded, but it sent no conversation data in a form Dealflow recognises. "
    "LinkedIn may have changed its site. Nothing was changed; the CSV import under Settings > LinkedIn still works."
)
WINDOW_CLOSED = "The LinkedIn browser window was closed before the sync finished."


class NeedsSignIn(ConnectorError):
    """LinkedIn wants the owner to sign in (or pass a security check) again."""


def base_url() -> str:
    """LinkedIn's address. Tests point this at a stand-in server."""
    return (os.environ.get("DEALFLOW_LINKEDIN_URL") or LINKEDIN).rstrip("/")


def profile_dir(settings: Settings) -> Path:
    return settings.data_dir / "linkedin-browser"


# -- what the Settings page and the Sync button need to know ----------------------


def browser_status(settings: Settings, db: DB) -> dict[str, Any]:
    executable = cdp.find_browser(settings.linkedin_browser_path)
    connected_at = db.get_state(NAME, "browser_connected_at") or ""
    connected = bool(connected_at) and profile_dir(settings).is_dir()
    return {
        "available": bool(executable),
        "browser": cdp.browser_name(executable),
        "connected": connected,
        "connected_at": connected_at if connected else "",
        "needs_sign_in": connected and db.get_state(NAME, "browser_needs_sign_in") == "1",
        "last_sync_at": db.get_state(NAME, "browser_last_sync_at") or "",
    }


def ready(settings: Settings, db: DB) -> bool:
    """True once the owner has connected LinkedIn here and a browser exists to run it."""
    status = browser_status(settings, db)
    return status["available"] and status["connected"]


def disconnect(settings: Settings, db: DB) -> None:
    """Sign out by deleting Dealflow's browser profile. Stored conversations stay."""
    shutil.rmtree(profile_dir(settings), ignore_errors=True)
    for key in ("browser_connected_at", "browser_needs_sign_in", "browser_last_sync_at"):
        db.set_state(NAME, key, "")


# -- reading what the page loaded -------------------------------------------------


def signed_out(url: str) -> bool:
    """True when the browser sits on LinkedIn's login, sign-up or security-check pages."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.netloc != urlsplit(base_url()).netloc:
        return False
    path = parts.path or "/"
    return path == "/" or any(path.startswith(prefix) for prefix in SIGNED_OUT_PATHS)


def _wanted(url: str) -> bool:
    """LinkedIn's own messaging data requests, whatever their exact name this month."""
    return "/voyager/api/" in url and "essag" in url


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("text")
    return value.strip() if isinstance(value, str) else ""


def _refs(holder: dict[str, Any], key: str, index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """A list held either in place or, in LinkedIn's flattened form, as "*key": [urn, ...]."""
    value = holder.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    urns = holder.get("*" + key)
    if isinstance(urns, list):
        return [index[urn] for urn in urns if isinstance(urn, str) and urn in index]
    return []


def _ref(holder: dict[str, Any], key: str, index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    value = holder.get(key)
    if isinstance(value, dict):
        return value
    urn = holder.get("*" + key)
    return index.get(urn, {}) if isinstance(urn, str) else {}


def _person(participant: dict[str, Any]) -> dict[str, str]:
    kind = participant.get("participantType") or {}
    member = kind.get("member")
    if isinstance(member, dict):
        name = " ".join(part for part in (_text(member.get("firstName")), _text(member.get("lastName"))) if part)
        distance = re.search(r"(\d)", str(member.get("distance") or ""))
        degree = {"1": "1st", "2": "2nd", "3": "3rd"}.get(distance.group(1), "") if distance else ""
        return {"name": name, "profile_url": str(member.get("profileUrl") or ""),
                "headline": _text(member.get("headline")), "connection": degree}
    organization = kind.get("organization")
    if isinstance(organization, dict):
        return {"name": _text(organization.get("name")), "profile_url": str(organization.get("pageUrl") or ""),
                "headline": "", "connection": ""}
    custom = kind.get("custom")
    if isinstance(custom, dict):
        return {"name": _text(custom.get("name")), "profile_url": "", "headline": "", "connection": ""}
    return {"name": "", "profile_url": "", "headline": "", "connection": ""}


def _is_me(participant: dict[str, Any], me: str) -> bool:
    if not me:
        return False
    return str(participant.get("entityUrn") or "").endswith(me) or participant.get("hostIdentityUrn") == me


def _attachments(message: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
    """(attachments, extra text) from a message's rendered content."""
    attachments: list[dict[str, str]] = []
    extra = ""
    for item in message.get("renderContent") or []:
        if not isinstance(item, dict):
            continue
        file = item.get("file")
        if isinstance(file, dict):
            attachments.append({"type": "file", "name": str(file.get("name") or "file")})
        if item.get("vectorImage"):
            attachments.append({"type": "image", "name": "image"})
        if item.get("video"):
            attachments.append({"type": "video", "name": "video"})
        if item.get("audio"):
            attachments.append({"type": "audio", "name": "voice message"})
        media = item.get("externalMedia")
        if isinstance(media, dict):
            attachments.append({"type": "gif", "name": str(media.get("title") or "GIF")})
        if item.get("hostUrnData"):
            attachments.append({"type": "post", "name": "shared LinkedIn post"})
        forwarded = item.get("forwardedMessageContent")
        if isinstance(forwarded, dict) and _text(forwarded.get("forwardedBody")):
            extra = "Forwarded message: " + _text(forwarded.get("forwardedBody"))
    return attachments, extra


def parse_captures(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Turn the JSON the page loaded into {thread id: {"me", "raw", "messages": {urn: message}}}.

    LinkedIn sends this either nested (a conversation holds its participants and
    newest messages in place) or flattened (everything in one "included" list,
    joined by "*name" references). Both are read by walking the whole document
    for objects with a conversation or message id, rather than by a fixed path.
    """
    index: dict[str, dict[str, Any]] = {}
    for record in records:
        for obj in _walk(record.get("doc")):
            urn = obj.get("entityUrn")
            if isinstance(urn, str):
                index[urn] = {**index.get(urn, {}), **obj}

    conversations: dict[str, dict[str, Any]] = {}

    def entry_for(urn: str) -> dict[str, Any] | None:
        match = CONVERSATION_RE.search(urn or "")
        if not match:
            return None
        return conversations.setdefault(
            match.group(2), {"thread_id": match.group(2), "me": match.group(1), "raw": {}, "messages": {}}
        )

    def by_thread_id(thread_id: str) -> dict[str, Any] | None:
        return conversations.get(thread_id)

    for record in records:
        # A request for one conversation's messages names that conversation in its address.
        hint = CONVERSATION_RE.search(unquote(str(record.get("url") or "")))
        for obj in _walk(record.get("doc")):
            urn = obj.get("entityUrn")
            if not isinstance(urn, str):
                continue
            if urn.startswith("urn:li:msg_conversation:"):
                entry = entry_for(urn)
                if entry is None:
                    continue
                entry["raw"].update(obj)
                holder = obj.get("messages")
                for message in (_refs(holder, "elements", index) if isinstance(holder, dict) else []):
                    if isinstance(message.get("entityUrn"), str):
                        entry["messages"][message["entityUrn"]] = {**index.get(message["entityUrn"], {}), **message}
            elif urn.startswith("urn:li:msg_message:"):
                nested = obj.get("conversation")
                owner = (
                    obj.get("conversationUrn") or obj.get("*conversation")
                    or (nested.get("entityUrn") if isinstance(nested, dict) else None)
                )
                entry = entry_for(owner) if isinstance(owner, str) else None
                if entry is None:
                    backend = THREAD_RE.search(str(obj.get("backendConversationUrn") or ""))
                    entry = by_thread_id(backend.group(1)) if backend else None
                if entry is None and hint:
                    entry = entry_for(hint.group(0))
                if entry is not None:
                    entry["messages"][urn] = {**index.get(urn, {}), **obj}

    for entry in conversations.values():
        entry["index"] = index
        # Flattened documents keep conversation details only in the index.
        urn = f"urn:li:msg_conversation:({entry['me']},{entry['thread_id']})"
        entry["raw"] = {**index.get(urn, {}), **entry["raw"]}
    return conversations


def _categories(raw: dict[str, Any]) -> list[str]:
    return [str(item).upper() for item in raw.get("categories") or []]


def thread_payload(entry: dict[str, Any]) -> dict[str, Any] | None:
    """One conversation in the shape linkedin.ingest_payload takes. None for ones to leave out."""
    raw, me, index = entry["raw"], entry["me"], entry.get("index") or {}
    categories = _categories(raw)
    if any("ARCHIVE" in item or "SPAM" in item for item in categories):
        return None
    participants = _refs(raw, "conversationParticipants", index)
    others = [p for p in participants if not _is_me(p, me)]
    people = [_person(p) for p in others]
    group = bool(raw.get("groupChat")) or len(others) > 1
    who = people[0] if people else {"name": "", "profile_url": "", "headline": "", "connection": ""}
    title = _text(raw.get("title"))
    if group:
        names = [person["name"] for person in people if person["name"]]
        who = {"name": title or ", ".join(names[:4]) or "Group conversation", "profile_url": "", "headline": "", "connection": ""}

    messages = []
    subject = ""
    for message in sorted(entry["messages"].values(), key=lambda m: m.get("deliveredAt") or 0):
        sender = _ref(message, "sender", index)
        from_me = _is_me(sender, me)
        attachments, extra = _attachments(message)
        text = "\n\n".join(part for part in (_text(message.get("body")), extra) if part)
        if not text and not attachments:
            continue
        subject = _text(message.get("subject")) or subject
        messages.append({
            "from_me": from_me,
            "sent_at": norm_iso(message.get("deliveredAt"), now_iso()),
            "text": text,
            "attachments": attachments,
            "sender_name": "" if from_me else (_person(sender)["name"] or who["name"]),
        })
    if not messages and not who["name"]:
        return None

    thread_id = entry["thread_id"]
    read = raw.get("read")
    return {
        "thread_id": thread_id,
        "url": f"{LINKEDIN}/messaging/thread/{thread_id}/",
        "unread": None if read is None else not read,
        "inmail": any("INMAIL" in item for item in categories),
        "sponsored": any("SPONSORED" in item for item in categories),
        "group": group,
        "request": str(raw.get("state") or "").upper() == "PENDING" or any("REQUEST" in item for item in categories),
        "subject": subject or (title if group else ""),
        "participant": who,
        "messages": messages,
    }


def to_open(conversations: dict[str, dict[str, Any]], db: DB, settings: Settings) -> list[dict[str, Any]]:
    """Which conversations to open for their earlier messages, newest first.

    Opening marks a conversation read on LinkedIn, so unread ones are left
    alone unless the owner asked otherwise. One already read in full, with
    nothing new since, is not opened again. Sponsored messages are never opened.
    """
    limit = max(0, min(MAX_OPEN, settings.linkedin_open_threads))
    chosen: list[dict[str, Any]] = []
    ordered = sorted(conversations.values(), key=lambda e: e["raw"].get("lastActivityAt") or 0, reverse=True)
    for entry in ordered:
        if len(chosen) >= limit:
            break
        raw = entry["raw"]
        categories = _categories(raw)
        if any("SPONSORED" in item or "ARCHIVE" in item or "SPAM" in item for item in categories):
            continue
        if raw.get("read") is not True and not settings.linkedin_open_unread:
            continue
        seen = db.get_state(NAME, f"read:{entry['thread_id']}")
        try:
            if seen not in (None, "") and int(seen) >= int(raw.get("lastActivityAt") or 0):
                continue
        except (TypeError, ValueError):
            pass
        chosen.append(entry)
    return chosen


# -- the browser session ----------------------------------------------------------


class _Capture:
    """Collects the messaging data the page loads from LinkedIn while it renders."""

    def __init__(self, page: cdp.Page):
        self.page = page
        self.pending: dict[str, str] = {}  # request id -> address, for requests still in flight
        self.records: list[dict[str, Any]] = []
        self.last_activity = time.monotonic()

    def pump(self, wait: float = 0.3) -> None:
        for event in self.page.browser.take_events(wait):
            if event.get("sessionId") != self.page.session:
                continue
            method, params = event.get("method"), event.get("params") or {}
            request_id = str(params.get("requestId") or "")
            if method == "Network.requestWillBeSent":
                url = str((params.get("request") or {}).get("url") or "")
                if _wanted(url):
                    self.pending[request_id] = url
                    self.last_activity = time.monotonic()
            elif method == "Network.loadingFinished" and request_id in self.pending:
                url = self.pending.pop(request_id)
                self.last_activity = time.monotonic()
                try:
                    doc = json.loads(self.page.response_body(request_id) or "null")
                except (cdp.BrowserError, ValueError):
                    continue
                if isinstance(doc, (dict, list)):
                    self.records.append({"url": url, "doc": doc})
            elif method == "Network.loadingFailed":
                self.pending.pop(request_id, None)


def _settle(capture: _Capture, page: cdp.Page, need: Callable[[], bool], timeout: float, quiet: float = 1.5) -> bool:
    """Let the page finish loading its messaging data. Stops at once on a login or security-check page."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        capture.pump(0.3)
        if page.browser.closed:
            raise ConnectorError(WINDOW_CLOSED)
        if signed_out(page.url()):
            raise NeedsSignIn(NEEDS_SIGN_IN)
        idle = time.monotonic() - capture.last_activity
        # Quiet for a moment with nothing in flight; or quiet for longer while a request that
        # LinkedIn deliberately keeps open is still pending.
        if idle >= (quiet if not capture.pending else quiet * 3) and need():
            return True
    return need()


def _looks_like_messaging(records: list[dict[str, Any]]) -> bool:
    """True if LinkedIn answered with a conversation list at all, even an empty one."""
    for record in records:
        for obj in _walk(record.get("doc")):
            if any(str(key).startswith("messengerConversations") for key in obj):
                return True
    return False


def collect(settings: Settings, db: DB, progress: Callable[[str], None] | None = None,
            pause: tuple[float, float] | None = None) -> dict[str, Any]:
    """Read the inbox in the browser and store what is new. Raises ConnectorError with a message for the owner."""
    say = progress or (lambda _message: None)
    executable = cdp.find_browser(settings.linkedin_browser_path)
    if not executable:
        raise ConnectorError(NO_BROWSER)
    if not ready(settings, db):
        raise NotConfigured(NOT_CONNECTED)

    say(f"LinkedIn: opening {cdp.browser_name(executable)}")
    visited: list[str] = []  # conversations whose page was loaded, whether or not their messages were recognised
    opened: list[str] = []
    auto_opened = ""
    try:
        with cdp.Browser(executable, profile_dir(settings), headless=not settings.linkedin_browser_visible) as browser:
            page = cdp.Page.first(browser)
            page.call("Network.enable")
            capture = _Capture(page)
            page.navigate(base_url() + LIST_PATH)
            say("LinkedIn: reading the inbox list")
            _settle(capture, page, lambda: _looks_like_messaging(capture.records) or bool(parse_captures(capture.records)),
                    timeout=LIST_TIMEOUT)
            conversations = parse_captures(capture.records)
            if not conversations and not _looks_like_messaging(capture.records):
                raise ConnectorError(NOT_RECOGNISED)
            # The list page is chosen because it opens no conversation. If LinkedIn opened one anyway, say so.
            landed = linkedin.thread_id_from_url(page.url())
            if landed and landed != "new":
                auto_opened = landed

            wanted = to_open(conversations, db, settings)
            for number, entry in enumerate(wanted, start=1):
                time.sleep(random.uniform(*(pause or PAUSE)))
                say(f"LinkedIn: reading conversation {number} of {len(wanted)}")
                thread_id = entry["thread_id"]
                start = len(capture.records)
                page.navigate(f"{base_url()}/messaging/thread/{quote(thread_id, safe='=-_')}/")
                visited.append(thread_id)

                def loaded() -> bool:
                    return any(
                        "messengerMessages" in record["url"] and thread_id in unquote(record["url"])
                        for record in capture.records[start:]
                    )

                if _settle(capture, page, loaded, timeout=THREAD_TIMEOUT):
                    opened.append(thread_id)
            conversations = parse_captures(capture.records)
    except NeedsSignIn:
        db.set_state(NAME, "browser_needs_sign_in", "1")
        raise
    except cdp.BrowserClosed:
        raise ConnectorError(WINDOW_CLOSED) from None
    except cdp.BrowserError as error:
        raise ConnectorError(str(error)) from None

    threads = [payload for payload in (thread_payload(entry) for entry in conversations.values()) if payload]
    # Say what was left out and why, so "the page listed 32, 20 were stored" explains itself.
    archived = sum(
        1 for entry in conversations.values()
        if any("ARCHIVE" in item or "SPAM" in item for item in _categories(entry["raw"]))
    )
    stamp = now_iso()
    stats = linkedin.ingest_payload(db, {
        "collected_at": stamp,
        "request_count": sum(1 for thread in threads if thread["request"]),
        "threads": threads,
    })
    # Remembered for every conversation whose page was loaded, even if its messages were not
    # recognised: a conversation is opened at most once until something new arrives in it.
    for thread_id in visited:
        if thread_id in conversations:
            db.set_state(NAME, f"read:{thread_id}", conversations[thread_id]["raw"].get("lastActivityAt") or 0)
    db.set_state(NAME, "browser_needs_sign_in", "")
    db.set_state(NAME, "browser_last_sync_at", stamp)
    result = {**stats, "listed": len(conversations), "opened": len(opened),
              "left_out_archived_or_spam": archived, "left_out_empty": len(conversations) - len(threads) - archived}
    if auto_opened:
        result["opened_by_linkedin"] = auto_opened
    return result


def sign_in(settings: Settings, db: DB, progress: Callable[[str], None] | None = None,
            timeout: float = 300.0, headless: bool = False) -> dict[str, Any]:
    """Open LinkedIn's own sign-in page in a visible window and wait for the owner to finish.

    The owner types their password into LinkedIn; nothing here reads or stores
    it. The session lives in Dealflow's browser profile, like in any browser.
    """
    say = progress or (lambda _message: None)
    executable = cdp.find_browser(settings.linkedin_browser_path)
    if not executable:
        raise ConnectorError(NO_BROWSER)
    try:
        # Always a visible window for the owner; `headless` exists for the automated tests only.
        with cdp.Browser(executable, profile_dir(settings), headless=headless) as browser:
            page = cdp.Page.first(browser)
            page.navigate(base_url() + "/login")
            say("Sign in to LinkedIn in the window that just opened. It closes by itself once you are in.")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                time.sleep(1.0)
                if browser.closed:
                    raise ConnectorError("The LinkedIn window was closed before sign-in finished.")
                url = page.url()
                on_linkedin = urlsplit(url).netloc == urlsplit(base_url()).netloc and urlsplit(url).scheme in ("http", "https")
                if on_linkedin and not signed_out(url) and "li_at" in page.cookie_names(base_url()):
                    db.set_state(NAME, "browser_connected_at", now_iso())
                    db.set_state(NAME, "browser_needs_sign_in", "")
                    time.sleep(1.5)  # give the browser a moment to write the session to disk
                    return {"connected": True}
    except cdp.BrowserClosed:
        raise ConnectorError("The LinkedIn window was closed before sign-in finished.") from None
    except cdp.BrowserError as error:
        raise ConnectorError(str(error)) from None
    raise ConnectorError("Sign-in was not finished within five minutes. Press the button to try again.")
