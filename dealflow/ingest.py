"""The single doorway into the database.

Every connector (Instagram, Proton email, LinkedIn, anything added later)
converts what it fetched into the three small shapes below and calls
`upsert_thread`. Deduplication, contact resolution and thread roll-ups all
happen here, so connectors stay dumb and re-running a sync is always safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .db import DB
from .util import (
    clean_text,
    company_from_domain,
    domain_of,
    dumps,
    loads,
    norm_iso,
    norm_name,
    now_iso,
    one_line,
    sha,
)

CHANNELS = ("instagram", "linkedin", "email")


@dataclass
class Participant:
    """The other party in a conversation."""

    key: str  # stable unique id within the channel (IGSID, profile slug, email address)
    name: str = ""
    handle: str = ""  # what a human would recognise: @user, email address, profile slug
    url: str = ""
    email: str = ""
    company: str = ""
    title: str = ""
    avatar_url: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    external_id: str
    direction: str  # 'in' (they wrote) | 'out' (I wrote)
    sent_at: str  # anything util.parse_iso understands
    text: str = ""
    sender_name: str = ""
    sender_handle: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Thread:
    channel: str
    external_id: str
    participant: Participant
    messages: list[Message] = field(default_factory=list)
    subject: str = ""
    url: str = ""
    unread: bool | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    is_demo: bool = False
    account: str = ""  # which of the owner's own accounts received it (accounts.key); "" if only one


@dataclass
class UpsertResult:
    thread_id: int
    new_messages: int
    created: bool


def upsert_thread(db: DB, thread: Thread) -> UpsertResult:
    if not thread.external_id:
        raise ValueError("thread.external_id is required")
    stamp = now_iso()
    with db.tx():
        contact_id = resolve_contact(db, thread.channel, thread.participant)
        row = db.one(
            "SELECT id, meta FROM threads WHERE channel=? AND external_id=?",
            (thread.channel, thread.external_id),
        )
        created = row is None
        if created:
            cur = db.run(
                "INSERT INTO threads(channel, external_id, contact_id, subject, url, meta, "
                "is_demo, account, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    thread.channel,
                    thread.external_id,
                    contact_id,
                    one_line(thread.subject, 300),
                    thread.url,
                    dumps(thread.meta),
                    int(thread.is_demo),
                    thread.account,
                    stamp,
                    stamp,
                ),
            )
            thread_id = int(cur.lastrowid)
        else:
            thread_id = int(row["id"])
            meta = loads(row["meta"], {}) or {}
            meta.update(thread.meta)
            # `account` is filled in if it was unknown, never reassigned: a thread
            # belongs to the account that first received it.
            db.run(
                "UPDATE threads SET contact_id=COALESCE(contact_id, ?), "
                "subject=CASE WHEN ?<>'' THEN ? ELSE subject END, "
                "url=CASE WHEN ?<>'' THEN ? ELSE url END, meta=?, "
                "account=CASE WHEN account='' THEN ? ELSE account END WHERE id=?",
                (
                    contact_id,
                    thread.subject,
                    one_line(thread.subject, 300),
                    thread.url,
                    thread.url,
                    dumps(meta),
                    thread.account,
                    thread_id,
                ),
            )

        new_messages = 0
        new_inbound = 0
        for message in thread.messages:
            if message.direction not in ("in", "out"):
                raise ValueError(f"bad direction {message.direction!r}")
            cur = db.run(
                "INSERT OR IGNORE INTO messages(thread_id, external_id, direction, sender_name, "
                "sender_handle, sent_at, text, attachments, meta) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    thread_id,
                    message.external_id,
                    message.direction,
                    message.sender_name,
                    message.sender_handle,
                    norm_iso(message.sent_at, stamp),
                    clean_text(message.text),
                    dumps(message.attachments),
                    dumps(message.meta),
                ),
            )
            new_messages += cur.rowcount
            if message.direction == "in":
                new_inbound += cur.rowcount

        # Read state: a connector that knows it (IMAP's Seen flag, LinkedIn's badge) says so.
        # One that cannot (Instagram) leaves it None, and a new message from them then makes
        # the conversation unread here. This is Dealflow's own marker; nothing is written back.
        unread = thread.unread
        if unread is None and new_inbound > 0:
            unread = True
        refresh_thread(db, thread_id, unread=unread, touched=new_messages > 0 or created)
    return UpsertResult(thread_id=thread_id, new_messages=new_messages, created=created)


def refresh_thread(
    db: DB, thread_id: int, unread: bool | None = None, touched: bool = True
) -> None:
    """Recompute the roll-up columns on a thread from its messages."""
    agg = db.one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(direction='in'),0) AS n_in, "
        "MIN(sent_at) AS first_at, MAX(sent_at) AS last_at, "
        "MAX(CASE WHEN direction='in' THEN sent_at END) AS last_in, "
        "MAX(CASE WHEN direction='out' THEN sent_at END) AS last_out "
        "FROM messages WHERE thread_id=?",
        (thread_id,),
    )
    last = db.one(
        "SELECT direction, text, attachments FROM messages WHERE thread_id=? "
        "ORDER BY sent_at DESC, id DESC LIMIT 1",
        (thread_id,),
    )
    snippet = ""
    last_direction = ""
    if last is not None:
        last_direction = last["direction"]
        snippet = one_line(last["text"], 220)
        if not snippet:
            attachments = loads(last["attachments"], []) or []
            if attachments:
                snippet = f"[{attachments[0].get('type', 'attachment')}]"
    inbound_ids = [
        r["external_id"]
        for r in db.all(
            "SELECT external_id FROM messages WHERE thread_id=? AND direction='in' "
            "ORDER BY sent_at, id",
            (thread_id,),
        )
    ]
    # Only inbound messages change the hash, so my own replies never trigger a re-triage.
    content_hash = sha(*inbound_ids) if inbound_ids else ""
    params: list[Any] = [
        agg["n"], agg["n_in"], agg["first_at"], agg["last_at"], agg["last_in"], agg["last_out"],
        last_direction, snippet, content_hash,
    ]
    sql = (
        "UPDATE threads SET message_count=?, inbound_count=?, first_message_at=?, "
        "last_message_at=?, last_inbound_at=?, last_outbound_at=?, last_direction=?, "
        "snippet=?, content_hash=?"
    )
    if unread is not None:
        sql += ", unread=?"
        params.append(int(bool(unread)))
    if touched:
        sql += ", updated_at=?"
        params.append(now_iso())
    sql += " WHERE id=?"
    params.append(thread_id)
    db.run(sql, params)


# -- contacts -----------------------------------------------------------------


def resolve_contact(db: DB, channel: str, p: Participant) -> int:
    """Find or create the contact behind a channel identity.

    Linking across channels is deliberately conservative: an identity only
    joins an existing contact on a hard key (same email address). Same-name
    matches are surfaced as merge suggestions instead, because two different
    people called "Rahul Sharma" silently fused into one is worse than a
    duplicate the user merges with one click.
    """
    key = (p.key or p.handle or p.email or p.name).strip()
    if not key:
        key = "unknown"
    if channel == "email":
        key = key.lower()
    stamp = now_iso()
    company = p.company or (company_from_domain(domain_of(p.email)) if p.email else "")

    row = db.one("SELECT id, contact_id, meta FROM identities WHERE channel=? AND key=?", (channel, key))
    if row is not None:
        meta = loads(row["meta"], {}) or {}
        meta.update({k: v for k, v in p.meta.items() if v not in (None, "")})
        db.run(
            "UPDATE identities SET handle=CASE WHEN ?<>'' THEN ? ELSE handle END, "
            "name=CASE WHEN ?<>'' THEN ? ELSE name END, "
            "url=CASE WHEN ?<>'' THEN ? ELSE url END, meta=? WHERE id=?",
            (p.handle, p.handle, p.name, p.name, p.url, p.url, dumps(meta), row["id"]),
        )
        contact_id = int(row["contact_id"])
        _fill_contact(db, contact_id, p, company, stamp)
        return contact_id

    contact_id = None
    if p.email:
        hit = db.one(
            "SELECT contact_id FROM identities WHERE channel='email' AND key=?", (p.email.lower(),)
        )
        if hit is not None:
            contact_id = int(hit["contact_id"])
    if contact_id is None and channel == "instagram" and p.handle.strip("@ "):
        # Instagram gives one person a different id for each of the owner's accounts
        # they write to, but their @username is the same. A username is unique on
        # Instagram, so it is a hard key: the same handle is the same person.
        hit = db.one(
            "SELECT contact_id FROM identities WHERE channel='instagram' AND lower(handle)=lower(?) "
            "ORDER BY id LIMIT 1",
            (p.handle,),
        )
        if hit is not None:
            contact_id = int(hit["contact_id"])
    if contact_id is None:
        name = p.name or p.handle or key
        cur = db.run(
            "INSERT INTO contacts(name, name_key, company, title, avatar_url, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (name, norm_name(name), company, p.title, p.avatar_url, stamp, stamp),
        )
        contact_id = int(cur.lastrowid)
    else:
        _fill_contact(db, contact_id, p, company, stamp)

    db.run(
        "INSERT INTO identities(contact_id, channel, key, handle, name, url, meta) VALUES(?,?,?,?,?,?,?)",
        (contact_id, channel, key, p.handle, p.name, p.url, dumps(p.meta)),
    )
    return contact_id


def _fill_contact(db: DB, contact_id: int, p: Participant, company: str, stamp: str) -> None:
    """Fill blanks on a contact; never overwrite what is already known."""
    row = db.one("SELECT name, company, title, avatar_url FROM contacts WHERE id=?", (contact_id,))
    if row is None:
        return
    updates: dict[str, str] = {}
    current_name = row["name"] or ""
    # Upgrade a placeholder name (a bare handle or email) to a real one.
    if p.name and (not current_name or "@" in current_name or " " not in current_name) \
            and p.name != current_name and " " in p.name.strip():
        updates["name"] = p.name
        updates["name_key"] = norm_name(p.name)
    if company and not row["company"]:
        updates["company"] = company
    if p.title and not row["title"]:
        updates["title"] = p.title
    if p.avatar_url and not row["avatar_url"]:
        updates["avatar_url"] = p.avatar_url
    if updates:
        updates["updated_at"] = stamp
        sets = ", ".join(f"{column}=?" for column in updates)
        db.run(f"UPDATE contacts SET {sets} WHERE id=?", [*updates.values(), contact_id])


def remember_account(db: DB, channel: str, key: str, username: str = "", label: str = "") -> None:
    """Record one of the owner's own accounts, so its conversations can be labelled by name."""
    if not key:
        return
    db.run(
        "INSERT INTO accounts(channel, key, username, label, updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(channel, key) DO UPDATE SET "
        "username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE accounts.username END, "
        "label=excluded.label, updated_at=excluded.updated_at",
        (channel, key, username, label, now_iso()),
    )


def merge_contacts(db: DB, keep_id: int, drop_id: int) -> None:
    if keep_id == drop_id:
        return
    with db.tx():
        keep = db.one("SELECT * FROM contacts WHERE id=?", (keep_id,))
        drop = db.one("SELECT * FROM contacts WHERE id=?", (drop_id,))
        if keep is None or drop is None:
            raise ValueError("contact not found")
        db.run("UPDATE identities SET contact_id=? WHERE contact_id=?", (keep_id, drop_id))
        db.run("UPDATE threads SET contact_id=? WHERE contact_id=?", (keep_id, drop_id))
        db.run(
            "UPDATE contacts SET company=CASE WHEN company='' THEN ? ELSE company END, "
            "title=CASE WHEN title='' THEN ? ELSE title END, "
            "avatar_url=CASE WHEN avatar_url='' THEN ? ELSE avatar_url END, updated_at=? WHERE id=?",
            (drop["company"], drop["title"], drop["avatar_url"], now_iso(), keep_id),
        )
        db.run("DELETE FROM contacts WHERE id=?", (drop_id,))


def merge_suggestions(db: DB, contact_id: int) -> list[dict[str, Any]]:
    """Other contacts that look like the same person (same full name, different channel)."""
    row = db.one("SELECT name_key FROM contacts WHERE id=?", (contact_id,))
    if row is None or " " not in (row["name_key"] or ""):
        return []
    mine = {r["channel"] for r in db.all("SELECT channel FROM identities WHERE contact_id=?", (contact_id,))}
    out = []
    for other in db.all(
        "SELECT id, name, company FROM contacts WHERE name_key=? AND id<>?", (row["name_key"], contact_id)
    ):
        channels = [
            r["channel"]
            for r in db.all("SELECT DISTINCT channel FROM identities WHERE contact_id=?", (other["id"],))
        ]
        if set(channels) - mine:
            out.append({"id": other["id"], "name": other["name"], "company": other["company"], "channels": channels})
    return out


# -- generic JSON intake (used by the LinkedIn agent and any future collector) --


def thread_from_payload(channel: str, data: dict[str, Any], is_demo: bool = False) -> Thread:
    """Build a Thread from loosely-shaped JSON. See collectors/README for the format."""
    who = data.get("participant") or {}
    participant = Participant(
        key=str(who.get("key") or who.get("handle") or who.get("url") or who.get("email") or who.get("name") or ""),
        name=str(who.get("name") or ""),
        handle=str(who.get("handle") or ""),
        url=str(who.get("url") or ""),
        email=str(who.get("email") or ""),
        company=str(who.get("company") or ""),
        title=str(who.get("title") or who.get("headline") or ""),
        avatar_url=str(who.get("avatar_url") or ""),
        meta=dict(who.get("meta") or {}),
    )
    external_id = str(data.get("external_id") or data.get("thread_id") or "")
    messages = []
    for index, item in enumerate(data.get("messages") or []):
        direction = str(item.get("direction") or "").lower()
        if direction not in ("in", "out"):
            direction = "out" if item.get("from_me") else "in"
        text = str(item.get("text") or "")
        sent_at = item.get("sent_at") or item.get("time") or ""
        message_id = str(item.get("external_id") or item.get("id") or "") or sha(
            external_id, direction, sent_at, text, index
        )
        messages.append(
            Message(
                external_id=message_id,
                direction=direction,
                sent_at=sent_at,
                text=text,
                sender_name=str(item.get("sender_name") or (participant.name if direction == "in" else "")),
                sender_handle=str(item.get("sender_handle") or ""),
                attachments=list(item.get("attachments") or []),
                meta=dict(item.get("meta") or {}),
            )
        )
    return Thread(
        channel=channel,
        external_id=external_id,
        participant=participant,
        messages=messages,
        subject=str(data.get("subject") or ""),
        url=str(data.get("url") or ""),
        unread=data.get("unread"),
        meta=dict(data.get("meta") or {}),
        is_demo=is_demo,
        account=str(data.get("account") or ""),
    )
