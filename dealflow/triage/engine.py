"""Runs triage over threads that are new or have new inbound messages.

Per thread, in order:
  1. Automated email (newsletters, receipts) is filed as Noise without a model call.
  2. With an OpenRouter key, Jev answers the rubric's questions in one request.
     Without one, keyword rules stand in so the app still sorts.
  3. The rubric turns those signals into a category, a 0-100 score, a priority
     tier, and a "needs review" flag where Jev was unsure about something
     that matters.
  4. Optionally a chat model writes a one-line brief.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from ..config import Settings
from ..db import DB
from ..util import domain_of, dumps, loads, now, now_iso, parse_iso, truncate
from . import heuristics, jev, writer
from .rubric import Rubric, Verdict

CHANNEL_NAMES = {
    "instagram": "Instagram direct message",
    "linkedin": "LinkedIn message",
    "email": "Email",
}
PER_MESSAGE_CHARS = 2500
FATAL_STATUS = {401, 402, 403, 404}

_run_lock = threading.Lock()

Progress = Callable[[str], None]


class TriageHalt(Exception):
    """Stop the whole run: the key is wrong, out of credits, or the model id is bad."""


# -- context ------------------------------------------------------------------


def load_context(db: DB, thread_id: int) -> dict[str, Any] | None:
    thread = db.one("SELECT * FROM threads WHERE id=?", (thread_id,))
    if thread is None:
        return None
    contact = db.one("SELECT * FROM contacts WHERE id=?", (thread["contact_id"],)) if thread["contact_id"] else None
    identities = (
        db.all("SELECT * FROM identities WHERE contact_id=?", (thread["contact_id"],)) if thread["contact_id"] else []
    )
    messages = db.all("SELECT * FROM messages WHERE thread_id=? ORDER BY sent_at, id", (thread_id,))
    other_channels = []
    if thread["contact_id"]:
        other_channels = [
            r["channel"]
            for r in db.all(
                "SELECT DISTINCT channel FROM threads WHERE contact_id=? AND channel<>? AND inbound_count>0",
                (thread["contact_id"], thread["channel"]),
            )
        ]
    # Only meaningful once the owner has more than one account on this channel.
    received_on = ""
    other_accounts = 0
    if thread["account"] and db.scalar("SELECT COUNT(*) FROM accounts WHERE channel=?", (thread["channel"],), 0) > 1:
        account = db.one("SELECT username, label FROM accounts WHERE channel=? AND key=?", (thread["channel"], thread["account"]))
        if account is not None:
            handle = f"@{account['username']}" if account["username"] else ""
            received_on = " ".join(part for part in (handle, f"({account['label']})" if account["label"] else "") if part)
        if thread["contact_id"]:
            other_accounts = db.scalar(
                "SELECT COUNT(DISTINCT account) FROM threads WHERE contact_id=? AND channel=? AND account<>'' AND account<>? "
                "AND inbound_count>0",
                (thread["contact_id"], thread["channel"], thread["account"]), 0,
            )
    return {
        "thread": thread,
        "contact": contact,
        "identities": identities,
        "messages": messages,
        "other_channels": other_channels,
        "received_on": received_on,
        "other_accounts": other_accounts,
    }


def sender_profile(ctx: dict[str, Any]) -> dict[str, Any]:
    thread, contact = ctx["thread"], ctx["contact"]
    identity = next((i for i in ctx["identities"] if i["channel"] == thread["channel"]), None)
    meta = loads(identity["meta"], {}) if identity else {}
    profile: dict[str, Any] = {
        "name": (contact["name"] if contact else "") or (identity["name"] if identity else ""),
        "handle": identity["handle"] if identity else "",
        "company": contact["company"] if contact else "",
        "title": contact["title"] if contact else "",
    }
    if thread["channel"] == "email" and identity:
        profile["email_domain"] = domain_of(identity["key"])
    for source, target in (
        ("followers", "followers"), ("verified", "verified"), ("follows_me", "follows_owner"),
        ("connection", "connection"), ("inmail", "is_inmail"), ("sponsored", "is_sponsored"),
    ):
        if meta.get(source) not in (None, ""):
            profile[target] = meta[source]
    if ctx["other_channels"]:
        profile["also_wrote_via"] = ctx["other_channels"]
    if ctx.get("other_accounts"):
        profile["also_wrote_to_other_accounts_of_owner"] = ctx["other_accounts"]
    return {k: v for k, v in profile.items() if v not in (None, "", [])}


def build_state(rubric: Rubric, ctx: dict[str, Any]) -> dict[str, Any]:
    """The `state` Jev judges. Kept under jev.STATE_BYTE_BUDGET so it stays in fast mode."""
    thread, messages = ctx["thread"], ctx["messages"]
    turns = [
        {
            "from": "sender" if m["direction"] == "in" else "owner",
            "date": (m["sent_at"] or "")[:10],
            "text": truncate(m["text"] or _attachment_note(m), PER_MESSAGE_CHARS),
        }
        for m in messages
    ]
    state: dict[str, Any] = {
        "owner": rubric.owner_context,
        "channel": CHANNEL_NAMES.get(thread["channel"], thread["channel"]),
        "sender": sender_profile(ctx),
        "history": {
            "messages_from_sender": thread["inbound_count"],
            "replies_from_owner": thread["message_count"] - thread["inbound_count"],
            "first_contact": (thread["first_message_at"] or "")[:10],
            "latest_message": (thread["last_message_at"] or "")[:10],
            "latest_message_from": "sender" if thread["last_direction"] == "in" else "owner",
            "today": now_iso()[:10],
        },
        "conversation": turns,
    }
    if thread["subject"]:
        state["subject"] = thread["subject"]
    if ctx.get("received_on"):
        state["received_on_owner_account"] = ctx["received_on"]

    # Too long? Keep the opening message (the pitch) and the most recent turns.
    if _size(state) > jev.STATE_BYTE_BUDGET and len(turns) > 2:
        first_inbound = next((t for t in turns if t["from"] == "sender"), turns[0])
        kept: list[dict[str, Any]] = []
        state["conversation"] = [first_inbound]
        base = _size(state)
        for turn in reversed(turns):
            if turn is first_inbound:
                continue
            cost = len(json.dumps(turn, ensure_ascii=False).encode("utf-8")) + 2
            if base + cost > jev.STATE_BYTE_BUDGET:
                break
            kept.append(turn)
            base += cost
        skipped = len(turns) - 1 - len(kept)
        middle = [{"from": "note", "date": "", "text": f"[{skipped} earlier messages omitted]"}] if skipped > 0 else []
        state["conversation"] = [first_inbound, *middle, *reversed(kept)]
    while _size(state) > jev.STATE_BYTE_BUDGET and state["conversation"]:
        longest = max(state["conversation"], key=lambda t: len(t["text"]))
        if len(longest["text"]) <= 200:
            break
        longest["text"] = truncate(longest["text"], len(longest["text"]) // 2)
    return state


def transcript(ctx: dict[str, Any], limit_chars: int = 12000) -> str:
    lines = []
    for m in ctx["messages"]:
        who = "Them" if m["direction"] == "in" else "Me"
        lines.append(f"{who} ({(m['sent_at'] or '')[:10]}): {m['text'] or _attachment_note(m)}")
    text = "\n\n".join(lines)
    if len(text) > limit_chars:
        text = text[: limit_chars // 3] + "\n\n[...]\n\n" + text[-(2 * limit_chars // 3):]
    return text


def inbound_text(ctx: dict[str, Any]) -> str:
    parts = [ctx["thread"]["subject"] or ""]
    parts += [m["text"] for m in ctx["messages"] if m["direction"] == "in" and m["text"]]
    return "\n".join(p for p in parts if p)


def _attachment_note(message: Any) -> str:
    attachments = loads(message["attachments"], []) or []
    if not attachments:
        return ""
    return "[sent " + ", ".join(str(a.get("type", "attachment")) for a in attachments[:3]) + "]"


def _size(state: dict[str, Any]) -> int:
    return len(json.dumps(state, ensure_ascii=False).encode("utf-8"))


# -- one thread ---------------------------------------------------------------


def triage_thread(db: DB, settings: Settings, rubric: Rubric, thread_id: int) -> dict[str, Any] | None:
    ctx = load_context(db, thread_id)
    if ctx is None:
        return None
    thread = ctx["thread"]
    meta = loads(thread["meta"], {}) or {}
    text = inbound_text(ctx)
    answers: dict[str, Any] = {}
    cost = 0.0
    model = ""

    if meta.get("automated"):
        signals = {
            "category": "noise", "category_probs": {"noise": 1.0}, "category_confidence": 1.0,
            "opportunity": 0.0, "value": 0.0, "credibility": 0.3, "urgency": 0.0,
            "personalised": 0.0, "reply_expected": 0.0, "money": 0.0, "risk": 0.0, "declined": [],
            "filter": meta.get("automated_reason") or "automated email",
        }
        source = "filter"
    elif settings.jev_ready:
        try:
            response = jev.decide(
                settings.openrouter_api_key,
                settings.jev_model,
                build_state(rubric, ctx),
                rubric.questions(),
                base_url=settings.openrouter_base_url,
            )
        except jev.JevError as error:
            if error.status in FATAL_STATUS:
                raise TriageHalt(f"Jev request rejected ({error}). Check OPENROUTER_API_KEY, credits and JEV_MODEL.") from None
            raise
        answers = response.get("answers") or {}
        usage = response.get("usage") or {}
        cost = float(usage.get("cost") or 0)
        model = str(response.get("model") or settings.jev_model)
        signals = rubric.signals_from_answers(answers)
        source = "jev"
        _record_spend(db, "triage", model, cost, int(usage.get("input_tokens") or 0))
    else:
        signals = heuristics.classify(text, sender_profile(ctx))
        source = "rules"

    verdict = rubric.verdict(signals)
    if source == "filter":
        verdict.needs_review, verdict.review_reason = False, ""

    first_in = next((m["text"] for m in ctx["messages"] if m["direction"] == "in" and m["text"]), "")
    last_in = next((m["text"] for m in reversed(ctx["messages"]) if m["direction"] == "in" and m["text"]), "")
    summary = heuristics.summarize(first_in, last_in) or (thread["subject"] or "")
    extracted: dict[str, Any] = heuristics.extract_facts(text)

    if settings.writer_model and settings.jev_ready and source == "jev" and verdict.priority in ("P0", "P1", "P2"):
        try:
            sender = sender_profile(ctx)
            brief, writer_cost = writer.summarize(
                settings, transcript(ctx), f"{sender.get('name', '')} {sender.get('handle', '')}".strip(),
                CHANNEL_NAMES.get(thread["channel"], thread["channel"]),
            )
            _record_spend(db, "summary", settings.writer_model, writer_cost, 0)
            cost += writer_cost
            if brief.get("summary"):
                summary = brief["summary"]
            for key in ("ask", "terms", "next_step"):
                if brief.get(key):
                    extracted[key] = brief[key]
            if brief.get("company") and ctx["contact"] is not None and not ctx["contact"]["company"]:
                db.run("UPDATE contacts SET company=? WHERE id=?", (brief["company"][:120], ctx["contact"]["id"]))
        except writer.WriterError as error:
            extracted["writer_error"] = str(error)[:200]

    _save(db, thread, verdict, answers, summary, extracted, source, model, cost)
    return {
        "thread_id": thread_id, "category": verdict.category, "priority": verdict.priority,
        "score": verdict.score, "source": source, "cost": cost,
    }


def _save(db: DB, thread: Any, verdict: Verdict, answers: dict[str, Any], summary: str,
          extracted: dict[str, Any], source: str, model: str, cost: float) -> None:
    stamp = now_iso()
    with db.tx():
        previous = db.one("SELECT priority, category FROM triage WHERE thread_id=?", (thread["id"],))
        db.run(
            "INSERT INTO triage(thread_id, content_hash, category, category_confidence, priority, score, "
            "needs_review, review_reason, signals, answers, summary, extracted, source, model, cost, triaged_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(thread_id) DO UPDATE SET content_hash=excluded.content_hash, "
            "category=excluded.category, category_confidence=excluded.category_confidence, "
            "priority=excluded.priority, score=excluded.score, needs_review=excluded.needs_review, "
            "review_reason=excluded.review_reason, signals=excluded.signals, answers=excluded.answers, "
            "summary=excluded.summary, extracted=excluded.extracted, source=excluded.source, "
            "model=excluded.model, cost=triage.cost+excluded.cost, triaged_at=excluded.triaged_at",
            (
                thread["id"], thread["content_hash"], verdict.category, verdict.category_confidence,
                verdict.priority, verdict.score, int(verdict.needs_review), verdict.review_reason,
                dumps(verdict.signals), dumps(answers), summary, dumps(extracted), source, model, cost, stamp,
            ),
        )
        if previous is not None and previous["priority"] and previous["priority"] != verdict.priority:
            db.run(
                "INSERT INTO events(thread_id, kind, detail, created_at) VALUES(?,?,?,?)",
                (thread["id"], "reprioritised", dumps({"from": previous["priority"], "to": verdict.priority}), stamp),
            )
        # Something I passed on has written again and now looks important: bring it back.
        if previous is not None and thread["stage"] == "passed" and verdict.priority in ("P0", "P1"):
            db.run("UPDATE threads SET stage='new', snoozed_until=NULL WHERE id=?", (thread["id"],))
            db.run(
                "INSERT INTO events(thread_id, kind, detail, created_at) VALUES(?,?,?,?)",
                (thread["id"], "resurfaced", dumps({"priority": verdict.priority}), stamp),
            )


def _record_spend(db: DB, kind: str, model: str, cost: float, input_tokens: int) -> None:
    db.run(
        "INSERT INTO spend(kind, model, cost, input_tokens, created_at) VALUES(?,?,?,?,?)",
        (kind, model, cost, input_tokens, now_iso()),
    )


# -- batches ------------------------------------------------------------------


def _worker(db: DB, settings: Settings, rubric: Rubric, thread_id: int) -> dict[str, Any] | None:
    try:
        return triage_thread(db, settings, rubric, thread_id)
    finally:
        db.close()  # this pool thread's connection


def pending_ids(db: DB, settings: Settings, force: bool = False, limit: int | None = None) -> list[int]:
    """Threads never triaged, with new inbound messages, or sorted by rules now that Jev is available."""
    where = "t.inbound_count > 0"
    if not force:
        where += " AND (g.thread_id IS NULL OR g.content_hash <> t.content_hash"
        if settings.jev_ready:
            where += " OR g.source = 'rules'"
        where += ")"
    sql = (
        "SELECT t.id FROM threads t LEFT JOIN triage g ON g.thread_id = t.id "
        f"WHERE {where} ORDER BY t.last_inbound_at DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [r["id"] for r in db.all(sql)]


def run_pending(db: DB, settings: Settings, rubric: Rubric, force: bool = False,
                limit: int | None = None, progress: Progress | None = None) -> dict[str, Any]:
    say = progress or (lambda _message: None)
    stats: dict[str, Any] = {"triaged": 0, "failed": 0, "cost": 0.0, "by_source": {}, "error": ""}
    if not _run_lock.acquire(blocking=False):
        stats["error"] = "A triage run is already in progress"
        return stats
    try:
        ids = pending_ids(db, settings, force=force, limit=limit)
        stats["queued"] = len(ids)
        if not ids:
            return stats
        workers = settings.triage_concurrency if settings.jev_ready else 1
        halted: TriageHalt | None = None
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker, db, settings, rubric, thread_id): thread_id for thread_id in ids}
            for future in as_completed(futures):
                try:
                    result = future.result()
                except TriageHalt as halt:
                    halted = halt
                    for other in futures:
                        other.cancel()
                    continue
                except Exception as error:  # noqa: BLE001 - one bad thread must not stop the batch
                    stats["failed"] += 1
                    stats["error"] = str(error)[:300]
                    continue
                if result:
                    stats["triaged"] += 1
                    stats["cost"] += result["cost"]
                    stats["by_source"][result["source"]] = stats["by_source"].get(result["source"], 0) + 1
                    if stats["triaged"] % 10 == 0:
                        say(f"Triage: {stats['triaged']}/{len(ids)}")
        if halted is not None:
            stats["error"] = str(halted)
        db.set_state("triage", "last_run_at", now_iso())
        db.set_state("triage", "last_error", stats["error"])
        return stats
    finally:
        _run_lock.release()


def draft_for_thread(db: DB, settings: Settings, thread_id: int, stance: str) -> str:
    if not (settings.writer_model and settings.jev_ready):
        raise writer.WriterError("Set WRITER_MODEL (and OPENROUTER_API_KEY) in .env to draft replies")
    ctx = load_context(db, thread_id)
    if ctx is None:
        raise writer.WriterError("thread not found")
    sender = sender_profile(ctx)
    text, cost = writer.draft_reply(
        settings, transcript(ctx), sender.get("name") or sender.get("handle") or "the sender",
        ctx["thread"]["channel"], stance, settings.owner_name,
    )
    _record_spend(db, "draft", settings.writer_model, cost, 0)
    db.run("UPDATE triage SET draft_reply=?, cost=cost+? WHERE thread_id=?", (text, cost, thread_id))
    return text


def stale_days(thread_row: Any) -> int | None:
    """Days an inbound message has been waiting on the owner."""
    if thread_row["last_direction"] != "in":
        return None
    last = parse_iso(thread_row["last_inbound_at"])
    return None if last is None else max(0, (now() - last).days)
