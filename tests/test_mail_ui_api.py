"""What the mail-style UI needs from the server: a local read marker, connector
filters, a few extra row fields, and unread counts for the navigation.

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_dealflow import WRITE, http, start_server  # noqa: E402

from dealflow import demo, queries  # noqa: E402
from dealflow.app import App  # noqa: E402
from dealflow.config import GmailAccount, Settings  # noqa: E402
from dealflow.ingest import Message, Participant, Thread, upsert_thread  # noqa: E402
from dealflow.triage import engine  # noqa: E402


def thread(channel: str, external_id: str, messages: list[tuple[str, str, str]], *, unread=None, account="",
           who="jo", attachments=None) -> Thread:
    """messages: (id, direction, text)"""
    items = [Message(external_id=mid, direction=direction, sent_at=f"2026-11-1{index}T10:00:00Z", text=text)
             for index, (mid, direction, text) in enumerate(messages)]
    if attachments and items:
        items[-1].attachments = attachments
    return Thread(channel=channel, external_id=external_id, participant=Participant(key=who, name=who.title(), handle=who),
                  messages=items, unread=unread, account=account)


class ReadStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            data_dir=Path(self.tmp.name),
            gmail_address="work@example.com", gmail_app_password="abcdefghijklmnop",
            gmail_more_accounts=[GmailAccount(2, "home@example.com", "ponmlkjihgfedcba", "Home")],
        )
        self.app = App(self.settings)
        self.db = self.app.db

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def unread(self, external_id: str) -> int:
        return self.db.one("SELECT unread FROM threads WHERE external_id=?", (external_id,))["unread"]

    def test_a_new_message_from_them_makes_it_unread_when_the_connector_cannot_say(self) -> None:
        upsert_thread(self.db, thread("instagram", "ig1", [("m1", "in", "hello")]))
        self.assertEqual(self.unread("ig1"), 1)
        self.db.run("UPDATE threads SET unread=0")  # the owner opened it
        upsert_thread(self.db, thread("instagram", "ig1", [("m1", "in", "hello")]))  # the same message again
        self.assertEqual(self.unread("ig1"), 0)
        upsert_thread(self.db, thread("instagram", "ig1", [("m1", "in", "hello"), ("m2", "out", "my reply")]))
        self.assertEqual(self.unread("ig1"), 0)  # my own reply does not make it unread
        upsert_thread(self.db, thread("instagram", "ig1", [("m3", "in", "following up")]))
        self.assertEqual(self.unread("ig1"), 1)

    def test_a_connector_that_knows_the_read_state_is_believed(self) -> None:
        upsert_thread(self.db, thread("email", "<a@x>", [("<a@x>", "in", "already read in the mail app")], unread=False))
        self.assertEqual(self.unread("<a@x>"), 0)
        upsert_thread(self.db, thread("email", "<b@x>", [("<b@x>", "in", "not opened yet")], unread=True))
        self.assertEqual(self.unread("<b@x>"), 1)

    def test_rows_carry_what_the_mail_list_shows(self) -> None:
        upsert_thread(self.db, thread("email", "<a@x>", [("<a@x>", "in", "Budget is $8,000. Please confirm by Friday."),
                                                         ("<b@x>", "out", "Will do")],
                             attachments=[{"type": "file", "name": "Brief.pdf"}], account="work@example.com"))
        upsert_thread(self.db, thread("instagram", "ig1", [("m1", "in", "hi")], who="fan"))
        engine.run_pending(self.db, self.settings, self.app.rubric)
        rows = {t["name"]: t for t in queries.list_threads(self.db, self.app.rubric, {"view": "all"})["threads"]}
        self.assertEqual((rows["Jo"]["replied"], rows["Jo"]["attachment"], rows["Jo"]["deadline"]), (True, "Brief.pdf", "by Friday"))
        self.assertEqual((rows["Fan"]["replied"], rows["Fan"]["attachment"], rows["Fan"]["deadline"]), (False, "", ""))

    def test_connector_filter_tells_gmail_from_proton(self) -> None:
        upsert_thread(self.db, thread("email", "<g1@x>", [("<g1@x>", "in", "to work")], account="work@example.com", who="a"))
        upsert_thread(self.db, thread("email", "<g2@x>", [("<g2@x>", "in", "to home")], account="home@example.com", who="b"))
        upsert_thread(self.db, thread("email", "<p1@x>", [("<p1@x>", "in", "to proton")], account="me@proton.me", who="c"))
        upsert_thread(self.db, thread("email", "<p2@x>", [("<p2@x>", "in", "older, no account")], who="d"))
        upsert_thread(self.db, thread("instagram", "ig1", [("m1", "in", "dm")], who="e"))
        upsert_thread(self.db, thread("linkedin", "li1", [("m1", "in", "inmail")], who="f"))
        gmail = ["work@example.com", "home@example.com"]

        def names(connector: str) -> list[str]:
            result = queries.list_threads(self.db, self.app.rubric, {"view": "all", "connector": connector}, gmail_accounts=gmail)
            return sorted(t["name"] for t in result["threads"])

        self.assertEqual(names("gmail"), ["A", "B"])
        self.assertEqual(names("email"), ["C", "D"])
        self.assertEqual(names("instagram"), ["E"])
        self.assertEqual(names("linkedin"), ["F"])
        self.assertEqual(names("nonsense"), [])
        # With no Gmail account set up, all email belongs to Proton Mail.
        result = queries.list_threads(self.db, self.app.rubric, {"view": "all", "connector": "email"}, gmail_accounts=[])
        self.assertEqual(len(result["threads"]), 4)
        self.assertEqual(queries.list_threads(self.db, self.app.rubric, {"view": "all", "connector": "gmail"}, gmail_accounts=[])["total"], 0)

    def test_a_hand_labelled_but_unsorted_conversation_stays_in_the_inbox(self) -> None:
        upsert_thread(self.db, thread("instagram", "ig1", [("m1", "in", "hello")]))
        thread_id = self.db.scalar("SELECT id FROM threads")
        # The owner picks a category before any sorting run: a triage row with no priority.
        self.db.run("INSERT INTO triage(thread_id, content_hash, source, triaged_at, category_override) "
                    "VALUES(?, '', 'manual', '2026-11-18T00:00:00Z', 'community')", (thread_id,))
        queue = queries.list_threads(self.db, self.app.rubric, {"view": "queue"})
        self.assertEqual([t["id"] for t in queue["threads"]], [thread_id])
        self.assertEqual(queries.nav_counts(self.db)["tiers"][""]["total"], 1)

    def test_navigation_counts(self) -> None:
        demo.seed(self.db)
        engine.run_pending(self.db, self.settings, self.app.rubric)
        nav = queries.nav_counts(self.db, ["work@example.com"])
        inbox = self.db.scalar("SELECT COUNT(*) FROM threads WHERE stage='new' AND inbound_count>0 AND unread=1")
        self.assertEqual(nav["inbox_unread"], inbox)
        self.assertGreater(inbox, 10)
        self.assertEqual(sum(tier["total"] for tier in nav["tiers"].values()), 22)
        self.assertEqual(sum(nav["labels"].values()), self.db.scalar("SELECT COUNT(*) FROM threads WHERE unread=1"))
        self.assertEqual(nav["connectors"]["gmail"], 0)
        self.assertEqual(nav["connectors"]["instagram"], self.db.scalar("SELECT COUNT(*) FROM threads WHERE channel='instagram' AND unread=1"))
        self.assertEqual(nav["connectors"]["email"], self.db.scalar("SELECT COUNT(*) FROM threads WHERE channel='email' AND unread=1"))
        # Passing on something takes it out of every badge, read or not.
        before = nav["labels"].get("noise", 0)
        self.assertGreater(before, 0)
        self.db.run("UPDATE threads SET stage='passed' WHERE id IN (SELECT thread_id FROM triage WHERE category='noise')")
        after = queries.nav_counts(self.db, [])
        self.assertEqual(after["labels"].get("noise", 0), 0)
        self.assertEqual(after["inbox_unread"], inbox - before)


class ReadStateApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Settings(data_dir=Path(self.tmp.name), port=0))
        self.base, self.stop = start_server(self.app)
        http(self.base, "POST", "/api/demo/seed", WRITE, {})

    def tearDown(self) -> None:
        self.stop()
        self.tmp.cleanup()

    def test_mark_read_and_unread_one_and_many(self) -> None:
        self.assertTrue(http(self.base, "GET", "/api/bootstrap")[1]["read_state"])
        status = http(self.base, "GET", "/api/status")[1]
        unread_before = status["nav"]["inbox_unread"]
        self.assertGreater(unread_before, 0)
        rows = http(self.base, "GET", "/api/threads?view=all")[1]
        self.assertIn("nav", rows)
        unread_rows = [t for t in rows["threads"] if t["unread"]]
        first = unread_rows[0]["id"]

        code, detail, _, _ = http(self.base, "PATCH", f"/api/threads/{first}", WRITE, {"unread": False})
        self.assertEqual((code, detail["unread"]), (200, False))
        self.assertEqual(http(self.base, "GET", "/api/status")[1]["nav"]["inbox_unread"], unread_before - 1)
        code, detail, _, _ = http(self.base, "PATCH", f"/api/threads/{first}", WRITE, {"unread": True})
        self.assertEqual(detail["unread"], True)
        self.assertEqual(http(self.base, "PATCH", f"/api/threads/{first}", {}, {"unread": False})[0], 403)  # needs the write header

        ids = [t["id"] for t in unread_rows[:5]]
        code, result, _, _ = http(self.base, "POST", "/api/bulk", WRITE, {"ids": ids, "unread": False})
        self.assertEqual((code, result["updated"]), (200, 5))
        after = {t["id"]: t["unread"] for t in http(self.base, "GET", "/api/threads?view=all")[1]["threads"]}
        self.assertEqual([after[i] for i in ids], [False] * 5)
        # A read marker changes nothing else about the conversation.
        untouched = http(self.base, "GET", f"/api/threads/{ids[0]}")[1]
        self.assertEqual((untouched["stage"], untouched["starred"]), ("new", False))
        self.assertEqual(http(self.base, "POST", "/api/bulk", WRITE, {"ids": ids, "unread": True, "starred": True})[0], 200)
        again = http(self.base, "GET", f"/api/threads/{ids[0]}")[1]
        self.assertEqual((again["unread"], again["starred"]), (True, True))


if __name__ == "__main__":
    unittest.main()
