"""Read models for the UI: the thread list with its counts, and one thread in full."""

from __future__ import annotations

from typing import Any

from .db import DB
from .ingest import merge_suggestions
from .triage.rubric import NOUL_SIGNALS, SCORE_SIGNALS, Rubric
from .util import loads, now, now_iso, parse_iso

CATEGORY = "COALESCE(g.category_override, g.category, '')"
PRIORITY = "COALESCE(g.priority_override, g.priority, '')"
NOT_SNOOZED = "(t.snoozed_until IS NULL OR t.snoozed_until <= :now)"
JOINS = (
    "FROM threads t LEFT JOIN triage g ON g.thread_id = t.id LEFT JOIN contacts c ON c.id = t.contact_id"
)

STAGES = [
    {"id": "new", "label": "New"},
    {"id": "shortlist", "label": "Shortlisted"},
    {"id": "talking", "label": "In talks"},
    {"id": "won", "label": "Won"},
    {"id": "passed", "label": "Passed"},
]
STAGE_IDS = {s["id"] for s in STAGES}

VIEWS: dict[str, dict[str, str]] = {
    "queue": {
        "label": "To triage",
        "where": f"t.stage='new' AND {NOT_SNOOZED} AND t.inbound_count>0 "
                 f"AND {PRIORITY} IN ('P0','P1','P2','')",
        "order": f"CASE {PRIORITY} WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END, "
                 "g.score DESC, t.last_message_at DESC",
    },
    "review": {
        "label": "Needs review",
        "where": f"t.stage='new' AND {NOT_SNOOZED} AND g.needs_review=1 "
                 "AND g.category_override IS NULL AND g.priority_override IS NULL",
        "order": "g.score DESC, t.last_message_at DESC",
    },
    "waiting": {
        "label": "Waiting on me",
        "where": f"t.stage IN ('shortlist','talking') AND t.last_direction='in' AND {NOT_SNOOZED}",
        "order": "t.last_inbound_at ASC",
    },
    "shortlist": {"label": "Shortlisted", "where": "t.stage='shortlist'", "order": "t.last_message_at DESC"},
    "talking": {"label": "In talks", "where": "t.stage='talking'", "order": "t.last_message_at DESC"},
    "won": {"label": "Won", "where": "t.stage='won'", "order": "t.last_message_at DESC"},
    "passed": {"label": "Passed", "where": "t.stage='passed'", "order": "t.updated_at DESC"},
    "low": {
        "label": "Low priority",
        "where": f"t.stage='new' AND {NOT_SNOOZED} AND t.inbound_count>0 AND {PRIORITY}='P3'",
        "order": "t.last_message_at DESC",
    },
    "snoozed": {
        "label": "Snoozed",
        "where": "t.snoozed_until IS NOT NULL AND t.snoozed_until > :now",
        "order": "t.snoozed_until ASC",
    },
    "starred": {"label": "Starred", "where": "t.starred=1", "order": "t.last_message_at DESC"},
    "all": {"label": "Everything", "where": "1=1", "order": "t.last_message_at DESC"},
}

LIST_COLUMNS = (
    "t.id, t.channel, t.subject, t.url, t.snippet, t.message_count, t.inbound_count, t.last_message_at, "
    "t.last_inbound_at, t.last_direction, t.unread, t.stage, t.starred, t.snoozed_until, t.is_demo, t.meta, "
    "t.contact_id, t.account, c.name AS contact_name, c.company, c.title, "
    f"{CATEGORY} AS category, {PRIORITY} AS priority, g.score, g.needs_review, g.review_reason, "
    "g.summary, g.source, g.signals, g.extracted, g.category_override, g.priority_override, "
    "g.thread_id AS triaged, g.content_hash AS triaged_hash, t.content_hash, "
    "(SELECT handle FROM identities i WHERE i.contact_id = t.contact_id AND i.channel = t.channel LIMIT 1) AS handle, "
    "(SELECT COUNT(DISTINCT t2.channel) FROM threads t2 WHERE t2.contact_id = t.contact_id) AS channel_count, "
    # how many of the owner's own accounts this person has written to
    "(SELECT COUNT(DISTINCT t3.account) FROM threads t3 WHERE t3.contact_id = t.contact_id AND t3.account <> '') AS account_count, "
    # the most recent message that carried an attachment, for the chip under a row
    "(SELECT m.attachments FROM messages m WHERE m.thread_id = t.id AND m.attachments <> '[]' "
    "ORDER BY m.sent_at DESC, m.id DESC LIMIT 1) AS last_attachments"
)

# What "the inbox" is: new, not snoozed, and they have written. The mail-style
# UI splits it into tabs by priority; the queue and low views are its two halves.
INBOX_WHERE = f"t.stage='new' AND {NOT_SNOOZED} AND t.inbound_count>0"


def _connector_clause(connector: str, gmail_accounts: list[str], binds: dict[str, Any]) -> str:
    """Conversations one connector brought in. Gmail and Proton Mail share the email channel
    and are told apart by which of the owner's addresses received the conversation."""
    if connector in ("instagram", "linkedin"):
        binds["connector_channel"] = connector
        return "t.channel = :connector_channel"
    if connector not in ("gmail", "email"):
        return "0"
    names = []
    for index, address in enumerate(gmail_accounts):
        binds[f"gmail_{index}"] = address
        names.append(f":gmail_{index}")
    if connector == "gmail":
        return f"(t.channel = 'email' AND t.account IN ({', '.join(names)}))" if names else "0"
    return f"(t.channel = 'email' AND t.account NOT IN ({', '.join(names)}))" if names else "t.channel = 'email'"


def _filters(params: dict[str, str], gmail_accounts: list[str] | None = None) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    binds: dict[str, Any] = {}
    if params.get("connector"):
        clauses.append(_connector_clause(params["connector"], gmail_accounts or [], binds))
    if params.get("channel"):
        clauses.append("t.channel = :channel")
        binds["channel"] = params["channel"]
    if params.get("category"):
        clauses.append(f"{CATEGORY} = :category")
        binds["category"] = params["category"]
    if params.get("priority"):
        clauses.append(f"{PRIORITY} = :priority")
        binds["priority"] = params["priority"]
    if params.get("account"):
        clauses.append("t.account = :account")
        binds["account"] = params["account"]
    query = (params.get("q") or "").strip()
    if query:
        binds["q"] = "%" + query.replace("%", r"\%").replace("_", r"\_") + "%"
        clauses.append(
            "(c.name LIKE :q ESCAPE '\\' OR c.company LIKE :q ESCAPE '\\' OR t.subject LIKE :q ESCAPE '\\' "
            "OR g.summary LIKE :q ESCAPE '\\' OR EXISTS (SELECT 1 FROM messages m WHERE m.thread_id = t.id "
            "AND m.text LIKE :q ESCAPE '\\'))"
        )
    return (" AND " + " AND ".join(clauses)) if clauses else "", binds


def list_threads(db: DB, rubric: Rubric, params: dict[str, str],
                 gmail_accounts: list[str] | None = None) -> dict[str, Any]:
    view = params.get("view") if params.get("view") in VIEWS else "queue"
    # Searching looks everywhere: a search scoped to one queue hides what you are looking for.
    effective_view = "all" if (params.get("q") or "").strip() else view
    spec = VIEWS[effective_view]
    extra, binds = _filters(params, gmail_accounts)
    binds["now"] = now_iso()
    limit = min(int(params.get("limit") or 200), 500)
    rows = db.conn.execute(
        f"SELECT {LIST_COLUMNS} {JOINS} WHERE {spec['where']}{extra} ORDER BY {spec['order']} LIMIT {limit}",
        binds,
    ).fetchall()
    total = db.conn.execute(f"SELECT COUNT(*) {JOINS} WHERE {spec['where']}{extra}", binds).fetchone()[0]

    # Facets describe the current view before channel/category/priority filters,
    # so the filter chips show where else there is something to look at.
    facet_binds = {"now": binds["now"]}
    facet_extra = ""
    if "q" in binds:
        q_only, q_binds = _filters({"q": params.get("q", "")})
        facet_extra, facet_binds = q_only, {**facet_binds, **q_binds}
    facets: dict[str, dict[str, int]] = {}
    for name, expr in (("channel", "t.channel"), ("category", CATEGORY), ("priority", PRIORITY), ("account", "t.account")):
        facets[name] = {
            (r[0] or ""): r[1]
            for r in db.conn.execute(
                f"SELECT {expr}, COUNT(*) {JOINS} WHERE {spec['where']}{facet_extra} GROUP BY 1", facet_binds
            ).fetchall()
        }

    return {
        "view": view,
        "searching": effective_view != view,
        "total": total,
        "threads": [_list_item(r, rubric) for r in rows],
        "facets": facets,
        "counts": view_counts(db),
        "nav": nav_counts(db, gmail_accounts),
    }


def nav_counts(db: DB, gmail_accounts: list[str] | None = None) -> dict[str, Any]:
    """Unread counts for the navigation badges: the inbox and its priority tabs, each
    label (category) and each connector. Passed conversations never count."""
    binds = {"now": now_iso()}
    tiers: dict[str, dict[str, int]] = {}
    for row in db.conn.execute(
        f"SELECT {PRIORITY}, COUNT(*), COALESCE(SUM(t.unread), 0) {JOINS} WHERE {INBOX_WHERE} GROUP BY 1", binds
    ).fetchall():
        tiers[row[0] or ""] = {"total": row[1], "unread": row[2]}
    labels = {
        (row[0] or ""): row[1]
        for row in db.conn.execute(
            f"SELECT {CATEGORY}, COUNT(*) {JOINS} WHERE t.unread=1 AND t.stage<>'passed' GROUP BY 1"
        ).fetchall()
    }
    gmail = set(gmail_accounts or [])
    connectors = {"instagram": 0, "gmail": 0, "email": 0, "linkedin": 0}
    for row in db.conn.execute(
        "SELECT t.channel, t.account, COUNT(*) FROM threads t WHERE t.unread=1 AND t.stage<>'passed' GROUP BY 1, 2"
    ).fetchall():
        name = ("gmail" if row[1] in gmail else "email") if row[0] == "email" else row[0]
        connectors[name] = connectors.get(name, 0) + row[2]
    return {
        "inbox_unread": sum(tier["unread"] for tier in tiers.values()),
        "tiers": tiers,
        "labels": labels,
        "connectors": connectors,
    }


def view_counts(db: DB) -> dict[str, int]:
    binds = {"now": now_iso()}
    return {
        name: db.conn.execute(f"SELECT COUNT(*) {JOINS} WHERE {spec['where']}", binds).fetchone()[0]
        for name, spec in VIEWS.items()
    }


def _waiting_days(row: Any) -> int | None:
    if row["last_direction"] != "in":
        return None
    last = parse_iso(row["last_inbound_at"])
    return None if last is None else max(0, (now() - last).days)


def _attachment_name(raw: str | None) -> str:
    """A short name for a conversation's latest attachment: its file name, else its kind."""
    items = loads(raw, []) or []
    if not items or not isinstance(items[0], dict):
        return ""
    first = items[0]
    return str(first.get("name") or first.get("type") or "attachment")[:80]


def _list_item(row: Any, rubric: Rubric) -> dict[str, Any]:
    signals = loads(row["signals"], {}) or {}
    extracted = loads(row["extracted"], {}) or {}
    meta = loads(row["meta"], {}) or {}
    return {
        "id": row["id"],
        "channel": row["channel"],
        "name": row["contact_name"] or row["handle"] or "Unknown",
        "company": row["company"] or "",
        "title": row["title"] or "",
        "handle": row["handle"] or "",
        "subject": row["subject"] or "",
        "snippet": row["snippet"] or "",
        "summary": row["summary"] or "",
        "category": row["category"],
        "category_label": rubric.label(row["category"]) if row["category"] else "",
        "priority": row["priority"],
        "score": row["score"],
        "needs_review": bool(row["needs_review"]) and not (row["category_override"] or row["priority_override"]),
        "review_reason": row["review_reason"] or "",
        "overridden": bool(row["category_override"] or row["priority_override"]),
        "source": row["source"] or "",
        "triaged": row["triaged"] is not None,
        "stale_triage": row["triaged"] is not None and row["triaged_hash"] != row["content_hash"],
        "stage": row["stage"],
        "starred": bool(row["starred"]),
        "unread": bool(row["unread"]),
        "snoozed_until": row["snoozed_until"],
        "last_message_at": row["last_message_at"],
        "last_direction": row["last_direction"],
        "waiting_days": _waiting_days(row),
        "message_count": row["message_count"],
        "replied": (row["message_count"] or 0) > (row["inbound_count"] or 0),
        "attachment": _attachment_name(row["last_attachments"]),
        "deadline": ((extracted.get("deadlines") or [""])[0] or "")[:40],
        "channel_count": row["channel_count"] or 1,
        "account": row["account"] or "",
        "account_count": row["account_count"] or 0,
        "risk": float(signals.get("risk") or 0) >= 0.5,
        "money": float(signals.get("money") or 0) >= 0.6,
        "urgent": float(signals.get("urgency") or 0) >= 0.6,
        "amounts": (extracted.get("amounts") or [])[:2],
        "partial": bool(meta.get("partial")),
        "is_demo": bool(row["is_demo"]),
    }


SIGNAL_LABELS = {
    "opportunity": "Real opportunity",
    "value": "Deal value",
    "credibility": "Sender credibility",
    "urgency": "Urgency",
    "personalised": "Written for you",
    "reply_expected": "Waiting on a reply",
    "money": "States terms",
    "risk": "Scam risk",
}


def _signal_rows(signals: dict[str, Any], rubric: Rubric) -> list[dict[str, Any]]:
    rows = []
    scales = rubric.public()["scales"]
    for key in ("value", "opportunity", "credibility", "urgency", "personalised", "money", "reply_expected", "risk"):
        value = signals.get(key)
        if value is None:
            continue
        row: dict[str, Any] = {"key": key, "label": SIGNAL_LABELS[key], "value": round(float(value), 3)}
        if key in SCORE_SIGNALS and scales.get(key):
            levels = scales[key]
            index = min(len(levels) - 1, max(0, round(float(value) * (len(levels) - 1))))
            name = levels[index].split(":")[0].strip()
            if ":" in levels[index] and len(name) <= 22:
                row["level"] = name
            row["kind"] = "scale"
        else:
            row["kind"] = "yesno" if key in NOUL_SIGNALS else "scale"
        rows.append(row)
    for name, value in (signals.get("flags") or {}).items():
        if value is not None:
            rows.append({"key": name, "label": name.replace("_", " ").capitalize(), "value": round(float(value), 3), "kind": "yesno"})
    return rows


def thread_detail(db: DB, rubric: Rubric, thread_id: int) -> dict[str, Any] | None:
    row = db.conn.execute(
        f"SELECT {LIST_COLUMNS}, g.answers, g.draft_reply, g.model, g.cost, g.triaged_at, g.category AS model_category, "
        f"g.priority AS model_priority, g.category_confidence {JOINS} WHERE t.id = :id",
        {"id": thread_id},
    ).fetchone()
    if row is None:
        return None
    item = _list_item(row, rubric)
    signals = loads(row["signals"], {}) or {}
    probabilities = signals.get("category_probs") or {}
    item.update(
        {
            "url": row["url"] or "",
            "meta": loads(row["meta"], {}) or {},
            "contact_id": row["contact_id"],
            "draft_reply": row["draft_reply"] or "",
            "model": row["model"] or "",
            "cost": row["cost"] or 0,
            "triaged_at": row["triaged_at"],
            "model_category": row["model_category"] or "",
            "model_priority": row["model_priority"] or "",
            "category_confidence": row["category_confidence"],
            "extracted": loads(row["extracted"], {}) or {},
            "signals": _signal_rows(signals, rubric),
            "filter_reason": signals.get("filter") or "",
            "category_probs": [
                {"id": key, "label": rubric.label(key), "p": round(float(value), 3)}
                for key, value in sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)[:4]
                if value and float(value) >= 0.03
            ],
        }
    )
    item["messages"] = [
        {
            "id": m["id"],
            "direction": m["direction"],
            "sender": m["sender_name"] or m["sender_handle"] or "",
            "sent_at": m["sent_at"],
            "text": m["text"],
            "attachments": loads(m["attachments"], []) or [],
            "preview": bool((loads(m["meta"], {}) or {}).get("preview")),
        }
        for m in db.all("SELECT * FROM messages WHERE thread_id=? ORDER BY sent_at, id", (thread_id,))
    ]
    item["notes"] = [
        {"id": n["id"], "body": n["body"], "created_at": n["created_at"]}
        for n in db.all("SELECT * FROM notes WHERE thread_id=? ORDER BY id DESC", (thread_id,))
    ]
    item["events"] = [
        {"kind": e["kind"], "detail": loads(e["detail"], {}), "created_at": e["created_at"]}
        for e in db.all("SELECT * FROM events WHERE thread_id=? ORDER BY id DESC LIMIT 20", (thread_id,))
    ]
    item["identities"] = []
    item["other_threads"] = []
    item["merge_suggestions"] = []
    if row["contact_id"]:
        # One person has a separate Instagram identity for each of the owner's accounts
        # they wrote to (same handle, different id): show the handle once.
        shown: set[tuple[str, str]] = set()
        for i in db.all("SELECT * FROM identities WHERE contact_id=? ORDER BY channel, id", (row["contact_id"],)):
            key = (i["channel"], (i["handle"] or i["key"]).lower())
            if key in shown:
                continue
            shown.add(key)
            item["identities"].append(
                {"channel": i["channel"], "handle": i["handle"], "name": i["name"], "url": i["url"], "meta": loads(i["meta"], {}) or {}}
            )
        item["other_threads"] = [
            {
                "id": o["id"], "channel": o["channel"], "subject": o["subject"], "snippet": o["snippet"],
                "last_message_at": o["last_message_at"], "stage": o["stage"], "account": o["account"],
            }
            for o in db.all(
                "SELECT id, channel, subject, snippet, last_message_at, stage, account FROM threads "
                "WHERE contact_id=? AND id<>? ORDER BY last_message_at DESC LIMIT 8",
                (row["contact_id"], thread_id),
            )
        ]
        item["merge_suggestions"] = merge_suggestions(db, row["contact_id"])
    return item
