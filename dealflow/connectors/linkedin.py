"""LinkedIn DMs. There is no personal inbox API, so two doors lead in here:

  1. A computer-use agent (Claude driving the owner's own logged-in Chrome)
     reads linkedin.com/messaging and hands over JSON. See
     collectors/linkedin-sync.md for the routine and the payload format.
  2. The official data export ("Get a copy of your data" -> messages.csv),
     a one-time backfill with full history and exact timestamps.

Both lack stable message ids, and the UI only shows minute-level, relative
times ("2:15 PM", "Monday", "Nov 3"). So messages are matched on content:
an incoming message equals a stored one when direction and normalised text
match and the times are within 36 hours. That makes re-reads idempotent and
lets a CSV backfill and later agent reads land on the same rows.
"""

from __future__ import annotations

import csv
import html
import io
import re
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import DB
from ..ingest import Message, Participant, Thread, upsert_thread
from ..util import clean_text, loads, norm_iso, norm_name, now, now_iso, parse_iso, sha, to_iso

NAME = "linkedin"
MATCH_WINDOW = timedelta(hours=36)
TAG_RE = re.compile(r"<[^>]+>")
SLUG_RE = re.compile(r"linkedin\.com/(?:in|company|school)/([^/?#\s]+)", re.IGNORECASE)
THREAD_RE = re.compile(r"/messaging/thread/([^/?#\s]+)", re.IGNORECASE)


def profile_slug(url: str | None) -> str:
    match = SLUG_RE.search(url or "")
    return match.group(1).strip().lower() if match else ""


def thread_id_from_url(url: str | None) -> str:
    match = THREAD_RE.search(url or "")
    return match.group(1) if match else ""


def participant_key(profile_url: str, name: str) -> str:
    return profile_slug(profile_url) or ("name:" + norm_name(name) if name else "")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", clean_text(text)).strip().lower()


# -- thread lookup ------------------------------------------------------------


def _canonical_external_id(db: DB, external_id: str, key: str, special: bool) -> str:
    """Map an incoming thread id onto the thread we already hold for that person.

    The export's CONVERSATION ID is not guaranteed to equal the id in the web
    URL, so a second id for the same one-to-one conversation becomes an alias.
    InMail and sponsored threads are kept separate (special=True).
    """
    if external_id and db.one(
        "SELECT 1 FROM threads WHERE channel='linkedin' AND external_id=?", (external_id,)
    ):
        return external_id
    if external_id:
        alias = db.get_state(NAME, f"alias:{external_id}")
        if alias:
            return alias
    if key and not special:
        row = db.one(
            "SELECT t.external_id, t.meta FROM threads t JOIN identities i ON i.contact_id = t.contact_id "
            "WHERE t.channel='linkedin' AND i.channel='linkedin' AND i.key=? "
            "ORDER BY t.last_message_at DESC LIMIT 1",
            (key,),
        )
        if row is not None:
            meta = loads(row["meta"], {}) or {}
            if not (meta.get("group") or meta.get("inmail") or meta.get("sponsored")):
                if external_id:
                    db.set_state(NAME, f"alias:{external_id}", row["external_id"])
                return row["external_id"]
    return external_id or ("p:" + key)


def _new_messages(db: DB, external_id: str, incoming: list[dict[str, Any]]) -> list[Message]:
    """Drop incoming messages we already hold; give the rest stable ids."""
    thread = db.one("SELECT id FROM threads WHERE channel='linkedin' AND external_id=?", (external_id,))
    existing: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    taken_ids: set[str] = set()
    if thread is not None:
        for row in db.all("SELECT external_id, direction, text, sent_at FROM messages WHERE thread_id=?", (thread["id"],)):
            existing[(row["direction"], _norm(row["text"]))].append(
                {"at": parse_iso(row["sent_at"]), "claimed": False}
            )
            taken_ids.add(row["external_id"])

    fresh: list[Message] = []
    for item in sorted(incoming, key=lambda m: m["sent_at"]):
        key = (item["direction"], _norm(item["text"]))
        when = parse_iso(item["sent_at"])
        candidates = [
            c for c in existing[key]
            if not c["claimed"] and (c["at"] is None or when is None or abs(c["at"] - when) <= MATCH_WINDOW)
        ]
        if candidates:
            closest = min(candidates, key=lambda c: abs(c["at"] - when) if c["at"] and when else timedelta(0))
            closest["claimed"] = True
            continue
        minute = item["sent_at"][:16]
        message_id = sha("li", item["direction"], key[1], minute)
        bump = 0
        while message_id in taken_ids:
            bump += 1
            message_id = sha("li", item["direction"], key[1], minute, bump)
        taken_ids.add(message_id)
        existing[key].append({"at": when, "claimed": True})
        fresh.append(
            Message(
                external_id=message_id,
                direction=item["direction"],
                sent_at=item["sent_at"],
                text=item["text"],
                sender_name=item.get("sender_name", ""),
                attachments=item.get("attachments") or [],
                meta=item.get("meta") or {},
            )
        )
    return fresh


def _store(db: DB, external_id: str, url: str, participant: Participant, messages: list[dict[str, Any]],
           subject: str, unread: bool | None, meta: dict[str, Any], full_read: bool) -> tuple[int, bool]:
    """Returns (new message count, thread was created)."""
    special = bool(meta.get("inmail") or meta.get("sponsored") or meta.get("group"))
    with db.tx():
        canonical = _canonical_external_id(db, external_id, participant.key, special)
        if full_read:
            # A full read supersedes any truncated list preview stored earlier.
            row = db.one("SELECT id FROM threads WHERE channel='linkedin' AND external_id=?", (canonical,))
            if row is not None:
                db.run(
                    "DELETE FROM messages WHERE thread_id=? AND json_extract(meta, '$.preview') = 1", (row["id"],)
                )
        fresh = _new_messages(db, canonical, messages)
        known = db.one("SELECT 1 FROM threads WHERE channel='linkedin' AND external_id=?", (canonical,)) is not None
        if unread and known and not fresh:
            # Still unread on LinkedIn, nothing new since last time: the owner may have read it
            # here in the meantime, and a repeat sync must not mark it unread again.
            unread = None
        if not url and not canonical.startswith("p:"):
            url = f"https://www.linkedin.com/messaging/thread/{canonical}/"
        result = upsert_thread(
            db,
            Thread(
                channel=NAME, external_id=canonical, participant=participant, messages=fresh,
                subject=subject, url=url, unread=unread, meta=meta,
            ),
        )
    return result.new_messages, result.created


# -- door 1: the computer-use agent ---------------------------------------------


def ingest_payload(db: DB, payload: dict[str, Any]) -> dict[str, Any]:
    """Store what the agent collected. Safe to call repeatedly with overlapping data."""
    collected_at = norm_iso(payload.get("collected_at"), now_iso())
    stats = {"threads": 0, "new_threads": 0, "new_messages": 0, "skipped": 0}
    for raw in payload.get("threads") or []:
        who = raw.get("participant") or {}
        name = str(who.get("name") or "").strip()
        profile_url = str(who.get("profile_url") or who.get("url") or "").strip()
        key = participant_key(profile_url, name)
        url = str(raw.get("url") or "").strip()
        external_id = str(raw.get("thread_id") or "").strip() or thread_id_from_url(url)
        if not key and not external_id:
            stats["skipped"] += 1
            continue

        messages = []
        for item in raw.get("messages") or []:
            text = clean_text(str(item.get("text") or ""))
            attachments = list(item.get("attachments") or [])
            if not text and not attachments:
                continue
            direction = "out" if item.get("from_me") or str(item.get("direction", "")).lower() == "out" else "in"
            sent_at = norm_iso(item.get("sent_at") or item.get("time"), collected_at)
            meta = {}
            if item.get("time_text"):
                meta["time_text"] = str(item["time_text"])  # what the UI literally showed
            messages.append({
                "direction": direction, "text": text, "sent_at": sent_at, "attachments": attachments,
                "sender_name": str(item.get("sender_name") or (name if direction == "in" else "")), "meta": meta,
            })

        full_read = bool(messages)
        preview = clean_text(str(raw.get("preview") or ""))
        if not messages and preview:
            # List-row only: keep the truncated preview so the thread can still be triaged.
            direction = "out" if raw.get("preview_from_me") else "in"
            messages.append({
                "direction": direction, "text": preview,
                "sent_at": norm_iso(raw.get("last_message_at"), collected_at),
                "sender_name": name if direction == "in" else "", "meta": {"preview": 1},
            })

        meta: dict[str, Any] = {"collected_at": collected_at}
        for flag in ("inmail", "sponsored", "group", "request"):
            if raw.get(flag):
                meta[flag] = True
        if not full_read:
            meta["partial"] = True
        elif "partial" not in meta:
            meta["partial"] = False

        participant = Participant(
            key=key or f"thread:{external_id}",
            name=name,
            handle=profile_slug(profile_url),
            url=profile_url,
            title=str(who.get("headline") or who.get("title") or "").strip(),
            company=str(who.get("company") or "").strip(),
            meta={k: v for k, v in {
                "connection": who.get("connection"), "inmail": raw.get("inmail"), "sponsored": raw.get("sponsored"),
            }.items() if v not in (None, "", False)},
        )
        added, created = _store(
            db, external_id, url, participant, messages, str(raw.get("subject") or ""),
            raw.get("unread"), meta, full_read,
        )
        stats["threads"] += 1
        stats["new_messages"] += added
        stats["new_threads"] += int(created)

    db.set_state(NAME, "last_collected_at", collected_at)
    if payload.get("request_count") is not None:
        db.set_state(NAME, "request_count", payload["request_count"])
    return stats


def known_threads(db: DB, limit: int = 60) -> list[dict[str, Any]]:
    """What the agent compares the inbox list against to decide which threads to open."""
    rows = db.all(
        "SELECT t.external_id, t.url, t.last_message_at, t.snippet, t.last_direction, t.meta, c.name "
        "FROM threads t LEFT JOIN contacts c ON c.id = t.contact_id "
        "WHERE t.channel='linkedin' ORDER BY t.last_message_at DESC LIMIT ?",
        (limit,),
    )
    return [
        {
            "thread_id": r["external_id"], "url": r["url"], "name": r["name"] or "",
            "last_message_at": r["last_message_at"], "last_from_me": r["last_direction"] == "out",
            "last_text": r["snippet"], "partial": bool((loads(r["meta"], {}) or {}).get("partial")),
        }
        for r in rows
    ]


# -- door 2: the official export ----------------------------------------------


HEADER_ALIASES = {
    "conversation id": "conversation_id", "conversation title": "title", "from": "from",
    "sender profile url": "sender_url", "to": "to", "recipient profile urls": "recipient_urls",
    "date": "date", "subject": "subject", "content": "content", "folder": "folder",
    "is message draft": "draft",
}


def read_export(path: Path) -> list[dict[str, str]]:
    """Parse messages.csv, tolerating a notes preamble and header changes between export versions."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines[:40]) if "conversation id" in line.lower()), 0)
    reader = csv.DictReader(io.StringIO("".join(lines[start:])))
    rows = []
    for raw in reader:
        row = {HEADER_ALIASES.get((k or "").strip().lower(), (k or "").strip().lower()): (v or "") for k, v in raw.items()}
        if row.get("conversation_id") or row.get("content"):
            rows.append(row)
    return rows


def guess_owner(rows: list[dict[str, str]]) -> str:
    """The owner is the one name that appears in almost every conversation."""
    seen: Counter[str] = Counter()
    by_conversation: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        for field in ("from", "to"):
            for name in row.get(field, "").split(","):
                if name.strip():
                    by_conversation[row.get("conversation_id", "")].add(name.strip())
    for names in by_conversation.values():
        seen.update(names)
    return seen.most_common(1)[0][0] if seen else ""


def import_export(db: DB, settings: Settings, path: Path, since_days: int = 365) -> dict[str, Any]:
    rows = read_export(path)
    if not rows:
        raise ValueError(f"No messages found in {path.name}. Is this LinkedIn's messages.csv?")
    owner = settings.owner_name or guess_owner(rows)
    owner_key = norm_name(owner)
    cutoff = now() - timedelta(days=since_days)
    stats = {"conversations": 0, "new_threads": 0, "new_messages": 0, "skipped_old": 0, "owner": owner}

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row.get("draft", "").strip().lower() in ("yes", "true", "1"):
            continue
        grouped[row.get("conversation_id") or sha(row.get("from"), row.get("to"))].append(row)

    for conversation_id, items in grouped.items():
        parsed = []
        people: dict[str, str] = {}  # name -> profile url, for everyone who is not the owner
        for row in items:
            when = parse_iso(_export_date(row.get("date", "")))
            sender = row.get("from", "").strip()
            from_me = norm_name(sender) == owner_key
            if not from_me and sender:
                people.setdefault(sender, row.get("sender_url", "").strip())
            if from_me:
                names = [n.strip() for n in row.get("to", "").split(",") if n.strip()]
                urls = [u.strip() for u in row.get("recipient_urls", "").split(",") if u.strip()]
                for index, name in enumerate(names):
                    if norm_name(name) != owner_key:
                        people.setdefault(name, urls[index] if index < len(urls) else "")
            body = clean_text(html.unescape(TAG_RE.sub(" ", row.get("content", ""))))
            if not body:
                continue
            parsed.append({
                "direction": "out" if from_me else "in", "text": body,
                "sent_at": to_iso(when) if when else now_iso(), "sender_name": sender,
            })
        if not parsed or not people:
            continue
        latest = max(parse_iso(m["sent_at"]) for m in parsed)
        if latest < cutoff:
            stats["skipped_old"] += 1
            continue

        name, profile_url = next(iter(people.items()))
        meta: dict[str, Any] = {"source": "export", "partial": False}
        if len(people) > 1:
            meta["group"] = True
        folder = (items[0].get("folder") or "").strip()
        if folder:
            meta["folder"] = folder
        subject = next((r.get("subject", "").strip() for r in items if r.get("subject", "").strip()), "")
        title = items[0].get("title", "").strip()
        if meta.get("group") and title:
            subject = subject or title
        participant = Participant(
            key=participant_key(profile_url, name), name=name, handle=profile_slug(profile_url), url=profile_url,
        )
        added, created = _store(db, conversation_id, "", participant, parsed, subject, None, meta, full_read=True)
        stats["conversations"] += 1
        stats["new_messages"] += added
        stats["new_threads"] += int(created)
    return stats


def _export_date(value: str) -> str:
    """'2026-11-01 14:23:45 UTC' -> ISO. Other shapes are passed through for parse_iso to try."""
    value = value.strip()
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)(?: ?(?:UTC|Z))?", value)
    if match:
        clock = match.group(2) if len(match.group(2)) == 8 else match.group(2) + ":00"
        return f"{match.group(1)}T{clock}+00:00"
    return value
