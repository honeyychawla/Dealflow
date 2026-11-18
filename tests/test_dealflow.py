"""End-to-end checks that need no accounts:  python3 -m unittest discover -s tests -v

Jev is exercised against a local stand-in that speaks the documented
Decisions API shape, so the request we build and the response we parse are
both tested. Instagram, Proton and LinkedIn are tested at the parsing and
storage layer with synthetic input.
"""

from __future__ import annotations

import imaplib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_imap import FakeImapServer  # noqa: E402

from dealflow import demo, queries, settings_store  # noqa: E402
from dealflow.app import App  # noqa: E402
from dealflow.config import GmailAccount, InstagramAccount, Settings, load_settings  # noqa: E402
from dealflow.connectors.gmail import Gmail  # noqa: E402
from dealflow.connectors import linkedin  # noqa: E402
from dealflow.connectors.instagram import Instagram  # noqa: E402
from dealflow.connectors.protonmail import ProtonMail, strip_quoted  # noqa: E402
from dealflow.db import DB  # noqa: E402
from dealflow.ingest import merge_contacts, merge_suggestions  # noqa: E402
from dealflow.server import make_handler  # noqa: E402
from dealflow.triage import engine, heuristics, jev  # noqa: E402
from dealflow.triage.rubric import Rubric  # noqa: E402


class FakeOpenRouter:
    """Answers POST /api/alpha/decisions in the shape OpenRouter documents."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.fail_with: list[int] = []  # status codes to return before succeeding
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                if outer.fail_with:
                    status = outer.fail_with.pop(0)
                    payload = json.dumps({"error": {"code": status, "message": "simulated"}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                text = json.dumps(body["state"]).lower()
                answers = {}
                for name, question in body["questions"].items():
                    kind = question["type"]
                    if kind == "choice":
                        labels = list(question["criteria"])
                        pick = "brand_deal" if "sponsor" in text or "budget" in text else "noise" if "btc" in text else "community"
                        pick = pick if pick in labels else labels[0]
                        probabilities = {label: (0.84 if label == pick else 0.16 / (len(labels) - 1)) for label in labels}
                        answers[name] = {"type": "choice", "choice": pick, "probabilities": probabilities, "confidence": 0.84}
                    elif kind == "noul":
                        yes = {"opportunity": "budget" in text, "money": "budget" in text, "risk": "btc" in text}.get(name, False)
                        answers[name] = {"type": "noul", "noul": 0.96 if yes else 0.03}
                    elif kind == "score":
                        levels = len(question["criteria"])
                        top = levels - 1 if "budget" in text else 0
                        probabilities = {str(i): (0.99 if i == top else 0.01 / (levels - 1)) for i in range(levels)}
                        answers[name] = {
                            "type": "score", "score": float(top), "probabilities": probabilities,
                            "legend": {str(i): c for i, c in enumerate(question["criteria"])}, "confidence": 0.99,
                        }
                payload = json.dumps({
                    "id": "dec_test", "model": body["model"], "provider": "TypeSafe", "answers": answers,
                    "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.00002},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def start_server(app: App):
    """Serve `app` on a free loopback port. Returns (base_url, stop)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.settings.port = server.server_address[1]
    server.RequestHandlerClass = make_handler(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        app.db.close()

    return f"http://127.0.0.1:{app.settings.port}", stop


def http(base: str, method: str, path: str, headers: dict | None = None, body: dict | None = None):
    """(status, parsed JSON, raw text, response headers)"""
    request = urllib.request.Request(base + path, method=method, headers=headers or {},
                                     data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            text = response.read().decode()
            return response.status, json.loads(text or "{}"), text, response.headers
    except urllib.error.HTTPError as error:
        with error:
            text = error.read().decode()
            return error.code, json.loads(text or "{}"), text, error.headers


WRITE = {"X-Dealflow": "1"}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name), owner_emails=["me@owner.example"])
        self.db = DB(self.settings.db_path)
        self.rubric = Rubric.load(ROOT / "rubric.json")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()


class JevTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeOpenRouter()
        self.settings.openrouter_api_key = "sk-test"
        self.settings.openrouter_base_url = self.fake.url

    def tearDown(self) -> None:
        self.fake.close()
        super().tearDown()

    def test_request_shape_and_verdicts(self) -> None:
        demo.seed(self.db)
        stats = engine.run_pending(self.db, self.settings, self.rubric)
        self.assertEqual(stats["failed"], 0, stats)
        self.assertEqual(stats["by_source"].get("filter"), 2)  # newsletter + invoice never reach Jev
        self.assertEqual(stats["by_source"].get("jev"), 20)
        self.assertAlmostEqual(stats["cost"], 20 * 0.00002, places=7)

        request = self.fake.requests[0]
        self.assertEqual(request["path"], "/api/alpha/decisions")
        self.assertEqual(request["auth"], "Bearer sk-test")
        body = request["body"]
        self.assertEqual(body["model"], "typesafe/jev-1.14")
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertEqual(body["questions"]["category"]["type"], "choice")
        self.assertIn("brand_deal", body["questions"]["category"]["criteria"])
        self.assertEqual(set(body["questions"]["opportunity"]["criteria"]), {"true", "false"})
        self.assertIsInstance(body["questions"]["urgency"]["criteria"], list)
        self.assertIn("conversation", body["state"])

        lumen = self.db.one(
            "SELECT g.* FROM triage g JOIN threads t ON t.id=g.thread_id WHERE t.external_id='demo-ig-lumen.audio'"
        )
        self.assertEqual((lumen["category"], lumen["priority"], lumen["source"]), ("brand_deal", "P0", "jev"))
        scam = self.db.one(
            "SELECT g.* FROM triage g JOIN threads t ON t.id=g.thread_id WHERE t.external_id='demo-ig-crypto_alpha_signals'"
        )
        self.assertEqual((scam["category"], scam["priority"]), ("noise", "P3"))
        self.assertGreaterEqual(json.loads(scam["signals"])["risk"], 0.9)
        self.assertEqual(engine.pending_ids(self.db, self.settings), [])

    def test_retries_rate_limits_and_halts_on_bad_key(self) -> None:
        questions = {"q": {"type": "noul", "criteria": {"true": "yes", "false": "no"}}}
        self.fake.fail_with = [429, 503]
        with mock.patch.object(jev.time, "sleep"):
            response = jev.decide("sk-test", "typesafe/jev-1.14", "hello", questions, base_url=self.fake.url)
        self.assertEqual(len(self.fake.requests), 3)
        self.assertAlmostEqual(jev.noul_probability(response["answers"]["q"]), 0.03)

        self.fake.fail_with = [401]
        with self.assertRaises(jev.JevError) as caught:
            jev.decide("bad", "typesafe/jev-1.14", "hello", questions, base_url=self.fake.url)
        self.assertEqual(caught.exception.status, 401)

        demo.seed(self.db)
        self.fake.fail_with = [401] * 50
        stats = engine.run_pending(self.db, self.settings, self.rubric)
        self.assertIn("OPENROUTER_API_KEY", stats["error"])

    def test_rules_verdicts_are_upgraded_once_a_key_exists(self) -> None:
        demo.seed(self.db)
        keyless = Settings(data_dir=self.settings.data_dir)
        engine.run_pending(self.db, keyless, self.rubric)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM triage WHERE source='rules'"), 20)
        self.assertEqual(len(engine.pending_ids(self.db, self.settings)), 20)
        engine.run_pending(self.db, self.settings, self.rubric)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM triage WHERE source='rules'"), 0)


class ScoringTests(Base):
    def test_answer_normalisation(self) -> None:
        score = {"type": "score", "score": 1.99, "probabilities": {"0": 0, "1": 0.01, "2": 0.99}}
        self.assertAlmostEqual(jev.score_fraction(score, 3), 0.995)
        self.assertAlmostEqual(jev.score_fraction({"type": "score", "score": 1.0}, 3), 0.5)
        self.assertIsNone(jev.score_fraction({"type": "refusal"}, 3))
        chosen, probabilities, confidence = jev.choice_distribution(
            {"type": "choice", "choice": "payments", "probabilities": {"account": 0, "frontend": 0.16, "payments": 0.84}, "confidence": 0.75}
        )
        self.assertEqual((chosen, confidence, probabilities["frontend"]), ("payments", 0.75, 0.16))

    def test_refusal_and_close_calls_go_to_review(self) -> None:
        signals = self.rubric.signals_from_answers({"category": {"type": "refusal"}})
        self.assertTrue(self.rubric.verdict(signals).needs_review)

        torn = {
            "category": "brand_deal", "category_probs": {"brand_deal": 0.46, "vendor": 0.44, "noise": 0.1},
            "category_confidence": 0.46, "opportunity": 0.6, "value": 0.6, "credibility": 0.66, "urgency": 0.2,
            "personalised": 0.6, "reply_expected": 0.8, "money": 0.0, "risk": 0.0, "declined": [],
        }
        verdict = self.rubric.verdict(torn)
        self.assertTrue(verdict.needs_review)
        self.assertIn("Vendor pitch", verdict.review_reason)

        # Unsure between two kinds of junk: nobody needs to look at that.
        junk = {**torn, "category": "community", "category_probs": {"community": 0.5, "noise": 0.5},
                "opportunity": 0.02, "value": 0.0, "credibility": 0.3, "personalised": 0.1}
        self.assertFalse(self.rubric.verdict(junk).needs_review)

    def test_risk_sinks_an_otherwise_perfect_score(self) -> None:
        great = {
            "category": "brand_deal", "category_probs": {"brand_deal": 1.0}, "category_confidence": 0.99,
            "opportunity": 1.0, "value": 1.0, "credibility": 1.0, "urgency": 1.0, "personalised": 1.0,
            "reply_expected": 1.0, "money": 1.0, "risk": 0.0, "declined": [],
        }
        self.assertEqual(self.rubric.verdict(great).priority, "P0")
        self.assertEqual(self.rubric.verdict({**great, "risk": 0.98}).priority, "P3")

    def test_state_stays_inside_jevs_fast_mode_budget(self) -> None:
        payload = {"threads": [{
            "thread_id": "long", "participant": {"name": "Long Writer", "profile_url": "https://www.linkedin.com/in/long-writer/"},
            "messages": [{"from_me": i % 2 == 1, "sent_at": f"2026-11-{1 + i % 15:02d}T10:{i % 60:02d}:00Z", "text": f"message {i} " + "lorem ipsum " * 180}
                         for i in range(60)],
        }]}
        linkedin.ingest_payload(self.db, payload)
        ctx = engine.load_context(self.db, self.db.scalar("SELECT id FROM threads"))
        state = engine.build_state(self.rubric, ctx)
        self.assertLessEqual(len(json.dumps(state, ensure_ascii=False).encode()), jev.STATE_BYTE_BUDGET)
        self.assertIn("message 0 ", state["conversation"][0]["text"])  # the opening pitch survives
        self.assertIn("message 59 ", state["conversation"][-1]["text"])  # and so does the latest message

    def test_money_extraction(self) -> None:
        facts = heuristics.extract_facts("Budget is ₹6,50,000, with 50% up front. Or $40k. We have 2 lakh followers. Reply by Friday.")
        self.assertEqual(facts["amounts"], ["₹6,50,000", "$40k"])
        self.assertEqual(facts["deadlines"], ["by Friday"])


class LinkedInTests(Base):
    def payload(self, messages, **extra):
        return {"collected_at": "2026-11-16T12:00:00Z", "threads": [{
            "thread_id": "2-abc", "url": "https://www.linkedin.com/messaging/thread/2-abc/",
            "participant": {"name": "Asha Verma", "profile_url": "https://www.linkedin.com/in/asha-verma/", "headline": "VP Marketing at Kite"},
            "messages": messages, **extra,
        }]}

    def test_rereads_are_idempotent_even_when_times_drift(self) -> None:
        first = [
            {"from_me": False, "sent_at": "2026-11-14T09:15:00Z", "text": "Hi, are you open to a paid partnership?"},
            {"from_me": True, "sent_at": "2026-11-14T11:00:00Z", "text": "Possibly. What's the budget?"},
        ]
        self.assertEqual(linkedin.ingest_payload(self.db, self.payload(first))["new_messages"], 2)
        # Same thread read again: the agent resolved "Friday" a few hours differently, plus one new message.
        second = [
            {"from_me": False, "sent_at": "2026-11-14T03:45:00Z", "text": "Hi, are you open to a paid partnership?"},
            {"from_me": True, "sent_at": "2026-11-14T05:30:00Z", "text": "Possibly.  What's the budget?"},
            {"from_me": False, "sent_at": "2026-11-16T08:00:00Z", "text": "Around $20,000 for two posts."},
        ]
        self.assertEqual(linkedin.ingest_payload(self.db, self.payload(second))["new_messages"], 1)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM messages"), 3)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM threads"), 1)

    def test_repeated_identical_messages_are_kept(self) -> None:
        messages = [
            {"from_me": False, "sent_at": "2026-11-01T09:00:00Z", "text": "Following up"},
            {"from_me": False, "sent_at": "2026-11-10T09:00:00Z", "text": "Following up"},
        ]
        self.assertEqual(linkedin.ingest_payload(self.db, self.payload(messages))["new_messages"], 2)
        self.assertEqual(linkedin.ingest_payload(self.db, self.payload(messages))["new_messages"], 0)

    def test_preview_is_replaced_by_the_full_read(self) -> None:
        preview = self.payload([], preview="Hi, are you open to a paid partner…", last_message_at="2026-11-14T09:15:00Z", unread=True)
        linkedin.ingest_payload(self.db, preview)
        row = self.db.one("SELECT meta, unread FROM threads")
        self.assertTrue(json.loads(row["meta"])["partial"])
        self.assertEqual(row["unread"], 1)
        full = self.payload([{"from_me": False, "sent_at": "2026-11-14T09:15:00Z", "text": "Hi, are you open to a paid partnership?"}])
        linkedin.ingest_payload(self.db, full)
        self.assertEqual([r["text"] for r in self.db.all("SELECT text FROM messages")], ["Hi, are you open to a paid partnership?"])
        self.assertFalse(json.loads(self.db.one("SELECT meta FROM threads")["meta"])["partial"])

    def test_export_backfill_lands_on_the_same_thread_as_the_agent(self) -> None:
        linkedin.ingest_payload(self.db, self.payload([
            {"from_me": False, "sent_at": "2026-11-14T09:15:00Z", "text": "Hi, are you open to a paid partnership?"},
        ]))
        export = Path(self.tmp.name) / "messages.csv"
        export.write_text(
            "CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,RECIPIENT PROFILE URLS,DATE,SUBJECT,CONTENT,FOLDER,IS MESSAGE DRAFT\n"
            'EXPORT-ID-1,,Asha Verma,https://www.linkedin.com/in/asha-verma,Owner Name,https://www.linkedin.com/in/owner,2026-11-14 09:15:22 UTC,,"Hi, are you open to a paid partnership?",INBOX,No\n'
            'EXPORT-ID-1,,Owner Name,https://www.linkedin.com/in/owner,Asha Verma,https://www.linkedin.com/in/asha-verma,2026-11-14 11:00:05 UTC,,"<p>Possibly. What\'s the budget?</p>",INBOX,No\n'
            "EXPORT-ID-2,,Owner Name,https://www.linkedin.com/in/owner,Old Friend,https://www.linkedin.com/in/old-friend,2019-03-02 10:00:00 UTC,,Long time!,INBOX,No\n"
            "EXPORT-ID-3,,Bo Lee,https://www.linkedin.com/in/bo-lee,Owner Name,https://www.linkedin.com/in/owner,2026-11-15 10:00:00 UTC,,draft text,INBOX,Yes\n",
            encoding="utf-8",
        )
        stats = linkedin.import_export(self.db, self.settings, export, since_days=365)
        self.assertEqual(stats["owner"], "Owner Name")
        self.assertEqual(stats["skipped_old"], 1)
        self.assertEqual(stats["new_messages"], 1)  # only my reply is new; her message matched the agent's read
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM threads"), 1)
        rows = self.db.all("SELECT direction, text FROM messages ORDER BY sent_at")
        self.assertEqual([(r["direction"], r["text"]) for r in rows],
                         [("in", "Hi, are you open to a paid partnership?"), ("out", "Possibly. What's the budget?")])
        self.assertEqual(linkedin.import_export(self.db, self.settings, export)["new_messages"], 0)


def make_email(**headers: str) -> bytes:
    body = headers.pop("body", "Hello")
    message = EmailMessage()
    for key, value in headers.items():
        message[key.replace("_", "-")] = value
    message.set_content(body)
    return message.as_bytes()


class EmailTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.mail = ProtonMail(self.settings, self.db, self.rubric.email_prefilter)

    def store(self, raw: bytes, direction: str = "in", envelope: bytes = b"1 (UID 7 FLAGS () BODY[]<0> {100}"):
        item = self.mail._parse(envelope, raw, direction)
        self.assertIsNotNone(item)
        return item, self.mail._store(item)

    def test_threads_by_references_and_ignores_bridges_internal_ids(self) -> None:
        self.store(make_email(From="Jo Park <jo@acme.example>", To="me@owner.example", Subject="Sponsorship", Date="Mon, 09 Nov 2026 10:00:00 +0000",
                              Message_ID="<a1@acme.example>", body="We'd like to sponsor your newsletter."))
        self.store(make_email(From="me@owner.example", To="jo@acme.example", Subject="Re: Sponsorship", Date="Mon, 09 Nov 2026 12:00:00 +0000",
                              Message_ID="<b2@owner.example>", References="<a1@acme.example> <zz9@protonmail.internalid>", body="What's the budget?"), direction="out")
        item, _ = self.store(make_email(From="Jo Park <jo@acme.example>", To="me@owner.example", Subject="RE: Sponsorship", Date="Tue, 10 Nov 2026 09:00:00 +0000",
                                        Message_ID="<c3@acme.example>", In_Reply_To="<b2@owner.example>", References="<a1@acme.example> <b2@owner.example> <c3x@protonmail.internalid>",
                                        body="$8,000 per issue.\n\nOn Mon, 9 Nov 2026 at 12:00, Me <me@owner.example> wrote:\n> What's the budget?"))
        self.assertNotIn("<c3x@protonmail.internalid>", item["references"])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM threads"), 1)
        thread = self.db.one("SELECT * FROM threads")
        self.assertEqual((thread["subject"], thread["message_count"], thread["inbound_count"], thread["last_direction"]), ("Sponsorship", 3, 2, "in"))
        self.assertEqual(self.db.one("SELECT text FROM messages WHERE external_id='<c3@acme.example>'")["text"], "$8,000 per issue.")
        self.assertEqual(self.db.one("SELECT name, company FROM contacts")["company"], "Acme")

    def test_same_message_in_two_mailboxes_is_stored_once(self) -> None:
        raw = make_email(From="jo@acme.example", To="me@owner.example", Subject="Hi", Date="Mon, 09 Nov 2026 10:00:00 +0000", Message_ID="<dup@acme.example>")
        self.assertEqual(self.store(raw)[1].new_messages, 1)
        self.assertEqual(self.store(raw)[1].new_messages, 0)

    def test_newsletters_are_flagged_and_unread_comes_from_flags(self) -> None:
        item, _ = self.store(make_email(From="Growth <news@digest.example>", To="me@owner.example", Subject="Weekly", Date="Mon, 09 Nov 2026 10:00:00 +0000",
                                        Message_ID="<n1@digest.example>", List_Unsubscribe="<mailto:u@digest.example>"))
        self.assertEqual(item["automated"], "mailing list")
        self.assertTrue(json.loads(self.db.one("SELECT meta FROM threads")["meta"])["automated"])
        self.assertEqual(self.db.one("SELECT unread FROM threads")["unread"], 1)
        seen, _ = self.store(make_email(From="jo@acme.example", To="me@owner.example", Subject="Seen", Date="Mon, 09 Nov 2026 10:00:00 +0000", Message_ID="<s@acme.example>"),
                             envelope=rb"2 (UID 8 FLAGS (\Seen) BODY[]<0> {100}")
        self.assertTrue(seen["seen"])
        self.assertEqual(self.mail._parse(b"", make_email(From="x@noreply.example", To="me@owner.example", Subject="s", Message_ID="<q@x>"), "in")["automated"], "")
        self.assertIn("no-reply@", self.mail._parse(b"", make_email(From="no-reply@pay.example", To="me@owner.example", Subject="s", Message_ID="<r@x>"), "in")["automated"])

    def test_quote_stripping_keeps_the_new_text(self) -> None:
        self.assertEqual(strip_quoted("Yes, Thursday works.\n\n-----Original Message-----\nFrom: a@b.example\nold stuff"), "Yes, Thursday works.")
        self.assertEqual(strip_quoted("> only quoted"), "> only quoted")  # never return an empty body

    def test_tls_is_refused_without_a_pinned_certificate(self) -> None:
        self.settings.proton_username, self.settings.proton_bridge_password = "me@owner.example", "bridge-pass"
        with self.assertRaisesRegex(Exception, "No Bridge certificate is set"):
            self.mail._ssl_context()
        self.settings.proton_imap_host = "localhost"
        self.assertEqual(self.mail.host, "127.0.0.1")


class InstagramTests(Base):
    def test_sync_maps_conversations_and_is_incremental(self) -> None:
        self.settings.instagram_access_token = "IGTOKEN"
        ig = Instagram(self.settings, self.db)
        calls: list[str] = []
        me = {"user_id": "178414", "username": "my_brand"}
        conversations = {"data": [{
            "id": "conv1", "updated_time": "2026-11-15T18:42:10+0000",
            "participants": {"data": [{"username": "my_brand", "id": "178414"}, {"username": "jane.doe", "id": "999"}]},
        }]}
        messages = {"messages": {"data": [
            {"id": "m2", "created_time": "2026-11-15T18:42:10+0000", "from": {"username": "my_brand", "id": "178414"}, "message": "Sure, send details"},
            {"id": "m1", "created_time": "2026-11-15T18:40:02+0000", "from": {"username": "jane.doe", "id": "999"}, "message": "",
             "shares": {"data": [{"type": "ig_reel", "link": "https://www.instagram.com/reel/x/"}]}},
        ]}}
        profile = {"name": "Jane Doe", "username": "jane.doe", "follower_count": 52000, "is_verified_user": True, "is_user_follow_business": True}

        def fake_get(path, params=None, attempts=4):
            calls.append(path)
            if path.startswith("https://graph.instagram.com/refresh_access_token"):
                return {}
            return {"me": me, "me/conversations": conversations, "conv1": messages, "999": profile}[path]

        with mock.patch.object(ig, "_get", side_effect=fake_get):
            stats = ig.sync()
            self.assertEqual((stats["conversations_changed"], stats["new_messages"], stats["profiles"]), (1, 2, 1))
            calls.clear()
            again = Instagram(self.settings, self.db)
            with mock.patch.object(again, "_get", side_effect=fake_get):
                self.assertEqual(again.sync()["new_messages"], 0)
            self.assertNotIn("conv1", calls)  # untouched since the cursor: its messages are not refetched

        rows = self.db.all("SELECT direction, text, attachments FROM messages ORDER BY sent_at")
        self.assertEqual([r["direction"] for r in rows], ["in", "out"])
        self.assertEqual(json.loads(rows[0]["attachments"])[0]["type"], "reel")
        contact = self.db.one("SELECT c.name, i.handle, i.meta FROM contacts c JOIN identities i ON i.contact_id=c.id")
        self.assertEqual((contact["name"], contact["handle"]), ("Jane Doe", "@jane.doe"))
        self.assertEqual(json.loads(contact["meta"])["followers"], 52000)
        self.assertEqual(self.db.one("SELECT snippet, url FROM threads")["url"], "https://ig.me/m/jane.doe")


class InstagramFacebookRouteTests(Base):
    """A token starting with EAA is a Facebook token: DMs are read through the linked Page."""

    CONVERSATIONS = {"data": [{
        "id": "conv1", "updated_time": "2026-11-15T18:42:10+0000",
        "participants": {"data": [{"username": "my_brand", "id": "1784"}, {"username": "jane.doe", "id": "999"}]},
    }]}
    MESSAGES = {"messages": {"data": [
        {"id": "m1", "created_time": "2026-11-15T18:40:02+0000", "from": {"username": "jane.doe", "id": "999"}, "message": "Do you do brand collabs?"},
    ]}}

    def test_user_token_finds_the_page_and_uses_its_token(self) -> None:
        self.settings.instagram_access_token = "EAAuser-token"
        ig = Instagram(self.settings, self.db)
        seen: list[tuple[str, str]] = []
        replies = {
            "me": [],  # a user or system-user token: "me" is not a Page
            "me/accounts": {"data": [
                {"id": "PAGE0", "name": "Page without Instagram", "access_token": "EAApage0"},
                {"id": "PAGE1", "name": "My Brand", "access_token": "EAApage1",
                 "instagram_business_account": {"id": "1784", "username": "my_brand"}},
            ]},
            "PAGE1": {"id": "PAGE1"},
            "PAGE1/conversations": self.CONVERSATIONS,
            "conv1": self.MESSAGES,
            "999": {"name": "Jane Doe", "username": "jane.doe", "follower_count": 900},
        }

        def fake_get(path, params=None, attempts=4):
            seen.append((path, ig._token))
            return replies[path]

        with mock.patch.object(ig, "_get", side_effect=fake_get):
            info = ig.check()
            self.assertEqual((info["username"], info["route"], info["page"], info["sample_conversations"]),
                             ("my_brand", "facebook", "My Brand", 1))
            self.assertEqual(ig.base, "https://graph.facebook.com/v27.0")
            self.assertEqual(seen[-1], ("PAGE1/conversations", "EAApage1"))  # the Page's token, not the user's
            stats = ig.sync()
        self.assertEqual(stats["new_messages"], 1)
        self.assertEqual(self.db.one("SELECT direction, text FROM messages")["text"], "Do you do brand collabs?")
        self.assertEqual(self.db.get_state("instagram", "fb_page_id"), "PAGE1")

    def test_token_without_a_page_gets_an_exact_diagnosis(self) -> None:
        self.settings.instagram_access_token = "EAAads-only-token"
        ig = Instagram(self.settings, self.db)
        granted = ["ads_management", "ads_read", "instagram_basic", "instagram_manage_comments",
                   "instagram_content_publish", "instagram_manage_messages", "public_profile"]
        replies = {
            "me": [], "me/accounts": {"data": []},
            "me/permissions": {"data": [{"permission": name, "status": "granted"} for name in granted]},
        }
        with mock.patch.object(ig, "_get", side_effect=lambda path, params=None, attempts=4: replies[path]):
            with self.assertRaises(Exception) as caught:
                ig.check()
        message = str(caught.exception)
        self.assertIn("No Instagram account ID is set", message)
        self.assertIn("Unified login", message)
        self.assertIn("it lacks pages_show_list, pages_manage_metadata", message)
        self.assertNotIn("EAAads-only-token", message)

    def test_unified_login_reads_through_the_instagram_account_id(self) -> None:
        """Business Integration System User token + account id: no Page, no /me."""
        from dealflow.connectors.instagram import GraphError

        account_id = "17841400000000001"
        self.settings.instagram_access_token = "EAAsystem-user-token"
        self.settings.instagram_user_id = account_id
        ig = Instagram(self.settings, self.db)
        seen: list[tuple[str, str]] = []
        conversations = {"data": [{
            "id": "conv1", "updated_time": "2026-11-15T18:42:10+0000",
            "participants": {"data": [{"username": "my_brand", "id": account_id}, {"username": "jane.doe", "id": "999"}]},
        }]}

        def fake_get(path, params=None, attempts=4):
            seen.append((path, ig._token))
            if path == "999":
                raise GraphError("(#230) User consent is required to access user profile", code=230, status=400)
            if path in ("me", "me/accounts"):
                raise AssertionError("Unified Login must not depend on /me")
            return {
                account_id: {"id": account_id, "username": "my_brand", "media_count": 42},
                f"{account_id}/conversations": conversations,
                "conv1": self.MESSAGES,
            }[path]

        with mock.patch.object(ig, "_get", side_effect=fake_get):
            info = ig.check()
            self.assertEqual((info["username"], info["route"], info["sample_conversations"]), ("my_brand", "unified", 1))
            self.assertEqual(ig.base, "https://graph.facebook.com/v27.0")
            self.assertEqual(seen[-1], (f"{account_id}/conversations", "EAAsystem-user-token"))
            stats = ig.sync()
            self.assertEqual((stats["new_messages"], stats["profiles"]), (1, 0))  # the consent error is not fatal
            self.assertEqual(Instagram(self.settings, self.db)._conversations_path(), "me/conversations")  # before routing
        row = self.db.one("SELECT m.direction, m.text, t.url FROM messages m JOIN threads t ON t.id = m.thread_id")
        self.assertEqual((row["direction"], row["text"], row["url"]), ("in", "Do you do brand collabs?", "https://ig.me/m/jane.doe"))
        self.assertEqual(self.db.get_state("instagram", "fb_route"), "unified")

    def test_a_wrong_account_id_is_named_in_the_error(self) -> None:
        from dealflow.connectors.instagram import GraphError

        self.settings.instagram_access_token = "EAAsystem-user-token"
        self.settings.instagram_user_id = "123"
        ig = Instagram(self.settings, self.db)

        def fake_get(path, params=None, attempts=4):
            if path == "123":
                raise GraphError("Unsupported get request", code=100, status=400)
            return {"me": [], "me/accounts": {"data": []},
                    "me/permissions": {"data": [{"permission": "instagram_basic", "status": "granted"}]}}[path]

        with mock.patch.object(ig, "_get", side_effect=fake_get):
            with self.assertRaises(Exception) as caught:
                ig.check()
        self.assertIn("The ID 123 could not be read", str(caught.exception))
        self.assertIn("missing the permission(s) needed to read Instagram DMs: instagram_manage_messages", str(caught.exception))

    def test_token_prefix_picks_the_host(self) -> None:
        from dealflow.connectors.instagram import token_kind
        self.assertEqual([token_kind(t) for t in ("IGAAabc", "EAAPxyz", "something-else")], ["instagram", "facebook", "unknown"])
        for token, host in (("IGAAabc", "graph.instagram.com"), ("EAAPxyz", "graph.facebook.com")):
            self.settings.instagram_access_token = token
            self.settings.instagram_api_host = "graph.instagram.com"
            ig = Instagram(self.settings, self.db)
            ig._load_token()
            self.assertIn(host, ig.base)


class NetTests(unittest.TestCase):
    def test_trust_store_is_never_empty_and_verification_stays_on(self) -> None:
        import ssl

        from dealflow import net

        context = net.ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertNotEqual(net.trust_source(), "none")
        self.assertIsNone(net.explain_tls_failure("connection refused"))
        self.assertIn("could not be verified", net.explain_tls_failure("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed"))


class AppTests(Base):
    def test_views_merge_and_resurfacing(self) -> None:
        demo.seed(self.db)
        engine.run_pending(self.db, self.settings, self.rubric)
        listing = queries.list_threads(self.db, self.rubric, {"view": "queue"})
        self.assertGreater(listing["total"], 5)
        self.assertEqual(listing["threads"][0]["priority"], "P0")
        self.assertEqual(queries.list_threads(self.db, self.rubric, {"view": "queue", "q": "Tinkerly"})["threads"][0]["name"], "Nisha Kapoor")

        priya = {r["channel"]: r["id"] for r in self.db.all(
            "SELECT i.channel, c.id FROM contacts c JOIN identities i ON i.contact_id=c.id WHERE c.name='Priya Nair'")}
        self.assertEqual(set(priya), {"email", "linkedin"})  # same name, different channels: suggested, not auto-merged
        self.assertEqual(merge_suggestions(self.db, priya["email"])[0]["id"], priya["linkedin"])
        merge_contacts(self.db, priya["email"], priya["linkedin"])
        self.assertEqual(self.db.scalar("SELECT COUNT(DISTINCT channel) FROM threads WHERE contact_id=?", (priya["email"],)), 2)

        # A conversation I passed on comes back when a new message makes it important again.
        thread = self.db.one("SELECT id FROM threads WHERE external_id='demo-ig-lumen.audio'")["id"]
        self.db.run("UPDATE threads SET stage='passed' WHERE id=?", (thread,))
        from dealflow.ingest import Message, Participant, Thread, upsert_thread
        upsert_thread(self.db, Thread(channel="instagram", external_id="demo-ig-lumen.audio", participant=Participant(key="demo-lumen.audio"),
                                      messages=[Message(external_id="new-1", direction="in", sent_at="2026-11-16T12:00:00Z",
                                                        text="We can raise the budget to ₹9,00,000 if you confirm this week. Sponsored campaign.")]))
        self.assertEqual(engine.pending_ids(self.db, self.settings), [thread])
        engine.run_pending(self.db, self.settings, self.rubric)
        self.assertEqual(self.db.one("SELECT stage FROM threads WHERE id=?", (thread,))["stage"], "new")

        self.assertEqual(demo.clear(self.db), 22)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM contacts"), 0)

    def test_http_api_rejects_foreign_hosts_and_unmarked_writes(self) -> None:
        settings = Settings(data_dir=Path(self.tmp.name) / "web", port=0)
        settings.data_dir.mkdir()
        app = App(settings)
        base, stop = start_server(app)

        def call(method: str, path: str, headers: dict | None = None, body: dict | None = None):
            status, data, _text, response_headers = http(base, method, path, headers, body)
            return status, data, response_headers

        try:
            status, boot, headers = call("GET", "/api/bootstrap")
            self.assertEqual(status, 200)
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertEqual(call("GET", "/api/bootstrap", {"Host": "evil.example"})[0], 403)  # DNS rebinding
            self.assertEqual(call("POST", "/api/demo/seed", body={})[0], 403)  # no X-Dealflow header
            self.assertEqual(call("POST", "/api/demo/seed", {"X-Dealflow": "1", "Origin": "https://evil.example"}, {})[0], 403)
            self.assertEqual(call("POST", "/api/demo/seed", {"X-Dealflow": "1"}, {})[0], 200)
            listing = call("GET", "/api/threads?view=queue")[1]
            first = listing["threads"][0]["id"]
            status, detail, _ = call("PATCH", f"/api/threads/{first}", {"X-Dealflow": "1"}, {"stage": "shortlist", "priority": "P1"})
            self.assertEqual((status, detail["stage"], detail["priority"], detail["overridden"]), (200, "shortlist", "P1", True))
            self.assertEqual(call("PATCH", f"/api/threads/{first}", {"X-Dealflow": "1"}, {"stage": "bogus"})[0], 400)
            self.assertEqual(call("GET", "/../rubric.json")[0], 404)
        finally:
            stop()


class SettingsPageTests(unittest.TestCase):
    """The Settings page: edits .env and rubric.json, never leaks a secret, applies without a restart."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        self.env = folder / ".env"
        self.env.write_text("# my own notes\nOWNER_NAME=Old Name\nUNRELATED_TOOL=keep # trailing comment\n", encoding="utf-8")
        self.rubric_file = folder / "rubric.json"
        self.rubric_file.write_text((ROOT / "rubric.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.fake = FakeOpenRouter()
        # A developer's shell may export real keys; they must not leak into, or lock, this test.
        names = [field["key"] for field in settings_store.FIELDS] + ["OPENROUTER_BASE_URL"]
        self.patch = mock.patch.dict("os.environ", {}, clear=False)
        self.patch.start()
        for name in names:
            os.environ.pop(name, None)
        settings = load_settings(self.env)
        settings.data_dir = folder / "data"
        settings.data_dir.mkdir()
        settings.rubric_file = self.rubric_file
        self.app = App(settings)
        self.base, self.stop = start_server(self.app)

    def tearDown(self) -> None:
        self.stop()
        self.fake.close()
        self.patch.stop()
        self.tmp.cleanup()

    def post(self, path: str, body: dict):
        return http(self.base, "POST", path, WRITE, body)

    def field(self, payload: dict, key: str) -> dict:
        return next(f for f in payload["fields"] if f["key"] == key)

    def test_save_validate_and_never_echo_secrets(self) -> None:
        status, payload, text, _ = http(self.base, "GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertFalse(self.field(payload, "OPENROUTER_API_KEY")["set"])
        self.assertEqual(self.field(payload, "OWNER_NAME")["value"], "Old Name")
        self.assertEqual([s["id"] for s in payload["sections"]], ["sorting", "instagram", "gmail", "email", "linkedin", "general"])

        secret = "sk-or-v1-supersecretvalue-9f3a"
        status, payload, text, _ = self.post("/api/settings", {"values": {
            "OPENROUTER_API_KEY": secret, "OWNER_NAME": "New Name", "SYNC_INTERVAL_MINUTES": "30", "AUTO_TRIAGE": False,
        }})
        self.assertEqual(status, 200, text)
        self.assertNotIn(secret, text)
        self.assertNotIn("supersecret", text)
        key = self.field(payload, "OPENROUTER_API_KEY")
        self.assertEqual((key["set"], key["hint"]), (True, "9f3a"))
        self.assertNotIn("value", key)

        written = self.env.read_text(encoding="utf-8")
        self.assertIn("# my own notes", written)
        self.assertIn("UNRELATED_TOOL=keep # trailing comment", written)
        self.assertIn('OWNER_NAME="New Name"', written)
        self.assertIn(f"OPENROUTER_API_KEY={secret}", written)
        self.assertEqual(written.count("OWNER_NAME="), 1)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)

        # Applied to the running app without a restart.
        self.assertTrue(self.app.settings.jev_ready)
        self.assertEqual((self.app.settings.owner_name, self.app.settings.sync_interval_minutes, self.app.settings.auto_triage),
                         ("New Name", 30, False))
        self.assertEqual(http(self.base, "GET", "/api/status")[1]["triage"]["engine"], "jev")

        for bad in ({"PROTON_IMAP_PORT": "99999"}, {"NOT_A_SETTING": "x"}, {"OWNER_NAME": "a\nb"},
                    {"PROTON_IMAP_SECURITY": "plaintext"}, {"PROTON_CERT_PATH": "/no/such/cert.pem"}, {"JEV_MODEL": "has space"},
                    {"INSTAGRAM_USER_ID": "@my.handle"}):
            self.assertEqual(self.post("/api/settings", {"values": bad})[0], 400, bad)
        self.assertEqual(http(self.base, "POST", "/api/settings", {}, {"values": {"OWNER_NAME": "x"}})[0], 403)  # CSRF guard

        status, payload, _, _ = self.post("/api/settings", {"values": {"OPENROUTER_API_KEY": None}})
        self.assertFalse(self.field(payload, "OPENROUTER_API_KEY")["set"])
        self.assertNotIn("OPENROUTER_API_KEY", self.env.read_text(encoding="utf-8"))
        self.assertFalse(self.app.settings.jev_ready)

    def test_environment_variables_lock_a_field(self) -> None:
        os.environ["JEV_MODEL"] = "typesafe/jev-1.13"
        payload = http(self.base, "GET", "/api/settings")[1]
        self.assertTrue(self.field(payload, "JEV_MODEL")["locked"])
        self.assertEqual(self.post("/api/settings", {"values": {"JEV_MODEL": "typesafe/jev-1.14"}})[0], 400)

    def test_connection_tests_report_in_plain_words(self) -> None:
        result = self.post("/api/settings/test", {"target": "sorting"})[1]
        self.assertEqual((result["ok"], result["message"]), (False, "No OpenRouter API key is saved yet."))
        self.assertFalse(self.post("/api/settings/test", {"target": "instagram"})[1]["ok"])

        with self.env.open("a", encoding="utf-8") as handle:
            handle.write(f"OPENROUTER_BASE_URL={self.fake.url}\n")
        self.post("/api/settings", {"values": {"OPENROUTER_API_KEY": "sk-or-v1-test-key-0001"}})
        result = self.post("/api/settings/test", {"target": "sorting"})[1]
        self.assertTrue(result["ok"], result)
        self.assertIn("typesafe/jev-1.14 answered a test question", result["message"])
        self.assertEqual(self.fake.requests[-1]["auth"], "Bearer sk-or-v1-test-key-0001")

        self.fake.fail_with = [401]
        result = self.post("/api/settings/test", {"target": "sorting"})[1]
        self.assertFalse(result["ok"])
        self.assertIn("401", result["message"])

        self.post("/api/settings", {"values": {"PROTON_USERNAME": "me@owner.example", "PROTON_BRIDGE_PASSWORD": "bridge-pass-123"}})
        result = self.post("/api/settings/test", {"target": "email"})[1]
        self.assertFalse(result["ok"])
        self.assertIn("No Bridge certificate is set", result["message"])
        self.assertEqual(self.post("/api/settings/test", {"target": "nope"})[0], 400)

    def test_policy_editing_and_linkedin_import(self) -> None:
        status, payload, text, _ = self.post("/api/settings/rubric", {
            "owner_context": "I run a 2M-subscriber AI education channel and angel invest in developer tools.",
            "category_values": {"vendor": 0.5}, "category_criteria": {"vendor": "Anyone selling me a service."},
            "tiers": {"P0": 80, "P1": 60, "P2": 40},
        })
        self.assertEqual(status, 200, text)
        self.assertEqual(self.app.rubric.value("vendor"), 0.5)
        self.assertEqual(self.app.rubric.tier_for(75), "P1")
        self.assertIn("angel invest", self.app.rubric.owner_context)
        saved = json.loads(self.rubric_file.read_text(encoding="utf-8"))
        self.assertIn("_readme", saved)  # the file's own notes survive an edit
        self.assertEqual(saved["questions"]["category"]["type"], "choice")
        self.assertEqual(self.post("/api/settings/rubric", {"tiers": {"P0": 40, "P1": 60, "P2": 80}})[0], 400)
        self.assertEqual(self.post("/api/settings/rubric", {"category_values": {"vendor": 7}})[0], 400)
        self.assertEqual(self.post("/api/settings/rubric", {"owner_context": "  "})[0], 400)

        csv_text = (
            "CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,RECIPIENT PROFILE URLS,DATE,SUBJECT,CONTENT,FOLDER,IS MESSAGE DRAFT\n"
            "C1,,Asha Verma,https://www.linkedin.com/in/asha-verma,Owner Name,https://www.linkedin.com/in/owner,"
            + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            + ",,We would like to sponsor your channel. Budget is $12000.,INBOX,No\n"
        )
        status, stats, text, _ = self.post("/api/linkedin/import", {"csv": csv_text, "since_days": 30})
        self.assertEqual((status, stats.get("conversations"), stats.get("new_messages")), (200, 1, 1), text)
        self.assertEqual(self.post("/api/linkedin/import", {"csv": "nothing useful"})[0], 400)
        self.assertEqual(self.post("/api/linkedin/import", {})[0], 400)


# ---------------------------------------------------------------------------
# Several accounts
# ---------------------------------------------------------------------------


class GraphStub:
    """Stands in for Meta's Graph API across several Instagram accounts.

    `accounts` maps a slot number to that account's canned replies (path -> reply).
    A reply that is an Exception is raised instead of returned.
    """

    def __init__(self, accounts: dict[int, dict]) -> None:
        self.accounts = accounts
        self.calls: list[tuple[int, str]] = []

    def __call__(self, connector, path, params=None, attempts=4):
        self.calls.append((connector.account.slot, path))
        if path.startswith("https://graph.instagram.com/refresh_access_token"):
            return {}
        reply = self.accounts[connector.account.slot][path]
        if isinstance(reply, Exception):
            raise reply
        return reply


def conversation(conversation_id: str, me: tuple[str, str], other: tuple[str, str], updated: str = "2026-11-15T18:42:10+0000") -> dict:
    return {"id": conversation_id, "updated_time": updated,
            "participants": {"data": [{"username": me[0], "id": me[1]}, {"username": other[0], "id": other[1]}]}}


def one_message(message_id: str, sender: tuple[str, str], text: str, at: str = "2026-11-15T18:40:02+0000") -> dict:
    return {"messages": {"data": [{"id": message_id, "created_time": at, "from": {"username": sender[0], "id": sender[1]}, "message": text}]}}


class MultiInstagramTests(unittest.TestCase):
    """Two Instagram accounts: one Instagram Login token, one Unified Login token with an account id."""

    PERSONAL = ("personal", "1001")
    BRAND = ("brand", "17841400000000002")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            data_dir=Path(self.tmp.name),
            instagram_access_token="IGAApersonal-token",
            instagram_more_accounts=[InstagramAccount(2, "EAAbrand-token", self.BRAND[1], "Brand")],
        )
        self.app = App(self.settings)
        jane_on_personal, jane_on_brand = ("jane.doe", "999"), ("jane.doe", "888")  # one person, one id per account
        self.stub = GraphStub({
            1: {
                "me": {"user_id": self.PERSONAL[1], "username": self.PERSONAL[0]},
                "me/conversations": {"data": [conversation("convA", self.PERSONAL, jane_on_personal)]},
                "convA": one_message("mA1", jane_on_personal, "Loved your last video!"),
                "999": {},
            },
            2: {
                self.BRAND[1]: {"id": self.BRAND[1], "username": self.BRAND[0], "media_count": 12},
                f"{self.BRAND[1]}/conversations": {"data": [
                    conversation("convB", self.BRAND, jane_on_brand),
                    conversation("convC", self.BRAND, ("acme.co", "777"), updated="2026-11-14T10:00:00+0000"),
                ]},
                "convB": one_message("mB1", jane_on_brand, "We would like to sponsor a video. Budget is $8,000."),
                "convC": one_message("mC1", ("acme.co", "777"), "Paid partnership proposal attached", at="2026-11-14T10:00:00+0000"),
                "888": {}, "777": {},
            },
        })
        self.patch = mock.patch.object(Instagram, "_get", autospec=True, side_effect=self.stub)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.app.db.close()
        self.tmp.cleanup()

    def test_each_account_syncs_into_its_own_lane(self) -> None:
        db = self.app.db
        result = self.app.sync("instagram")
        self.assertNotIn("error", result)
        self.assertEqual((result["new_messages"], [a["name"] for a in result["accounts"]]), (3, ["@personal", "Brand"]))

        rows = {r["external_id"]: r for r in db.all("SELECT * FROM threads")}
        self.assertEqual({k: v["account"] for k, v in rows.items()},
                         {"convA": "1001", "convB": self.BRAND[1], "convC": self.BRAND[1]})
        # Separate cursors: one account catching up never moves the other's.
        self.assertEqual(db.get_state("instagram", "updated_cursor"), "2026-11-15T18:42:10Z")
        self.assertEqual(db.get_state("instagram:2", "updated_cursor"), "2026-11-15T18:42:10Z")
        self.assertEqual((db.get_state("instagram", "account_id"), db.get_state("instagram:2", "account_id")), ("1001", self.BRAND[1]))
        self.assertEqual(db.get_state("instagram:2", "fb_route"), "unified")
        self.assertIsNone(db.get_state("instagram", "fb_route") or None)

        # Jane wrote to both accounts: one person, two conversations.
        self.assertEqual(rows["convA"]["contact_id"], rows["convB"]["contact_id"])
        self.assertNotEqual(rows["convA"]["contact_id"], rows["convC"]["contact_id"])
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM contacts"), 2)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM identities WHERE handle='@jane.doe'"), 2)

        names = {a["id"]: (a["name"], a["connected"]) for a in self.app.known_accounts()}
        self.assertEqual(names, {"1001": ("@personal", True), self.BRAND[1]: ("Brand", True)})
        status = next(c for c in self.app.status()["connectors"] if c["name"] == "instagram")
        self.assertEqual((status["threads"], status["last_status"], [a["threads"] for a in status["accounts"]]), (3, "ok", [1, 2]))

        listing = queries.list_threads(db, self.app.rubric, {"view": "all", "account": self.BRAND[1]})
        self.assertEqual(sorted(t["name"] for t in listing["threads"]), ["@acme.co", "@jane.doe"])
        self.assertEqual(listing["facets"]["account"], {"1001": 1, self.BRAND[1]: 2})
        by_name = {t["name"]: t for t in queries.list_threads(db, self.app.rubric, {"view": "all"})["threads"]
                   if t["account"] == self.BRAND[1]}
        self.assertEqual((by_name["@jane.doe"]["account_count"], by_name["@acme.co"]["account_count"]), (2, 1))
        detail = queries.thread_detail(db, self.app.rubric, rows["convA"]["id"])
        self.assertEqual([i["handle"] for i in detail["identities"]], ["@jane.doe"])  # shown once, not per account
        self.assertEqual(detail["other_threads"][0]["account"], self.BRAND[1])

        # Jev is told which account received it, and that she also wrote to the other one.
        engine.run_pending(db, self.settings, self.app.rubric)
        state = engine.build_state(self.app.rubric, engine.load_context(db, rows["convB"]["id"]))
        self.assertEqual(state["received_on_owner_account"], "@brand (Brand)")
        self.assertEqual(state["sender"]["also_wrote_to_other_accounts_of_owner"], 1)

        calls_before = len(self.stub.calls)
        again = self.app.sync("instagram")
        self.assertEqual((again["new_messages"], "error" in again), (0, False))
        self.assertNotIn((2, "convB"), self.stub.calls[calls_before:])  # nothing changed: messages not refetched

    def test_one_broken_account_does_not_stop_the_other(self) -> None:
        from dealflow.connectors.instagram import GraphError

        self.stub.accounts[2][self.BRAND[1]] = GraphError("Meta says this token is invalid or expired.", code=190, status=400)
        result = self.app.sync("instagram")
        self.assertIn("Brand: Meta says this token is invalid or expired.", result["error"])
        self.assertEqual(result["new_messages"], 1)  # the personal account still synced
        self.assertEqual([r["external_id"] for r in self.app.db.all("SELECT external_id FROM threads")], ["convA"])
        status = next(c for c in self.app.status()["connectors"] if c["name"] == "instagram")
        self.assertEqual([a["last_status"] for a in status["accounts"]], ["ok", "error"])
        self.assertEqual(status["last_status"], "error")
        self.assertTrue(status["last_error"].startswith("Brand: "))
        runs = {r["connector"]: r["status"] for r in self.app.db.all("SELECT connector, status FROM sync_runs")}
        self.assertEqual(runs, {"instagram": "ok", "instagram:2": "error"})

    def test_the_same_account_twice_is_refused(self) -> None:
        self.settings.instagram_more_accounts = [InstagramAccount(2, "IGAAsame-account-again", "", "Copy")]
        self.stub.accounts[2] = dict(self.stub.accounts[1])
        result = self.app.sync("instagram")
        self.assertIn("Copy: This is the same account as @personal", result["error"])
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM threads"), 1)
        self.assertEqual([a["name"] for a in self.app.known_accounts()], ["@personal"])  # the duplicate's label does not take over

    def test_a_conversation_id_seen_by_two_accounts_stays_two_threads(self) -> None:
        # Two of the owner's own accounts messaging each other: each side lists the conversation.
        self.stub.accounts[2][f"{self.BRAND[1]}/conversations"] = {"data": [conversation("convA", self.BRAND, self.PERSONAL)]}
        self.stub.accounts[2]["convA"] = one_message("mA1", self.PERSONAL, "note to my other account")
        self.stub.accounts[2]["1001"] = {}
        self.app.sync("instagram")
        rows = {r["external_id"]: r["account"] for r in self.app.db.all("SELECT external_id, account FROM threads")}
        self.assertEqual(rows, {"convA": "1001", f"convA@{self.BRAND[1]}": self.BRAND[1]})

    def test_removing_an_account_keeps_its_conversations_labelled(self) -> None:
        self.app.sync("instagram")
        self.settings.instagram_more_accounts = []
        accounts = {a["id"]: a for a in self.app.known_accounts()}
        self.assertEqual((accounts[self.BRAND[1]]["name"], accounts[self.BRAND[1]]["connected"]), ("Brand", False))
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM threads WHERE account=?", (self.BRAND[1],)), 2)
        self.assertEqual(len(self.app.sync("instagram")["accounts"]), 1)


class MigrationTests(unittest.TestCase):
    """A database made before several accounts existed is upgraded in place, once, without losing anything."""

    def old_database(self, folder: Path, state: dict[str, str]) -> Path:
        path = folder / "dealflow.db"
        DB(path).close()
        raw = sqlite3.connect(path)
        raw.executescript("DROP INDEX threads_account; ALTER TABLE threads DROP COLUMN account; DROP TABLE accounts;")
        now = "2026-11-17T10:00:00Z"
        for channel, external_id, is_demo in (("instagram", "c1", 0), ("instagram", "c2", 0), ("instagram", "demo-ig-x", 1), ("email", "<m@x>", 0)):
            raw.execute("INSERT INTO threads(channel, external_id, is_demo, stage, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                        (channel, external_id, is_demo, "shortlist", now, now))
        for key, value in state.items():
            raw.execute("INSERT INTO sync_state(connector, key, value) VALUES('instagram', ?, ?)", (key, value))
        raw.commit()
        raw.close()
        return path

    def test_known_account_id_labels_old_conversations_at_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.old_database(Path(folder), {"fb_ig_id": "17841400000000009", "username": "old.account", "updated_cursor": "2026-11-17T09:00:00Z"})
            for _ in range(2):  # opening it again must change nothing
                db = DB(path)
                rows = {r["external_id"]: (r["account"], r["stage"]) for r in db.all("SELECT external_id, account, stage FROM threads")}
                self.assertEqual(rows, {"c1": ("17841400000000009", "shortlist"), "c2": ("17841400000000009", "shortlist"),
                                        "demo-ig-x": ("", "shortlist"), "<m@x>": ("", "shortlist")})
                self.assertEqual([(r["key"], r["username"]) for r in db.all("SELECT key, username FROM accounts")],
                                 [("17841400000000009", "old.account")])
                self.assertEqual(db.get_state("instagram", "updated_cursor"), "2026-11-17T09:00:00Z")  # no re-download
                self.assertEqual(db.get_state("instagram", "account_id"), "17841400000000009")
                db.close()

    def test_username_only_setups_are_adopted_by_the_same_account_only(self) -> None:
        for token_username, expected in (("old.account", "5005"), ("someone.else", "")):
            with tempfile.TemporaryDirectory() as folder:
                path = self.old_database(Path(folder), {"username": "old.account"})
                db = DB(path)
                self.assertEqual(db.get_state("instagram", "adopt_orphans_for"), "old.account")
                self.assertEqual(db.scalar("SELECT COUNT(*) FROM threads WHERE account<>''"), 0)
                settings = Settings(data_dir=Path(folder), instagram_access_token="IGAAtoken")
                connector = Instagram(settings, db)
                with mock.patch.object(connector, "_get", side_effect=lambda path, params=None, attempts=4:
                                       {} if path.startswith("https://") else {"user_id": "5005", "username": token_username}):
                    connector.identify()
                accounts = {r["external_id"]: r["account"] for r in db.all("SELECT external_id, account FROM threads")}
                self.assertEqual((accounts["c1"], accounts["c2"], accounts["demo-ig-x"], accounts["<m@x>"]), (expected, expected, "", ""))
                self.assertEqual(db.get_state("instagram", "adopt_orphans_for"), "")
                db.close()


def gmail_message(sender: str, to: str, subject: str, message_id: str, body: str = "Hello", **headers: str) -> bytes:
    return make_email(From=sender, To=to, Subject=subject, Date="Mon, 16 Nov 2026 10:00:00 +0000",
                      Message_ID=message_id, body=body, **headers)


class GmailTests(unittest.TestCase):
    """Two Gmail accounts, each against its own stand-in IMAP server."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.work = FakeImapServer("work@example.com", "abcdefghijklmnop")
        self.home = FakeImapServer("home@example.com", "ponmlkjihgfedcba", sent_name="[Gmail]/Gesendet")
        self.servers = {"work@example.com": self.work, "home@example.com": self.home}
        self.settings = Settings(
            data_dir=Path(self.tmp.name),
            gmail_address="work@example.com", gmail_app_password="abcd efgh ijkl mnop", gmail_label="Work",
            gmail_more_accounts=[GmailAccount(2, "home@example.com", "ponmlkjihgfedcba", "")],
            owner_emails=["work@example.com", "home@example.com"],
        )
        self.app = App(self.settings)
        servers = self.servers
        self.patch = mock.patch.object(Gmail, "_open", lambda connector: imaplib.IMAP4("127.0.0.1", servers[connector.account].port, timeout=10))
        self.patch.start()

        inbox, sent = self.work.mailboxes["INBOX"], self.work.mailboxes["[Gmail]/Sent Mail"]
        inbox.add(gmail_message("Jo Park <jo@acme.example>", "work@example.com", "Sponsorship", "<a1@acme.example>",
                                "We'd like to sponsor your newsletter. Budget is $8,000."), flags="\\Seen")
        inbox.add(gmail_message("Growth <news@digest.example>", "work@example.com", "Weekly digest", "<n1@digest.example>",
                                List_Unsubscribe="<mailto:u@digest.example>"), primary=False)
        inbox.add(gmail_message("Mia Chen <mia@studio.example>", "work@example.com", "Podcast invite", "<p1@studio.example>",
                                "Would you join our podcast?"), flags_after_body=True, flags="\\Seen")
        sent.add(gmail_message("work@example.com", "jo@acme.example", "Re: Sponsorship", "<b2@example.com>",
                               "Happy to talk. What are the dates?", References="<a1@acme.example>"), flags="\\Seen")
        self.home.mailboxes["INBOX"].add(gmail_message("Jo Park <jo@acme.example>", "home@example.com", "Personal note", "<h1@acme.example>", "Trying your other address too."))
        self.home.mailboxes["INBOX"].add(gmail_message("work@example.com", "home@example.com", "Fwd to self", "<s1@example.com>", "note"))

    def tearDown(self) -> None:
        self.patch.stop()
        self.work.close()
        self.home.close()
        self.app.db.close()
        self.tmp.cleanup()

    def test_two_accounts_sync_read_only_into_one_inbox(self) -> None:
        db = self.app.db
        result = self.app.sync("gmail")
        self.assertNotIn("error", result)
        self.assertEqual([a["name"] for a in result["accounts"]], ["Work", "home@example.com"])
        self.assertEqual(result["new_messages"], 5)  # 3 inbox + 1 sent at work, 1 at home; the note between own addresses is skipped

        threads = {r["subject"]: r for r in db.all("SELECT * FROM threads")}
        self.assertEqual({k: (v["channel"], v["account"]) for k, v in threads.items()}, {
            "Sponsorship": ("email", "work@example.com"), "Weekly digest": ("email", "work@example.com"),
            "Podcast invite": ("email", "work@example.com"), "Personal note": ("email", "home@example.com"),
        })
        sponsorship = threads["Sponsorship"]
        self.assertEqual((sponsorship["message_count"], sponsorship["inbound_count"], sponsorship["last_direction"]), (2, 1, "out"))
        self.assertEqual(sponsorship["url"], "https://mail.google.com/mail/u/work@example.com/#search/rfc822msgid:b2%40example.com")
        self.assertTrue(json.loads(threads["Weekly digest"]["meta"])["automated"])
        self.assertEqual((threads["Weekly digest"]["unread"], threads["Podcast invite"]["unread"]), (1, 0))  # FLAGS read before and after the body
        # Jo wrote to both addresses: one contact, two conversations on two accounts.
        self.assertEqual(sponsorship["contact_id"], threads["Personal note"]["contact_id"])

        # Nothing that could change a mailbox was ever sent, and the app password went out without its spaces.
        for server in self.servers.values():
            self.assertLessEqual(server.verbs, {"CAPABILITY", "LOGIN", "LIST", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"})
            self.assertTrue(all("BODY.PEEK[]" in c for c in server.commands if c.upper().startswith("UID FETCH")))
        self.assertIn('EXAMINE "[Gmail]/Gesendet"', self.home.commands)  # the translated Sent mailbox was found

        accounts = {a["id"]: (a["channel"], a["name"]) for a in self.app.known_accounts()}
        self.assertEqual(accounts, {"work@example.com": ("email", "Work"), "home@example.com": ("email", "home@example.com")})
        status = {c["name"]: c for c in self.app.status()["connectors"]}
        self.assertEqual((status["gmail"]["threads"], [a["threads"] for a in status["gmail"]["accounts"]], status["email"]["threads"]), (4, [3, 1], 0))
        self.assertEqual(db.get_state("gmail", "last_uid:INBOX"), "3")
        self.assertEqual(db.get_state("gmail:2", "last_uid:INBOX"), "2")
        self.assertEqual(db.get_state("gmail:2", "last_uid:[Gmail]/Gesendet"), None)

        # A repeat sync asks only for newer UIDs and stores nothing twice.
        self.work.commands.clear()
        self.work.mailboxes["INBOX"].add(gmail_message("Jo Park <jo@acme.example>", "work@example.com", "RE: Sponsorship", "<c3@acme.example>",
                                                       "Dec 5 to Dec 12.", In_Reply_To="<b2@example.com>", References="<a1@acme.example> <b2@example.com>"))
        again = self.app.sync("gmail")
        self.assertEqual(again["new_messages"], 1)
        self.assertIn("UID SEARCH UID 4:*", self.work.commands)
        self.assertIn("UID SEARCH UID 2:*", self.work.commands)  # Sent: nothing newer, and the server's "n:*" echo is ignored
        row = db.one("SELECT message_count, last_direction FROM threads WHERE subject='Sponsorship'")
        self.assertEqual((row["message_count"], row["last_direction"]), (3, "in"))
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM messages"), 6)

    def test_a_wrong_app_password_only_fails_that_account(self) -> None:
        self.settings.gmail_more_accounts = [GmailAccount(2, "home@example.com", "wrongwrongwrongw", "Home")]
        result = self.app.sync("gmail")
        self.assertIn("Home: Gmail rejected the sign-in for home@example.com", result["error"])
        self.assertIn("myaccount.google.com/apppasswords", result["error"])
        self.assertNotIn("wrongwrong", result["error"])
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM threads WHERE account='work@example.com'"), 3)

        self.home.login_reply = "[ALERT] Application-specific password required: https://support.google.com/accounts/answer/185833 (Failure)"
        self.assertIn("wants an app password", self.app.sync("gmail")["error"])

    def test_primary_only_filters_the_inbox_but_not_sent_mail(self) -> None:
        self.settings.gmail_primary_only = True
        self.settings.gmail_more_accounts = []
        self.app.sync("gmail")
        self.assertEqual(sorted(r["subject"] for r in self.app.db.all("SELECT subject FROM threads")), ["Podcast invite", "Sponsorship"])
        searches = [c for c in self.work.commands if c.upper().startswith("UID SEARCH")]
        self.assertTrue(searches[0].endswith('X-GM-RAW "category:primary"'))
        self.assertNotIn("X-GM-RAW", searches[1])

    def test_check_and_duplicate_addresses(self) -> None:
        info = Gmail(self.settings, self.app.db, self.settings.gmail_accounts[0]).check()
        self.assertEqual((info["address"], info["inbox_messages"], info["sent_mailbox"], info["gmail_extensions"]),
                         ("work@example.com", 3, "[Gmail]/Sent Mail", True))
        self.settings.gmail_more_accounts = [GmailAccount(2, "work@example.com", "abcdefghijklmnop", "Again")]
        result = self.app.sync("gmail")
        self.assertIn("Again: This is the same account as Work", result["error"])


class AccountSettingsTests(unittest.TestCase):
    """The Settings page for accounts: add, change and remove them without leaking a secret."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        self.env = folder / ".env"
        self.env.write_text("INSTAGRAM_ACCESS_TOKEN=EAAfirst-token-000000001\nINSTAGRAM_USER_ID=17841400000000001\n", encoding="utf-8")
        self.patch = mock.patch.dict("os.environ", {}, clear=False)
        self.patch.start()
        for name in list(os.environ):
            if name.startswith(("INSTAGRAM", "GMAIL", "PROTON", "OPENROUTER", "OWNER")):
                os.environ.pop(name)
        settings = load_settings(self.env)
        settings.data_dir = folder / "data"
        settings.data_dir.mkdir()
        self.app = App(settings)
        self.base, self.stop = start_server(self.app)

    def tearDown(self) -> None:
        self.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def post(self, path: str, body: dict):
        return http(self.base, "POST", path, WRITE, body)

    def test_add_edit_and_remove_accounts(self) -> None:
        status, payload, text, _ = http(self.base, "GET", "/api/settings")
        instagram = payload["accounts"]["instagram"]
        self.assertEqual([(s["slot"], s["values"]["user_id"], s["secrets"]["token"]) for s in instagram["slots"]],
                         [(1, "17841400000000001", {"set": True, "hint": "0001"})])
        self.assertEqual(instagram["key_names"]["2"]["token"], "INSTAGRAM_2_ACCESS_TOKEN")
        self.assertEqual(payload["accounts"]["gmail"]["slots"], [])
        self.assertNotIn("EAAfirst", text)

        status, payload, text, _ = self.post("/api/settings", {"values": {
            "INSTAGRAM_2_ACCESS_TOKEN": "EAAsecond-token-000000002", "INSTAGRAM_2_USER_ID": "17841400000000002", "INSTAGRAM_2_LABEL": "Brand",
            "GMAIL_ADDRESS": "Work@Example.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
            "GMAIL_2_ADDRESS": "home@example.com", "GMAIL_2_APP_PASSWORD": "ponmlkjihgfedcba", "GMAIL_2_LABEL": "Home",
        }})
        self.assertEqual(status, 200, text)
        for secret in ("EAAsecond", "EAAfirst", "abcdefgh", "ponmlkji"):
            self.assertNotIn(secret, text)
        self.assertEqual([(a.slot, a.user_id, a.label) for a in self.app.settings.instagram_accounts],
                         [(1, "17841400000000001", ""), (2, "17841400000000002", "Brand")])
        self.assertEqual([(a.slot, a.address, a.password, a.label) for a in self.app.settings.gmail_accounts],
                         [(1, "work@example.com", "abcdefghijklmnop", ""), (2, "home@example.com", "ponmlkjihgfedcba", "Home")])
        self.assertEqual(self.app.settings.owner_emails, ["work@example.com", "home@example.com"])
        written = self.env.read_text(encoding="utf-8")
        self.assertIn("INSTAGRAM_ACCESS_TOKEN=EAAfirst-token-000000001", written)  # account 1 untouched
        self.assertIn("GMAIL_APP_PASSWORD=abcdefghijklmnop", written)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        gmail = payload["accounts"]["gmail"]["slots"]
        self.assertEqual([(s["slot"], s["values"], s["secrets"]["password"]["hint"]) for s in gmail],
                         [(1, {"address": "work@example.com", "label": ""}, "mnop"), (2, {"address": "home@example.com", "label": "Home"}, "edcba"[-4:])])
        connectors = {c["name"]: c for c in payload["status"]["connectors"]}
        self.assertEqual((connectors["instagram"]["configured"], len(connectors["instagram"]["accounts"]),
                          connectors["gmail"]["configured"], len(connectors["gmail"]["accounts"])), (True, 2, True, 2))

        for bad in ({"INSTAGRAM_2_USER_ID": "brand"}, {"GMAIL_3_ADDRESS": "nope"}, {"GMAIL_3_APP_PASSWORD": "short"},
                    {"INSTAGRAM_9_ACCESS_TOKEN": "EAAx"}, {"GMAIL_2_LABEL": "x" * 41}):
            self.assertEqual(self.post("/api/settings", {"values": bad})[0], 400, bad)

        # Removing account 2 leaves account 1 exactly as it was.
        status, payload, _, _ = self.post("/api/settings", {"values": {
            "INSTAGRAM_2_ACCESS_TOKEN": None, "INSTAGRAM_2_USER_ID": None, "INSTAGRAM_2_LABEL": None}})
        self.assertEqual([s["slot"] for s in payload["accounts"]["instagram"]["slots"]], [1])
        self.assertEqual([a.slot for a in self.app.settings.instagram_accounts], [1])
        self.assertNotIn("INSTAGRAM_2_", self.env.read_text(encoding="utf-8"))

    def test_connection_tests_address_one_account(self) -> None:
        self.assertEqual(self.post("/api/settings/test", {"target": "gmail", "slot": 1})[1]["message"],
                         "Press Sign in with Google, or save the address and an app password first.")
        self.assertEqual(self.post("/api/settings/test", {"target": "instagram", "slot": 2})[1]["message"],
                         "No access token is saved for this account yet.")
        self.assertEqual(self.post("/api/settings/test", {"target": "instagram", "slot": "x"})[0], 400)

        server = FakeImapServer("work@example.com", "abcdefghijklmnop")
        server.mailboxes["INBOX"].add(gmail_message("jo@acme.example", "work@example.com", "Hi", "<a@x>"))
        self.post("/api/settings", {"values": {"GMAIL_ADDRESS": "work@example.com", "GMAIL_APP_PASSWORD": "abcdefghijklmnop",
                                               "GMAIL_2_ADDRESS": "work@example.com", "GMAIL_2_APP_PASSWORD": "abcdefghijklmnop"}})
        try:
            with mock.patch.object(Gmail, "_open", lambda connector: imaplib.IMAP4("127.0.0.1", server.port, timeout=10)):
                result = self.post("/api/settings/test", {"target": "gmail", "slot": 1})[1]
                self.assertEqual(result, {"ok": False, "message": "work@example.com is already added as account 2. Each address can only be added once: remove one of the two."})
                self.post("/api/settings", {"values": {"GMAIL_2_ADDRESS": None, "GMAIL_2_APP_PASSWORD": None}})
                result = self.post("/api/settings/test", {"target": "gmail", "slot": 1})[1]
                self.assertEqual(result, {"ok": True, "message": "Signed in to work@example.com. The inbox holds 1 message. Sent mail is read from “[Gmail]/Sent Mail”."})
                # Starting a sync from the page runs it as a background job and stores the mail.
                self.assertTrue(self.post("/api/sync", {"connector": "gmail"})[1]["started"])
                for _ in range(100):
                    if not http(self.base, "GET", "/api/status")[1]["job"]["running"]:
                        break
                    threading.Event().wait(0.05)
                status = http(self.base, "GET", "/api/status")[1]
                self.assertEqual(status["job"]["result"]["gmail"]["new_messages"], 1)
                self.assertEqual([a["name"] for a in status["accounts"]], ["work@example.com"])
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
