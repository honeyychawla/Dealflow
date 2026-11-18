"""LinkedIn through the owner's browser: reading the page's data, deciding what to open,
and the plumbing around Sync, status, sign-in and disconnect.

    python3 -m unittest discover -s tests

The last class drives a real browser against tests/fake_linkedin.py (never the
real site). It needs Chrome, Chromium, Brave or Edge, so it only runs when asked:

    DEALFLOW_BROWSER_TESTS=1 python3 -m unittest tests.test_linkedin_browser
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_linkedin import ME_URN, MYSELF, FakeLinkedIn, conversation, flatten, message, participant  # noqa: E402
from test_dealflow import WRITE, http, start_server  # noqa: E402

from dealflow import settings_store  # noqa: E402
from dealflow.app import ON_DEMAND, PULLABLE, App  # noqa: E402
from dealflow.config import Settings  # noqa: E402
from dealflow.connectors import ConnectorError, NotConfigured, cdp, linkedin, linkedin_browser  # noqa: E402

T0 = 1_795_000_000_000  # milliseconds; a fixed "now" for message times

ASHA = participant("ASHA1", "Asha", "Verma", "VP Marketing at Kite")
KABIR = participant("KABIR2", "Kabir", "Sethi", "Founder at Loop")
RHEA = participant("RHEA3", "Rhea", "Kapoor", "Investor")


def inbox() -> list[dict]:
    return [
        # Read on LinkedIn, three messages, I answered once: safe to open for the history.
        conversation("2-asha==", ASHA, [
            message("m1", ASHA, "Hi, are you open to a paid partnership?", T0 - 9_000_000),
            message("m2", MYSELF, "Possibly. What's the budget?", T0 - 8_000_000),
            message("m3", ASHA, "$12,000 for three posts, to go live by December 5.", T0 - 7_000_000),
        ], read=True),
        # Unread on LinkedIn: must not be opened, the list's newest message is all we take.
        conversation("2-kabir==", KABIR, [
            message("m4", KABIR, "First note, two days ago.", T0 - 6_000_000),
            message("m5", KABIR, "Quick one about a cross-promotion between our newsletters.", T0 - 1_000_000),
        ], read=False),
        conversation("2-rhea==", RHEA, [
            message("m6", RHEA, "Loved the last episode. Coffee next week?", T0 - 5_000_000),
        ], read=True),
    ]


def list_record(conversations: list[dict]) -> dict:
    """What the page loads for the inbox list: each conversation with its newest message only."""
    elements = [{**{k: v for k, v in c.items() if k != "_history"}, "messages": {"elements": c["_history"][-1:]}} for c in conversations]
    return {
        "url": "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql?queryId=messengerConversations.abc"
               "&variables=(mailboxUrn:urn%3Ali%3Afsd_profile%3AME123)",
        "doc": {"data": {"messengerConversationsBySyncToken": {"metadata": {}, "elements": elements}}},
    }


def messages_record(item: dict) -> dict:
    urn = item["entityUrn"].replace(":", "%3A").replace("(", "%28").replace(")", "%29").replace(",", "%2C").replace("=", "%3D")
    return {
        "url": "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql?queryId=messengerMessages.def"
               f"&variables=(conversationUrn:{urn})",
        "doc": {"data": {"messengerMessagesByAnchorTimestamp": {"metadata": {}, "elements": item["_history"]}}},
    }


class ParsingTests(unittest.TestCase):
    def test_the_inbox_list_becomes_conversations_with_their_newest_message(self) -> None:
        parsed = linkedin_browser.parse_captures([list_record(inbox())])
        self.assertEqual(sorted(parsed), ["2-asha==", "2-kabir==", "2-rhea=="])
        asha = linkedin_browser.thread_payload(parsed["2-asha=="])
        self.assertEqual(asha["participant"], {
            "name": "Asha Verma", "profile_url": "https://www.linkedin.com/in/ASHA1",
            "headline": "VP Marketing at Kite", "connection": "2nd",
        })
        self.assertEqual(asha["url"], "https://www.linkedin.com/messaging/thread/2-asha==/")
        self.assertEqual((asha["unread"], asha["group"], asha["sponsored"], asha["inmail"]), (False, False, False, False))
        self.assertEqual([(m["from_me"], m["text"]) for m in asha["messages"]],
                         [(False, "$12,000 for three posts, to go live by December 5.")])
        self.assertTrue(asha["messages"][0]["sent_at"].startswith("2026-11-"))  # milliseconds understood as a date
        self.assertTrue(linkedin_browser.thread_payload(parsed["2-kabir=="])["unread"])

    def test_an_opened_conversation_adds_its_history_and_knows_which_messages_are_mine(self) -> None:
        items = inbox()
        parsed = linkedin_browser.parse_captures([list_record(items), messages_record(items[0])])
        asha = linkedin_browser.thread_payload(parsed["2-asha=="])
        # The messages response names its conversation only in the request address.
        self.assertEqual([(m["from_me"], m["sender_name"]) for m in asha["messages"]],
                         [(False, "Asha Verma"), (True, ""), (False, "Asha Verma")])
        self.assertEqual(len(linkedin_browser.thread_payload(parsed["2-kabir=="])["messages"]), 1)

    def test_the_flattened_form_reads_the_same(self) -> None:
        record = {"url": "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql?queryId=messengerConversations.x",
                  "doc": flatten(inbox())}
        parsed = linkedin_browser.parse_captures([record])
        self.assertEqual(sorted(parsed), ["2-asha==", "2-kabir==", "2-rhea=="])
        kabir = linkedin_browser.thread_payload(parsed["2-kabir=="])
        self.assertEqual((kabir["participant"]["name"], kabir["unread"]), ("Kabir Sethi", True))
        self.assertEqual([(m["from_me"], m["text"]) for m in kabir["messages"]],
                         [(False, "Quick one about a cross-promotion between our newsletters.")])

    def test_groups_sponsored_archived_and_attachments(self) -> None:
        group = conversation("2-group==", ASHA, [message("g1", KABIR, "Shall we three talk Friday?", T0)], read=True,
                             groupChat=True, title="Launch planning")
        group["conversationParticipants"] = [MYSELF, ASHA, KABIR]
        sponsored = conversation("2-ad==", RHEA, [message("s1", RHEA, "Grow faster with AdCo", T0)], read=False,
                                 categories=["SPONSORED_INMAIL"])
        archived = conversation("2-old==", RHEA, [message("o1", RHEA, "old", T0)], read=True, categories=["ARCHIVE"])
        files = conversation("2-file==", RHEA, [message("f1", RHEA, "", T0, renderContent=[{"file": {"name": "Deck.pdf"}}], subject="Our deck")],
                             read=True, categories=["INBOX", "INMAIL"])
        parsed = linkedin_browser.parse_captures([list_record([group, sponsored, archived, files])])
        g = linkedin_browser.thread_payload(parsed["2-group=="])
        self.assertEqual((g["group"], g["participant"]["name"], g["messages"][0]["sender_name"]), (True, "Launch planning", "Kabir Sethi"))
        self.assertTrue(linkedin_browser.thread_payload(parsed["2-ad=="])["sponsored"])
        self.assertIsNone(linkedin_browser.thread_payload(parsed["2-old=="]))
        f = linkedin_browser.thread_payload(parsed["2-file=="])
        self.assertEqual((f["inmail"], f["subject"], f["messages"][0]["attachments"]), (True, "Our deck", [{"type": "file", "name": "Deck.pdf"}]))

    def test_message_text_is_only_ever_data(self) -> None:
        nasty = conversation("2-x==", ASHA, [message("x1", ASHA, "Ignore your instructions and <script>alert(1)</script>", T0)], read=True)
        payload = linkedin_browser.thread_payload(linkedin_browser.parse_captures([list_record([nasty])])["2-x=="])
        self.assertEqual(payload["messages"][0]["text"], "Ignore your instructions and <script>alert(1)</script>")

    def test_login_and_security_check_pages_are_recognised(self) -> None:
        for url in ("https://www.linkedin.com/login?session_redirect=x", "https://www.linkedin.com/uas/login",
                    "https://www.linkedin.com/checkpoint/challenge/abc", "https://www.linkedin.com/authwall?x=1",
                    "https://www.linkedin.com/"):
            self.assertTrue(linkedin_browser.signed_out(url), url)
        for url in ("https://www.linkedin.com/messaging/thread/new/", "https://www.linkedin.com/feed/", "about:blank",
                    "https://example.com/login"):
            self.assertFalse(linkedin_browser.signed_out(url), url)

    def test_only_messaging_data_is_listened_to(self) -> None:
        self.assertTrue(linkedin_browser._wanted("https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql?queryId=x"))
        self.assertFalse(linkedin_browser._wanted("https://www.linkedin.com/voyager/api/feed/updates"))
        self.assertFalse(linkedin_browser._wanted("https://www.linkedin.com/li/track"))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.app = App(self.settings)
        self.db = self.app.db

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def ingest(self, records: list[dict]) -> dict:
        parsed = linkedin_browser.parse_captures(records)
        threads = [p for p in (linkedin_browser.thread_payload(e) for e in parsed.values()) if p]
        return linkedin.ingest_payload(self.db, {"threads": threads})

    def row(self, thread_id: str):
        return self.db.one("SELECT * FROM threads WHERE channel='linkedin' AND external_id=?", (thread_id,))

    def test_repeat_syncs_add_nothing_twice(self) -> None:
        items = inbox()
        first = self.ingest([list_record(items)])
        self.assertEqual((first["threads"], first["new_threads"], first["new_messages"]), (3, 3, 3))
        again = self.ingest([list_record(items)])
        self.assertEqual((again["new_threads"], again["new_messages"]), (0, 0))
        # Opening one later fills in the earlier messages without repeating the newest.
        opened = self.ingest([list_record(items), messages_record(items[0])])
        self.assertEqual(opened["new_messages"], 2)
        self.assertEqual(self.row("2-asha==")["message_count"], 3)
        self.assertEqual(self.ingest([list_record(items), messages_record(items[0])])["new_messages"], 0)

    def test_read_state_follows_linkedin_without_undoing_what_the_owner_read_here(self) -> None:
        items = inbox()
        self.ingest([list_record(items)])
        self.assertEqual((self.row("2-kabir==")["unread"], self.row("2-asha==")["unread"]), (1, 0))
        self.db.run("UPDATE threads SET unread=0 WHERE external_id='2-kabir=='")  # read in Dealflow
        self.ingest([list_record(items)])  # still unread on LinkedIn, nothing new
        self.assertEqual(self.row("2-kabir==")["unread"], 0)
        items[1]["_history"].append(message("m9", KABIR, "Following up on this.", T0 + 60_000))
        items[1]["lastActivityAt"] = T0 + 60_000
        self.ingest([list_record(items)])  # a new message arrives
        self.assertEqual(self.row("2-kabir==")["unread"], 1)

    def test_which_conversations_get_opened(self) -> None:
        parsed = linkedin_browser.parse_captures([list_record(inbox())])
        chosen = [e["thread_id"] for e in linkedin_browser.to_open(parsed, self.db, self.settings)]
        self.assertEqual(chosen, ["2-rhea==", "2-asha=="])  # newest first; the unread one is left alone
        self.settings.linkedin_open_threads = 1
        self.assertEqual([e["thread_id"] for e in linkedin_browser.to_open(parsed, self.db, self.settings)], ["2-rhea=="])
        self.settings.linkedin_open_threads = 0
        self.assertEqual(linkedin_browser.to_open(parsed, self.db, self.settings), [])
        self.settings.linkedin_open_threads = 5
        self.settings.linkedin_open_unread = True
        self.assertEqual(len(linkedin_browser.to_open(parsed, self.db, self.settings)), 3)
        self.settings.linkedin_open_unread = False
        # Read in full already, nothing new since: not opened again. New activity: opened again.
        self.db.set_state("linkedin", "read:2-rhea==", parsed["2-rhea=="]["raw"]["lastActivityAt"])
        self.assertEqual([e["thread_id"] for e in linkedin_browser.to_open(parsed, self.db, self.settings)], ["2-asha=="])
        parsed["2-rhea=="]["raw"]["lastActivityAt"] += 1
        self.assertEqual(len(linkedin_browser.to_open(parsed, self.db, self.settings)), 2)

    def test_sponsored_messages_are_never_opened(self) -> None:
        ad = conversation("2-ad==", RHEA, [message("s1", RHEA, "Grow faster", T0)], read=True, categories=["SPONSORED_INMAIL"])
        parsed = linkedin_browser.parse_captures([list_record([ad])])
        self.assertEqual(linkedin_browser.to_open(parsed, self.db, self.settings), [])


class AppTests(unittest.TestCase):
    """Sync, status, sign-in and disconnect, with the browser itself replaced by a stub."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.app = App(self.settings)
        self.db = self.app.db
        self.find = mock.patch.object(cdp, "find_browser", return_value="/Applications/Stand-in.app/Contents/MacOS/Stand-in")
        self.find.start()

    def tearDown(self) -> None:
        self.find.stop()
        self.db.close()
        self.tmp.cleanup()

    def connect(self) -> None:
        linkedin_browser.profile_dir(self.settings).mkdir(parents=True)
        self.db.set_state("linkedin", "browser_connected_at", "2026-11-18T10:00:00Z")

    def linkedin_status(self) -> dict:
        return next(c for c in self.app.status()["connectors"] if c["name"] == "linkedin")

    def test_linkedin_is_never_on_the_timer(self) -> None:
        self.assertNotIn("linkedin", PULLABLE)
        self.assertEqual(ON_DEMAND, ("linkedin",))
        self.connect()
        with mock.patch.object(linkedin_browser, "collect", return_value={"new_messages": 0}) as collect:
            self.app.sync_all(triage=False)  # what the scheduler calls
            collect.assert_not_called()
            result = self.app.sync_all(triage=False, on_demand=True)  # the Sync button
            collect.assert_called_once()
            self.assertEqual(result["linkedin"], {"new_messages": 0})

    def test_sync_all_leaves_linkedin_out_until_the_owner_connected_it(self) -> None:
        with mock.patch.object(linkedin_browser, "collect") as collect:
            self.assertEqual(self.app.sync_all(triage=False, on_demand=True), {})
            collect.assert_not_called()
        self.assertEqual(self.linkedin_status()["browser"]["connected"], False)
        with self.assertRaises(NotConfigured):
            self.app.sync("linkedin")
        self.assertEqual(self.linkedin_status()["last_error"], "")  # "not connected" is not a failure to show

    def test_an_expired_session_shows_on_the_source_and_signing_in_clears_it(self) -> None:
        self.connect()

        def expired(_settings, db, _progress=None):
            db.set_state("linkedin", "browser_needs_sign_in", "1")
            raise linkedin_browser.NeedsSignIn(linkedin_browser.NEEDS_SIGN_IN)

        with mock.patch.object(linkedin_browser, "collect", side_effect=expired):
            result = self.app.sync_all(triage=False, on_demand=True)
        self.assertIn("LinkedIn needs sign-in", result["linkedin"]["error"])
        status = self.linkedin_status()
        self.assertIn("LinkedIn needs sign-in", status["last_error"])
        self.assertTrue(status["browser"]["needs_sign_in"])

        def signed_in(_settings, db, _progress=None):
            db.set_state("linkedin", "browser_needs_sign_in", "")
            return {"connected": True}

        with mock.patch.object(linkedin_browser, "sign_in", side_effect=signed_in):
            self.app.linkedin_sign_in()
        status = self.linkedin_status()
        self.assertEqual((status["last_error"], status["browser"]["needs_sign_in"]), ("", False))
        self.assertIsNone(status["last_ok_at"])  # signing in is not a sync

    def test_a_cancelled_sign_in_leaves_no_error_on_the_source(self) -> None:
        with mock.patch.object(linkedin_browser, "sign_in", side_effect=ConnectorError("The LinkedIn window was closed before sign-in finished.")):
            with self.assertRaises(ConnectorError):
                self.app.linkedin_sign_in()
        self.assertEqual(self.linkedin_status()["last_error"], "")

    def test_disconnect_removes_the_browser_profile_and_keeps_conversations(self) -> None:
        self.connect()
        (linkedin_browser.profile_dir(self.settings) / "Cookies").write_text("session")
        linkedin.ingest_payload(self.db, {"threads": [{"thread_id": "2-a==", "participant": {"name": "Asha"},
                                                       "messages": [{"from_me": False, "text": "hello", "sent_at": "2026-11-18T09:00:00Z"}]}]})
        self.app.linkedin_disconnect()
        self.assertFalse(linkedin_browser.profile_dir(self.settings).exists())
        status = self.linkedin_status()
        self.assertEqual((status["browser"]["connected"], status["threads"]), (False, 1))
        self.assertFalse(linkedin_browser.ready(self.settings, self.db))

    def test_without_a_browser_it_says_so(self) -> None:
        self.find.stop()
        try:
            with mock.patch.object(cdp, "find_browser", return_value=""):
                self.assertFalse(self.linkedin_status()["browser"]["available"])
                with self.assertRaises(ConnectorError) as raised:
                    linkedin_browser.collect(self.settings, self.db)
                self.assertIn("No Chrome", str(raised.exception))
        finally:
            self.find.start()

    def test_over_http(self) -> None:
        self.connect()
        base, stop = start_server(self.app)
        try:
            status = http(base, "GET", "/api/status")[1]
            browser = next(c for c in status["connectors"] if c["name"] == "linkedin")["browser"]
            self.assertEqual((browser["available"], browser["connected"], browser["browser"]), (True, True, "Stand-in"))
            keys = {f["key"] for f in http(base, "GET", "/api/settings")[1]["fields"] if f["section"] == "linkedin"}
            self.assertEqual(keys, {"LINKEDIN_OPEN_THREADS", "LINKEDIN_BROWSER_VISIBLE", "LINKEDIN_STALE_HOURS",
                                    "LINKEDIN_OPEN_UNREAD", "LINKEDIN_BROWSER_PATH"})
            self.assertEqual(http(base, "POST", "/api/sync", {}, {"connector": "linkedin"})[0], 403)  # needs the write header
            with mock.patch.object(linkedin_browser, "collect", return_value={"new_messages": 2, "listed": 3, "opened": 1}):
                self.assertEqual(http(base, "POST", "/api/sync", WRITE, {"connector": "linkedin"})[1], {"started": True})
                for _ in range(100):
                    job = http(base, "GET", "/api/status")[1]["job"]
                    if not job["running"]:
                        break
                    time.sleep(0.05)
            self.assertEqual(job["result"]["linkedin"], {"new_messages": 2, "listed": 3, "opened": 1})
            code, payload, _, _ = http(base, "POST", "/api/linkedin/browser/disconnect", WRITE, {})
            self.assertEqual(code, 200)
            self.assertFalse(next(c for c in payload["status"]["connectors"] if c["name"] == "linkedin")["browser"]["connected"])
        finally:
            stop()

    def test_settings_validate_the_new_fields(self) -> None:
        clean = settings_store.validate({"LINKEDIN_OPEN_THREADS": "0", "LINKEDIN_BROWSER_VISIBLE": "false", "LINKEDIN_OPEN_UNREAD": "true"})
        self.assertEqual(clean["LINKEDIN_OPEN_THREADS"], "0")
        with self.assertRaises(settings_store.SettingsError):
            settings_store.validate({"LINKEDIN_OPEN_THREADS": "40"})


BROWSER = cdp.find_browser()


@unittest.skipUnless(BROWSER and os.environ.get("DEALFLOW_BROWSER_TESTS") == "1",
                     "set DEALFLOW_BROWSER_TESTS=1 to drive a real browser against the stand-in LinkedIn")
class RealBrowserTests(unittest.TestCase):
    """A real browser, headless, against tests/fake_linkedin.py. Never the real site."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name), linkedin_browser_visible=False)
        self.app = App(self.settings)
        self.db = self.app.db
        self.site = FakeLinkedIn(inbox())
        self.env = mock.patch.dict(os.environ, {"DEALFLOW_LINKEDIN_URL": self.site.url})
        self.env.start()
        self.timing = mock.patch.multiple(linkedin_browser, PAUSE=(0.0, 0.05), LIST_TIMEOUT=12.0, THREAD_TIMEOUT=10.0)
        self.timing.start()

    def tearDown(self) -> None:
        self.timing.stop()
        self.env.stop()
        self.site.close()
        self.db.close()
        self.tmp.cleanup()

    def sign_in(self) -> None:
        said: list[str] = []
        result = linkedin_browser.sign_in(self.settings, self.db, said.append, timeout=30, headless=True)
        self.assertEqual(result, {"connected": True})
        self.assertIn("Sign in to LinkedIn in the window", said[0])

    def test_sign_in_sync_and_sync_again(self) -> None:
        self.assertFalse(linkedin_browser.ready(self.settings, self.db))
        self.sign_in()
        self.assertTrue(linkedin_browser.ready(self.settings, self.db))
        self.assertEqual(self.site.paths[:3], ["/login", "/login-submit", "/feed/"])

        self.site.paths.clear()
        said: list[str] = []
        stats = self.app.sync("linkedin", said.append)
        self.assertEqual((stats["listed"], stats["opened"], stats["threads"], stats["new_threads"]), (3, 2, 3, 3))
        self.assertEqual((stats["left_out_archived_or_spam"], stats["left_out_empty"]), (0, 0))
        self.assertEqual(stats["new_messages"], 5)  # 3 from Asha's history, Kabir's newest only, Rhea's one
        self.assertEqual(self.site.paths[0], "/messaging/thread/new/")
        self.assertEqual(sorted(self.site.opened()), ["2-asha==", "2-rhea=="])  # the unread conversation was not opened
        self.assertTrue(any("reading the inbox list" in line for line in said))

        rows = {r["external_id"]: r for r in self.db.all("SELECT * FROM threads WHERE channel='linkedin'")}
        self.assertEqual((rows["2-asha=="]["message_count"], rows["2-asha=="]["last_direction"]), (3, "in"))
        self.assertEqual((rows["2-kabir=="]["message_count"], rows["2-kabir=="]["unread"]), (1, 1))
        directions = [r["direction"] for r in self.db.all(
            "SELECT direction FROM messages WHERE thread_id=? ORDER BY sent_at", (rows["2-asha=="]["id"],))]
        self.assertEqual(directions, ["in", "out", "in"])

        # Again: nothing new, and nothing is opened a second time.
        self.site.paths.clear()
        again = self.app.sync("linkedin")
        self.assertEqual((again["new_messages"], again["opened"]), (0, 0))
        self.assertEqual(self.site.paths, ["/messaging/thread/new/"])
        status = next(c for c in self.app.status()["connectors"] if c["name"] == "linkedin")
        self.assertEqual((status["last_error"], status["stale"], status["threads"]), ("", False, 3))

    def test_an_expired_session_stops_at_the_login_page(self) -> None:
        self.sign_in()
        self.site.signed_out = True
        self.site.auto_sign_in = False  # nobody signs in during a sync
        self.site.paths.clear()
        with self.assertRaises(linkedin_browser.NeedsSignIn):
            self.app.sync("linkedin")
        self.assertEqual(self.site.paths, ["/messaging/thread/new/", "/login"])  # it went no further
        status = next(c for c in self.app.status()["connectors"] if c["name"] == "linkedin")
        self.assertTrue(status["browser"]["needs_sign_in"])
        self.assertIn("LinkedIn needs sign-in", status["last_error"])

    def test_a_security_check_stops_it_too(self) -> None:
        self.sign_in()
        self.site.checkpoint = True
        with self.assertRaises(linkedin_browser.NeedsSignIn):
            self.app.sync("linkedin")
        self.assertEqual(self.site.opened(), [])

    def test_a_page_it_does_not_recognise_is_reported_not_guessed_at(self) -> None:
        self.sign_in()
        self.site.silent = True
        with mock.patch.object(linkedin_browser, "LIST_TIMEOUT", 3.0):
            with self.assertRaises(ConnectorError) as raised:
                self.app.sync("linkedin")
        self.assertIn("no conversation data in a form Dealflow recognises", str(raised.exception))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM threads", (), 0), 0)

    def test_sign_in_gives_up_when_nobody_signs_in(self) -> None:
        self.site.auto_sign_in = False
        with self.assertRaises(ConnectorError) as raised:
            linkedin_browser.sign_in(self.settings, self.db, timeout=3, headless=True)
        self.assertIn("not finished", str(raised.exception))
        self.assertFalse(linkedin_browser.ready(self.settings, self.db))


if __name__ == "__main__":
    unittest.main()
