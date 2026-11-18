"""Read-only IMAP sync, shared by every mail connector (Proton Bridge, Gmail).

What every subclass gets:

  * mailboxes are opened with EXAMINE (read-only) and fetched with BODY.PEEK,
    so nothing is ever marked as read and no flag is changed;
  * incremental sync by UID per mailbox, with a rescan if UIDVALIDITY changes;
  * threading by References / In-Reply-To;
  * de-duplication by Message-ID, because one message shows up in several
    mailboxes (Inbox, All Mail, each label) and, with several accounts, in
    more than one of the owner's own inboxes;
  * newsletters and automated mail flagged, so they are filed as Noise without
    spending a Jev call.

A subclass says how to connect and which mailboxes to read.
"""

from __future__ import annotations

import email
import email.policy
import html
import imaplib
import re
from datetime import timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import quote

from ..config import Settings
from ..db import DB
from ..ingest import Message, Participant, Thread, upsert_thread
from ..util import clean_text, now, now_iso, sha, to_iso
from . import ConnectorError

NAME = "email"  # the channel every mail connector files conversations under
PARTIAL_BYTES = 262_144  # read at most the first 256 KB of each message: text, not attachments
BATCH = 20
BODY_LIMIT = 8000

Progress = Callable[[str], None]

UID_RE = re.compile(rb"\bUID (\d+)")
FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
MSGID_RE = re.compile(r"<[^<>\s]+>")
LIST_RE = re.compile(r'^\((?P<attrs>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.*)$')
QUOTE_CUTS = [
    re.compile(r"^\s*On .{4,200}\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*(Original|Forwarded) Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*_{10,}\s*$"),
    re.compile(r"^\s*(From|Von|De):\s.+@.+$", re.IGNORECASE),
    re.compile(r"^\s*Le .{4,200}\ba écrit\s*:\s*$", re.IGNORECASE),
]
MOBILE_SIG_RE = re.compile(r"^\s*(Sent from (my|Proton|Outlook)|Get Outlook for)\b.*$", re.IGNORECASE)


class ImapMail:
    progress_name = "Email"

    def __init__(self, settings: Settings, db: DB, prefilter: dict[str, Any] | None = None):
        self.settings = settings
        self.db = db
        self.prefilter = prefilter or {}
        self.sender_patterns = [p.lower() for p in self.prefilter.get("skip_sender_patterns", [])]
        # What a subclass sets:
        self.ns = NAME  # sync-state namespace; one per account
        self.account = ""  # the owner's address this mailbox belongs to (threads.account)
        self.mailboxes: list[str] = ["INBOX"]
        self.sent_mailbox = ""
        self.lookback_days = 30
        self.max_messages = 1500
        self.extra_search: tuple[str, ...] = ()  # extra IMAP SEARCH keys for inbound mailboxes
        self.owner_emails: list[str] = list(settings.owner_emails)

    # -- for subclasses -----------------------------------------------------

    def _connect(self) -> imaplib.IMAP4:
        raise NotImplementedError

    def _after_login(self, conn: imaplib.IMAP4) -> None:
        """Hook: runs once per sync, right after a successful login."""

    def _boxes(self, conn: imaplib.IMAP4) -> list[tuple[str, str]]:
        """(mailbox, direction) pairs to read: inbound mailboxes, then sent mail."""
        boxes = [(name, "in") for name in self.mailboxes]
        if self.sent_mailbox:
            boxes.append((self.sent_mailbox, "out"))
        return boxes

    # -- sync ---------------------------------------------------------------

    def sync(self, progress: Progress | None = None) -> dict[str, Any]:
        say = progress or (lambda _message: None)
        conn = self._connect()
        stats = {"fetched": 0, "new_messages": 0, "new_threads": 0, "automated": 0, "mailboxes": {}}
        collected: list[dict[str, Any]] = []
        cursors: list[tuple[str, int, int]] = []
        try:
            self._after_login(conn)
            for mailbox, direction in self._boxes(conn):
                try:
                    items, uidvalidity, top_uid = self._read_mailbox(conn, mailbox, direction, say)
                except ConnectorError as error:
                    stats["mailboxes"][mailbox] = f"skipped: {error}"
                    continue
                collected.extend(items)
                stats["mailboxes"][mailbox] = len(items)
                if top_uid:
                    cursors.append((mailbox, uidvalidity, top_uid))
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

        stats["fetched"] = len(collected)
        # Oldest first, so a reply always finds the message it answers already stored.
        collected.sort(key=lambda item: item["sent_at"])
        for item in collected:
            result = self._store(item)
            stats["new_messages"] += result.new_messages
            stats["new_threads"] += int(result.created)
            stats["automated"] += int(bool(item["automated"]) and result.new_messages > 0)
        # Cursors advance only after everything is safely stored.
        for mailbox, uidvalidity, top_uid in cursors:
            self.db.set_state(self.ns, f"uidvalidity:{mailbox}", uidvalidity)
            self.db.set_state(self.ns, f"last_uid:{mailbox}", top_uid)
        return stats

    def _read_mailbox(self, conn: imaplib.IMAP4, mailbox: str, direction: str, say: Progress):
        status, _ = conn.select(_quote(mailbox), readonly=True)
        if status != "OK":
            raise ConnectorError(f"mailbox {mailbox!r} not found (see `./dealflow.sh doctor` for the list)")
        _, validity_data = conn.response("UIDVALIDITY")
        uidvalidity = int(validity_data[0]) if validity_data and validity_data[0] else 0
        stored_validity = int(self.db.get_state(self.ns, f"uidvalidity:{mailbox}", "0") or 0)
        last_uid = int(self.db.get_state(self.ns, f"last_uid:{mailbox}", "0") or 0)
        if stored_validity != uidvalidity:
            last_uid = 0  # the server renumbered this mailbox: rescan (Message-ID dedupe makes that safe)

        if last_uid:
            criteria = ["UID", f"{last_uid + 1}:*"]
        else:
            criteria = ["SINCE", (now() - timedelta(days=self.lookback_days)).strftime("%d-%b-%Y")]
        if direction == "in":
            criteria += list(self.extra_search)  # e.g. Gmail's "Primary tab only"
        status, data = conn.uid("SEARCH", None, *criteria)
        if status != "OK":
            raise ConnectorError(f"search failed in {mailbox!r}")
        # "n:*" always returns the newest message, even when its UID is below n: filter.
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last_uid)
        uids = uids[-self.max_messages:]
        items: list[dict[str, Any]] = []
        for start in range(0, len(uids), BATCH):
            chunk = uids[start:start + BATCH]
            status, fetched = conn.uid(
                "FETCH", ",".join(map(str, chunk)), f"(UID FLAGS BODY.PEEK[]<0.{PARTIAL_BYTES}>)"
            )
            if status != "OK":
                # Fail the mailbox rather than skip the batch: its cursor then stays put
                # and these messages are fetched again next time instead of being lost.
                raise ConnectorError(f"fetch failed in {mailbox!r}")
            for index, part in enumerate(fetched):
                if not isinstance(part, tuple) or len(part) < 2:
                    continue
                # A server may send FLAGS after the message body instead of before it;
                # that tail arrives as the next, non-tuple element.
                tail = fetched[index + 1] if index + 1 < len(fetched) and isinstance(fetched[index + 1], bytes) else b""
                parsed = self._parse(part[0] + b" " + tail, part[1], direction)
                if parsed:
                    items.append(parsed)
            if len(uids) > BATCH:
                say(f"{self.progress_name}: {mailbox} {min(start + BATCH, len(uids))}/{len(uids)}")
        return items, uidvalidity, (uids[-1] if uids else last_uid)

    # -- parsing ------------------------------------------------------------

    def _parse(self, envelope: bytes, raw: bytes, mailbox_direction: str) -> dict[str, Any] | None:
        try:
            msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            return None
        flags = FLAGS_RE.search(envelope or b"")
        return self._from_message(msg, bool(flags and b"\\Seen" in flags.group(1)), mailbox_direction)

    def _from_message(self, msg: EmailMessage, seen: bool, mailbox_direction: str) -> dict[str, Any] | None:
        """One parsed message as the item _store takes. Shared with the Gmail API connector."""
        from_name, from_addr = parseaddr(_header(msg, "From"))
        from_addr = from_addr.lower()
        if not from_addr:
            return None
        direction = mailbox_direction
        if from_addr in self.owner_emails:
            direction = "out"
        recipients = [
            (name, addr.lower())
            for name, addr in getaddresses([_header(msg, "To"), _header(msg, "Cc")])
            if addr and addr.lower() not in self.owner_emails
        ]
        if direction == "out" and not recipients:
            return None  # a note to self

        message_id = (MSGID_RE.search(_header(msg, "Message-ID")) or [None])[0] if _header(msg, "Message-ID") else None
        if not message_id:
            message_id = "<generated-" + sha(from_addr, _header(msg, "Date"), _header(msg, "Subject")) + ">"
        # Bridge appends each message's own <id@protonmail.internalid> to References;
        # left in, every message would look like a reply to itself.
        references = [
            reference
            for reference in MSGID_RE.findall(_header(msg, "References")) + MSGID_RE.findall(_header(msg, "In-Reply-To"))
            if not reference.lower().endswith("@protonmail.internalid>") and reference != message_id
        ]

        try:
            sent_at = to_iso(parsedate_to_datetime(_header(msg, "Date")))
        except (TypeError, ValueError):
            sent_at = now_iso()

        body = strip_quoted(body_text(msg))[:BODY_LIMIT]
        attachments = []
        try:
            for part in msg.iter_attachments():
                filename = part.get_filename()
                if filename:
                    attachments.append({"type": "file", "name": filename})
        except Exception:  # noqa: BLE001 - a truncated MIME tree is expected with partial fetches
            pass

        reason = self._automated_reason(msg, from_addr) if direction == "in" else ""
        return {
            "message_id": message_id, "references": references, "direction": direction,
            "from_name": from_name, "from_addr": from_addr, "recipients": recipients,
            "subject": _header(msg, "Subject").strip(), "sent_at": sent_at, "text": body,
            "attachments": attachments[:8], "seen": seen, "automated": reason,
        }

    def _automated_reason(self, msg: EmailMessage, from_addr: str) -> str:
        if self.prefilter.get("skip_bulk_headers", True):
            if msg.get("List-Unsubscribe") or msg.get("List-Id"):
                return "mailing list"
            if _header(msg, "Precedence").strip().lower() in ("bulk", "list", "junk"):
                return "bulk mail"
            auto = _header(msg, "Auto-Submitted").strip().lower()
            if auto and auto != "no":
                return "auto-generated"
            if msg.get("X-Auto-Response-Suppress") or msg.get("Feedback-ID") and msg.get("X-SES-Outgoing"):
                return "automated sender"
        for pattern in self.sender_patterns:
            if pattern in from_addr:
                return f"sender matches {pattern}"
        return ""

    # -- storing ------------------------------------------------------------

    def _thread_key(self, item: dict[str, Any]) -> str:
        """Join the thread of any message this one references; else start from the root reference."""
        for reference in reversed(item["references"]):
            row = self.db.one(
                "SELECT t.external_id FROM messages m JOIN threads t ON t.id = m.thread_id "
                "WHERE t.channel='email' AND m.external_id=? LIMIT 1",
                (reference,),
            )
            if row is not None:
                return row["external_id"]
        return item["references"][0] if item["references"] else item["message_id"]

    def _store(self, item: dict[str, Any]):
        if item["direction"] == "in":
            name, address = item["from_name"], item["from_addr"]
        else:
            name, address = item["recipients"][0]
        subject = re.sub(r"^\s*((re|fwd?|aw|sv)\s*:\s*)+", "", item["subject"], flags=re.IGNORECASE)
        meta: dict[str, Any] = {}
        if item["direction"] == "in":
            meta["automated"] = bool(item["automated"])
            meta["automated_reason"] = item["automated"]
        thread = Thread(
            channel=NAME,
            external_id=self._thread_key(item),
            participant=Participant(key=address, name=name, handle=address, email=address, url=f"mailto:{address}"),
            messages=[
                Message(
                    external_id=item["message_id"],
                    direction=item["direction"],
                    sent_at=item["sent_at"],
                    text=item["text"],
                    sender_name=item["from_name"],
                    sender_handle=item["from_addr"],
                    attachments=item["attachments"],
                )
            ],
            subject=subject,
            url=self._thread_url(item, address, subject),
            unread=(not item["seen"]) if item["direction"] == "in" else None,
            meta=meta,
            account=self.account,
        )
        return upsert_thread(self.db, thread)

    def _thread_url(self, item: dict[str, Any], address: str, subject: str) -> str:
        """Where "open" takes the owner. Plain IMAP has no web address: start a reply instead."""
        return f"mailto:{address}?subject={quote('Re: ' + subject)}"

    # -- mailboxes ----------------------------------------------------------

    def list_mailboxes(self, conn: imaplib.IMAP4) -> list[tuple[str, set[str]]]:
        """(name, attributes) for every mailbox. Names are returned exactly as SELECT needs them."""
        status, listing = conn.list()
        out: list[tuple[str, set[str]]] = []
        if status != "OK":
            return out
        for line in listing or []:
            if isinstance(line, tuple):  # a name sent as a literal: (b'(...) "/" {n}', b'name')
                head, name = line[0].decode("ascii", "replace"), line[1].decode("ascii", "replace")
            elif isinstance(line, bytes):
                head, name = line.decode("ascii", "replace"), None
            else:
                continue
            match = LIST_RE.match(head)
            if not match:
                continue
            if name is None:
                name = match.group("name").strip()
                if len(name) >= 2 and name[0] == name[-1] == '"':
                    name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            attributes = {attribute.lower() for attribute in match.group("attrs").split()}
            out.append((name, attributes))
        return out

    def special_mailbox(self, conn: imaplib.IMAP4, attribute: str) -> str:
        """The mailbox carrying a special-use attribute such as \\Sent (RFC 6154), or ""."""
        wanted = attribute.lower()
        for name, attributes in self.list_mailboxes(conn):
            if wanted in attributes:
                return name
        return ""




# -- text helpers ---------------------------------------------------------------


def _header(msg: EmailMessage, name: str) -> str:
    try:
        return str(msg.get(name, "") or "")
    except Exception:  # noqa: BLE001 - malformed headers should not sink the message
        return ""


def _text(error: Exception) -> str:
    value = error.args[0] if error.args else error
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _quote(mailbox: str) -> str:
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def body_text(msg: EmailMessage) -> str:
    """Best plain-text rendering of a message: text/plain if present, else stripped HTML."""
    for preference in (("plain",), ("html",)):
        try:
            part = msg.get_body(preferencelist=preference)
        except Exception:  # noqa: BLE001
            part = None
        if part is None:
            continue
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001 - unknown charset or truncated encoding
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
        if not isinstance(content, str):
            continue
        return html_to_text(content) if preference == ("html",) else clean_text(content)
    return ""


def html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|head|title)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?is)<blockquote\b.*?</blockquote>", " ", markup)  # quoted replies
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6])>", "\n", markup)
    markup = re.sub(r"(?s)<[^>]+>", " ", markup)
    return clean_text(html.unescape(markup))


def strip_quoted(text: str) -> str:
    """Keep what the sender wrote now; drop the quoted earlier thread underneath."""
    kept: list[str] = []
    for line in text.split("\n"):
        if any(pattern.match(line) for pattern in QUOTE_CUTS):
            break
        if line.lstrip().startswith(">"):
            continue
        if MOBILE_SIG_RE.match(line):
            continue
        kept.append(line)
    result = clean_text("\n".join(kept))
    return result or clean_text(text)
