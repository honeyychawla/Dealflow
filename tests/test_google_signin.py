"""Sign in with Google for Gmail, against a local stand-in for Google.

The stand-in speaks the documented shapes of Google's OAuth token and revoke
endpoints and of the Gmail API (profile, labels, messages.list with q and
labelIds, messages.get format=full). It also checks what a real Google would
refuse: a wrong PKCE verifier, a reused code, an unknown refresh token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_dealflow import WRITE, http, start_server  # noqa: E402

from dealflow.app import App  # noqa: E402
from dealflow.config import GmailAccount, Settings, load_settings  # noqa: E402
from dealflow.connectors import gmail_api, google_oauth  # noqa: E402
from dealflow.connectors.gmail_api import GmailApi, mime_from_payload  # noqa: E402
from dealflow.connectors.imap import body_text  # noqa: E402

CLIENT_ID = "1234-test.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-secret-0000"


def b64(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def headers(**values: str) -> list[dict[str, str]]:
    return [{"name": key.replace("_", "-"), "value": value} for key, value in values.items()]


def text_message(message_id: str, sender: str, to: str, subject: str, body: str, at: int, labels: list[str],
                 **extra: str) -> dict:
    """A message as messages.get(format=full) returns a simple text/plain mail."""
    return {
        "id": message_id, "threadId": "t" + message_id, "labelIds": labels, "internalDate": str(at * 1000),
        "payload": {
            "mimeType": "text/plain",
            "headers": headers(From=sender, To=to, Subject=subject, Date="Mon, 16 Nov 2026 10:00:00 +0000",
                               Message_ID=f"<{message_id}@mail.example>", Content_Type='text/plain; charset="UTF-8"',
                               Content_Transfer_Encoding="quoted-printable", **extra),
            "body": {"size": len(body), "data": b64(body)},
        },
    }


class FakeGoogle:
    """accounts.google.com, oauth2.googleapis.com and gmail.googleapis.com, for one test."""

    def __init__(self) -> None:
        self.challenges: dict[str, str] = {}  # code -> PKCE challenge it was issued for
        self.codes: dict[str, str] = {}  # code -> the address the owner "chose" on Google's screen
        self.refresh: dict[str, str] = {}  # refresh token -> address
        self.access: dict[str, str] = {}  # access token -> address
        self.mail: dict[str, list[dict]] = {}  # address -> messages
        self.revoked: list[str] = []
        self.requests: list[tuple[str, str]] = []  # (method, path with query)
        self.grant_scope = google_oauth.SCOPE
        self.fail_refresh = ""
        self.gmail_api_disabled = False
        # Quota: refuse the next N message reads, or every read after `reads_allowed` successful ones.
        self.refuse_next = 0
        self.reads_allowed: int | None = None
        self.reads_ok = 0
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def _send(self, status: int, payload: dict, extra: dict | None = None) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _quota(self) -> bool:
                """True if this message read is refused for quota, the way Gmail does it."""
                with outer.lock:
                    if outer.refuse_next > 0:
                        outer.refuse_next -= 1
                        refused = True
                    elif outer.reads_allowed is not None and outer.reads_ok >= outer.reads_allowed:
                        refused = True
                    else:
                        outer.reads_ok += 1
                        refused = False
                if refused:
                    self._send(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                               "message": "Quota exceeded for quota metric 'Total Query Cost'",
                                               "errors": [{"reason": "rateLimitExceeded"}]}}, {"Retry-After": "0"})
                return refused

            def do_POST(self) -> None:  # noqa: N802
                outer.requests.append(("POST", self.path))
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                if self.path == "/revoke":
                    outer.revoked.append(form["token"])
                    return self._send(200, {})
                if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
                    return self._send(401, {"error": "invalid_client"})
                if form["grant_type"] == "authorization_code":
                    code = form.get("code", "")
                    address = outer.codes.pop(code, None)
                    challenge = outer.challenges.pop(code, "")
                    verified = base64.urlsafe_b64encode(
                        hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                    if address is None or verified != challenge:
                        return self._send(400, {"error": "invalid_grant"})
                    refresh, access = f"1//rt-{address}-{len(outer.refresh)}", f"ya29.at-{address}-{len(outer.access)}"
                    outer.refresh[refresh], outer.access[access] = address, address
                    return self._send(200, {"access_token": access, "refresh_token": refresh, "expires_in": 3599,
                                            "scope": outer.grant_scope, "token_type": "Bearer"})
                if form["grant_type"] == "refresh_token":
                    address = outer.refresh.get(form.get("refresh_token", ""))
                    if address is None or outer.fail_refresh:
                        return self._send(400, {"error": outer.fail_refresh or "invalid_grant"})
                    access = f"ya29.at-{address}-{len(outer.access)}"
                    outer.access[access] = address
                    return self._send(200, {"access_token": access, "expires_in": 3599, "scope": outer.grant_scope})
                return self._send(400, {"error": "unsupported_grant_type"})

            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append(("GET", self.path))
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                address = outer.access.get(token)
                if address is None:
                    return self._send(401, {"error": {"code": 401, "message": "Invalid Credentials", "status": "UNAUTHENTICATED"}})
                if outer.gmail_api_disabled:
                    return self._send(403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                                      "message": "Gmail API has not been used in project 1 before or it is disabled.",
                                                      "errors": [{"reason": "accessNotConfigured"}]}})
                parts = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parts.query))
                path = parts.path.removeprefix("/gmail/v1/users/me/")
                mail = outer.mail.get(address, [])
                if path == "profile":
                    return self._send(200, {"emailAddress": address, "messagesTotal": len(mail)})
                if path == "labels":
                    return self._send(200, {"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"},
                                                       {"id": "SENT", "name": "SENT", "type": "system"},
                                                       {"id": "Label_7", "name": "Deals", "type": "user"}]})
                if path == "labels/INBOX":
                    return self._send(200, {"id": "INBOX", "messagesTotal": sum("INBOX" in m["labelIds"] for m in mail)})
                if path == "messages":
                    found = [m for m in mail if query["labelIds"] in m["labelIds"]]
                    for term in query.get("q", "").split():
                        if term.startswith("after:"):
                            found = [m for m in found if int(m["internalDate"]) // 1000 > int(term[6:])]
                        if term == "category:primary":
                            found = [m for m in found if not any(l.startswith("CATEGORY_") and l != "CATEGORY_PERSONAL" for l in m["labelIds"])]
                    found.sort(key=lambda m: -int(m["internalDate"]))
                    start = int(query.get("pageToken") or 0)
                    size = int(query.get("maxResults") or 100)
                    page = found[start:start + size]
                    body = {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in page], "resultSizeEstimate": len(found)}
                    if start + size < len(found):
                        body["nextPageToken"] = str(start + size)
                    return self._send(200, body)
                if path.startswith("messages/"):
                    if self._quota():
                        return
                    message = next((m for m in mail if m["id"] == path[9:]), None)
                    if message is None:
                        return self._send(404, {"error": {"code": 404, "message": "Requested entity was not found."}})
                    return self._send(200, message)
                return self._send(404, {"error": {"code": 404, "message": "Not found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.patches = [
            mock.patch.object(google_oauth, "AUTH_URL", self.url + "/o/oauth2/v2/auth"),
            mock.patch.object(google_oauth, "TOKEN_URL", self.url + "/token"),
            mock.patch.object(google_oauth, "REVOKE_URL", self.url + "/revoke"),
            mock.patch.object(gmail_api, "API", self.url + "/gmail/v1/users/me"),
            mock.patch.object(gmail_api, "REQUESTS_PER_SECOND", 1000.0),
            mock.patch.object(gmail_api, "BACKOFF", 0.01),
        ]
        for patch in self.patches:
            patch.start()
        google_oauth._cache.clear()

    def consent(self, auth_url: str, address: str) -> str:
        """Play the owner on Google's screen: pick `address`, allow. Returns the code."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(auth_url).query))
        code = f"4/code-{len(self.codes)}-{address}"
        self.codes[code] = address
        self.challenges[code] = query["code_challenge"]
        return code

    def sign_in(self, address: str) -> str:
        """A refresh token for `address`, as if the owner had signed in before."""
        refresh = f"1//rt-{address}-preset"
        self.refresh[refresh] = address
        return refresh

    def gmail_gets(self) -> list[str]:
        return [path for method, path in self.requests if method == "GET"]

    def close(self) -> None:
        for patch in self.patches:
            patch.stop()
        self.server.shutdown()
        self.server.server_close()
        google_oauth._cache.clear()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def get_raw(url: str) -> tuple[int, dict]:
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(url, timeout=20) as response:
            return response.status, dict(response.headers)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, dict(error.headers)


class SignInFlowTests(unittest.TestCase):
    """Settings page > Sign in with Google > Google > back to the app, through the real server."""

    def setUp(self) -> None:
        self.google = FakeGoogle()
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        self.env = folder / ".env"
        self.env.write_text(
            f"GOOGLE_CLIENT_ID={CLIENT_ID}\nGOOGLE_CLIENT_SECRET={CLIENT_SECRET}\n"
            "GMAIL_ADDRESS=work@example.com\nGMAIL_APP_PASSWORD=abcdefghijklmnop\nGMAIL_LABEL=Work\n",
            encoding="utf-8",
        )
        self.environ = mock.patch.dict("os.environ", {}, clear=False)
        self.environ.start()
        for name in list(os.environ):
            if name.startswith(("INSTAGRAM", "GMAIL", "PROTON", "OPENROUTER", "OWNER", "GOOGLE")):
                os.environ.pop(name)
        settings = load_settings(self.env)
        settings.data_dir = folder / "data"
        settings.data_dir.mkdir()
        self.app = App(settings)
        self.base, self.stop = start_server(self.app)
        self.google.mail["work@example.com"] = [
            text_message("m1", "Jo Park <jo@acme.example>", "work@example.com", "Sponsorship", "Budget is $8,000.", 1_763_200_000, ["INBOX"]),
        ]
        self.google.mail["home@example.com"] = []

    def tearDown(self) -> None:
        self.stop()
        self.environ.stop()
        self.google.close()
        self.tmp.cleanup()

    def post(self, path: str, body: dict):
        return http(self.base, "POST", path, WRITE, body)

    def start(self, slot: int = 1) -> tuple[str, dict]:
        status, payload, text, _ = self.post("/api/gmail/google/start", {"slot": slot})
        self.assertEqual(status, 200, text)
        return payload["url"], dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(payload["url"]).query))

    def callback(self, **params: str) -> tuple[int, dict]:
        return get_raw(self.base + google_oauth.CALLBACK_PATH + "?" + urllib.parse.urlencode(params))

    def result(self) -> dict | None:
        return http(self.base, "GET", "/api/settings")[1]["google"]["result"]

    def test_sign_in_replaces_the_app_password_and_syncs_read_only(self) -> None:
        url, query = self.start(1)
        port = self.app.settings.port
        self.assertTrue(url.startswith(self.google.url + "/o/oauth2/v2/auth?"))
        self.assertEqual(query["scope"], "https://www.googleapis.com/auth/gmail.readonly")  # nothing broader
        self.assertEqual((query["client_id"], query["response_type"], query["access_type"], query["prompt"]),
                         (CLIENT_ID, "code", "offline", "consent"))
        self.assertEqual(query["redirect_uri"], f"http://127.0.0.1:{port}/oauth/google/callback")
        self.assertEqual((query["code_challenge_method"], query["login_hint"]), ("S256", "work@example.com"))
        self.assertNotIn(CLIENT_SECRET, url)

        code = self.google.consent(url, "work@example.com")
        status, response_headers = self.callback(code=code, state=query["state"])
        self.assertEqual((status, response_headers["Location"]), (303, "/#/settings/gmail"))

        status, payload, text, _ = http(self.base, "GET", "/api/settings")
        self.assertTrue(payload["google"]["result"]["ok"], payload["google"]["result"])
        self.assertIn("Signed in with Google as work@example.com", payload["google"]["result"]["message"])
        self.assertIn("app password for it was removed", payload["google"]["result"]["message"])
        self.assertIsNone(self.result())  # shown once
        self.assertEqual(payload["google"]["redirect_uri"], f"http://127.0.0.1:{port}/oauth/google/callback")
        slot = payload["accounts"]["gmail"]["slots"][0]
        self.assertEqual((slot["values"]["address"], slot["values"]["label"]), ("work@example.com", "Work"))
        self.assertEqual(slot["secrets"]["refresh_token"], {"set": True, "hint": ""})
        self.assertEqual(slot["secrets"]["password"], {"set": False, "hint": ""})
        for secret in ("1//rt-", "ya29.", CLIENT_SECRET, "abcdefghijklmnop"):
            self.assertNotIn(secret, text)

        written = self.env.read_text(encoding="utf-8")
        self.assertIn("GMAIL_REFRESH_TOKEN=1//rt-work@example.com-0", written)
        self.assertNotIn("GMAIL_APP_PASSWORD", written)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        account = self.app.settings.gmail_accounts[0]
        self.assertTrue(account.signed_in_with_google)
        self.assertIsInstance(self.app.gmail_connector(account), GmailApi)

        # The same state cannot be used twice, and the token cannot be typed in.
        self.callback(code=code, state=query["state"])
        self.assertIn("expired or was already used", self.result()["message"])
        self.assertEqual(self.post("/api/settings", {"values": {"GMAIL_REFRESH_TOKEN": "1//forged"}})[0], 400)

        result = self.post("/api/settings/test", {"target": "gmail", "slot": 1})[1]
        self.assertEqual(result, {"ok": True, "message": (
            "Signed in with Google to work@example.com, read-only. The inbox holds 1 message. Sent mail is read too.")})
        synced = self.app.sync("gmail")
        self.assertEqual((synced["new_messages"], synced.get("error")), (1, None))
        self.assertTrue(all(method == "GET" for method, path in self.google.requests if "/gmail/" in path))

        # Remove account: the sign-in is withdrawn at Google as well.
        status, payload, _, _ = self.post("/api/settings", {"values": {
            "GMAIL_ADDRESS": None, "GMAIL_REFRESH_TOKEN": None, "GMAIL_LABEL": None}})
        self.assertEqual((status, payload["accounts"]["gmail"]["slots"]), (200, []))
        for _ in range(100):
            if self.google.revoked:
                break
            threading.Event().wait(0.05)
        self.assertEqual(self.google.revoked, ["1//rt-work@example.com-0"])

    def test_a_second_account_signs_in_without_typing_anything(self) -> None:
        url, query = self.start(2)  # a fresh, never-saved block
        self.assertNotIn("login_hint", query)
        self.callback(code=self.google.consent(url, "home@example.com"), state=query["state"])
        self.assertTrue(self.result()["ok"])
        self.assertEqual([(a.slot, a.address, a.signed_in_with_google) for a in self.app.settings.gmail_accounts],
                         [(1, "work@example.com", False), (2, "home@example.com", True)])
        self.assertIn("home@example.com", self.app.settings.owner_emails)

        # Signing in again on slot 2 with the address of slot 1 is refused, and slot 1's grant is not touched.
        url, query = self.start(2)
        self.callback(code=self.google.consent(url, "work@example.com"), state=query["state"])
        result = self.result()
        self.assertEqual((result["slot"], result["ok"]), (2, False))
        self.assertIn("work@example.com is already added as Work", result["message"])
        self.assertEqual(self.google.revoked, [])

    def test_wrong_account_cancel_and_missing_permission_change_nothing(self) -> None:
        before = self.env.read_text(encoding="utf-8")
        url, query = self.start(1)
        self.callback(code=self.google.consent(url, "someone@else.example"), state=query["state"])
        result = self.result()
        self.assertFalse(result["ok"])
        self.assertIn("You signed in as someone@else.example, but this account is work@example.com", result["message"])
        self.assertEqual(self.google.revoked, ["1//rt-someone@else.example-0"])  # an unused grant is withdrawn

        url, query = self.start(1)
        self.callback(error="access_denied", state=query["state"])
        self.assertEqual(self.result()["message"], "Google sign-in was cancelled. Nothing was changed.")

        self.google.grant_scope = "openid"  # the owner unticked "Read your email"
        url, query = self.start(1)
        self.callback(code=self.google.consent(url, "work@example.com"), state=query["state"])
        self.assertIn("without permission to read Gmail", self.result()["message"])

        url, query = self.start(1)
        self.google.consent(url, "work@example.com")
        self.callback(code="4/forged", state=query["state"])  # a code Google never issued for this verifier
        self.assertIn("expired or was withdrawn", self.result()["message"])
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)

    def test_start_needs_the_client_and_a_valid_slot(self) -> None:
        self.assertEqual(self.post("/api/gmail/google/start", {"slot": 9})[0], 400)
        self.assertEqual(self.post("/api/gmail/google/start", {"slot": "x"})[0], 400)
        self.assertEqual(self.post("/api/gmail/google/start", {"slot": 1})[0], 200)
        # The start route changes state, so it needs the same CSRF header as every other write.
        self.assertEqual(http(self.base, "POST", "/api/gmail/google/start", {}, {"slot": 1})[0], 403)
        self.post("/api/settings", {"values": {"GOOGLE_CLIENT_SECRET": None}})
        status, payload, _, _ = self.post("/api/gmail/google/start", {"slot": 1})
        self.assertEqual(status, 400)
        self.assertIn("client ID and secret first", payload["error"])
        self.assertEqual(self.post("/api/settings", {"values": {"GOOGLE_CLIENT_ID": "not-a-client-id"}})[0], 400)


class GmailApiSyncTests(unittest.TestCase):
    """The Gmail API connector: same results as IMAP, only reads, and incremental."""

    def setUp(self) -> None:
        self.google = FakeGoogle()
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            data_dir=Path(self.tmp.name), google_client_id=CLIENT_ID, google_client_secret=CLIENT_SECRET,
            gmail_address="work@example.com", gmail_label="Work", gmail_refresh_token=self.google.sign_in("work@example.com"),
            owner_emails=["work@example.com"],
        )
        self.app = App(self.settings)
        html = "<p>Would you join our <b>podcast</b>? Café at 10.</p>"
        self.google.mail["work@example.com"] = [
            text_message("m1", "Jo Park <jo@acme.example>", "work@example.com", "Sponsorship",
                         "We'd like to sponsor your newsletter. Budget is $8,000.", 1_763_200_000, ["INBOX"]),
            text_message("m2", "Growth <news@digest.example>", "work@example.com", "Weekly digest", "Top links",
                         1_763_200_100, ["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"], List_Unsubscribe="<mailto:u@digest.example>"),
            {"id": "m3", "threadId": "t3", "labelIds": ["INBOX", "UNREAD", "CATEGORY_PERSONAL"], "internalDate": "1763200200000",
             "payload": {"mimeType": "multipart/mixed",
                         "headers": headers(From="Mia Chen <mia@studio.example>", To="work@example.com", Subject="Podcast invite",
                                            Date="Mon, 16 Nov 2026 10:05:00 +0000", Message_ID="<p1@studio.example>",
                                            Content_Type='multipart/mixed; boundary="b1"'),
                         "parts": [
                             {"mimeType": "multipart/alternative", "headers": headers(Content_Type='multipart/alternative; boundary="b2"'),
                              "parts": [{"mimeType": "text/html", "headers": headers(Content_Type='text/html; charset="ISO-8859-1"'),
                                         "body": {"data": b64(html.encode("iso-8859-1"))}}]},
                             {"mimeType": "application/pdf", "filename": "deck.pdf",
                              "headers": headers(Content_Type='application/pdf; name="deck.pdf"',
                                                 Content_Disposition='attachment; filename="deck.pdf"'),
                              "body": {"attachmentId": "ANGjdJ8", "size": 900000}},
                         ]}},
            text_message("s1", "work@example.com", "jo@acme.example", "Re: Sponsorship", "Happy to talk. What are the dates?",
                         1_763_200_300, ["SENT"], References="<m1@mail.example>"),
            {**text_message("d1", "work@example.com", "jo@acme.example", "Draft", "unsent", 1_763_200_400, ["DRAFT", "SENT"])},
        ]

    def tearDown(self) -> None:
        self.app.db.close()
        self.google.close()
        self.tmp.cleanup()

    def test_sync_matches_the_imap_connector_and_only_reads(self) -> None:
        db = self.app.db
        result = self.app.sync("gmail")
        self.assertNotIn("error", result)
        self.assertEqual(result["new_messages"], 4)  # 3 inbox + 1 sent; the draft is skipped
        threads = {r["subject"]: r for r in db.all("SELECT * FROM threads")}
        self.assertEqual(sorted(threads), ["Podcast invite", "Sponsorship", "Weekly digest"])
        sponsorship = threads["Sponsorship"]
        self.assertEqual((sponsorship["message_count"], sponsorship["last_direction"], sponsorship["account"]), (2, "out", "work@example.com"))
        self.assertEqual(sponsorship["url"], "https://mail.google.com/mail/u/work@example.com/#search/rfc822msgid:s1%40mail.example")
        self.assertTrue(json.loads(threads["Weekly digest"]["meta"])["automated"])
        self.assertEqual((threads["Sponsorship"]["unread"], threads["Podcast invite"]["unread"]), (0, 1))
        podcast = db.one("SELECT text, attachments FROM messages WHERE external_id='<p1@studio.example>'")
        self.assertEqual(podcast["text"], "Would you join our podcast ? Café at 10.")  # HTML in Latin-1, decoded
        self.assertEqual(json.loads(podcast["attachments"]), [{"type": "file", "name": "deck.pdf"}])
        self.assertEqual([a["via"] for a in self.app.status()["connectors"][1]["accounts"]], ["google"])

        gets = self.google.gmail_gets()
        self.assertTrue(all(path.startswith("/gmail/v1/users/me/") for path in gets))
        self.assertFalse([m for m, p in self.google.requests if m != "GET" and p.startswith("/gmail/")])
        self.assertTrue(all("format=full" in p for p in gets if "/messages/" in p))
        self.assertFalse([p for p in gets if "attachments" in p])  # attachment data never downloaded
        self.assertIn("newer_than:30d", urllib.parse.unquote_plus(next(p for p in gets if "/messages?" in p)))

        # A repeat sync asks only for newer mail, and does not fetch what it already read.
        self.google.requests.clear()
        self.google.mail["work@example.com"].append(text_message(
            "m4", "Jo Park <jo@acme.example>", "work@example.com", "RE: Sponsorship", "Dec 5 to Dec 12.",
            1_763_200_500, ["INBOX", "UNREAD"], In_Reply_To="<s1@mail.example>", References="<m1@mail.example> <s1@mail.example>"))
        again = self.app.sync("gmail")
        self.assertEqual(again["new_messages"], 1)
        gets = [urllib.parse.unquote_plus(p) for p in self.google.gmail_gets()]
        self.assertIn(f"after:{1_763_200_200 - gmail_api.OVERLAP}", next(p for p in gets if "labelIds=INBOX" in p))
        self.assertEqual([p.split("?")[0] for p in gets if "/messages/" in p], ["/gmail/v1/users/me/messages/m4"])
        self.assertEqual(db.one("SELECT message_count FROM threads WHERE subject='Sponsorship'")["message_count"], 3)

    def test_primary_only_and_paging(self) -> None:
        self.settings.gmail_primary_only = True
        with mock.patch.object(gmail_api, "PAGE", 1):
            self.app.sync("gmail")
        self.assertEqual(sorted(r["subject"] for r in self.app.db.all("SELECT subject FROM threads")), ["Podcast invite", "Sponsorship"])
        lists = [urllib.parse.unquote_plus(p) for p in self.google.gmail_gets() if "/messages?" in p]
        self.assertTrue(all("category:primary" in p for p in lists if "labelIds=INBOX" in p))
        self.assertFalse([p for p in lists if "labelIds=SENT" in p and "category:primary" in p])
        self.assertGreater(sum("pageToken=" in p for p in lists), 0)

    def test_failures_say_what_to_do_without_leaking_tokens(self) -> None:
        self.google.fail_refresh = "invalid_grant"
        result = self.app.sync("gmail")
        self.assertIn("Google sign-in for this account has expired or was withdrawn", result["error"])
        self.assertNotIn("1//rt-", result["error"])

        self.google.fail_refresh = ""
        self.google.gmail_api_disabled = True
        self.assertIn("Gmail API is switched off", self.app.sync("gmail")["error"])

        self.google.gmail_api_disabled = False
        self.settings.gmail_refresh_token = self.google.sign_in("someone@else.example")
        self.assertIn("This sign-in opens someone@else.example, but the account is set up as work@example.com",
                      self.app.sync("gmail")["error"])

        self.settings.google_client_secret = ""
        self.assertIn("client ID and secret", self.app.sync("gmail")["error"])

    def test_a_short_quota_pushback_is_waited_out(self) -> None:
        self.google.refuse_next = 5  # Gmail says "slow down" five times, then lets reads through
        result = self.app.sync("gmail")
        self.assertNotIn("error", result)
        self.assertEqual(result["new_messages"], 4)
        self.assertNotIn("unfinished", result["accounts"][0])

    def test_a_sync_cut_short_keeps_what_it_read_and_finishes_next_time(self) -> None:
        db = self.app.db
        self.google.reads_allowed = 2  # then Gmail refuses every read for the rest of this sync
        first = self.app.sync("gmail")
        account = first["accounts"][0]
        self.assertEqual(account["unfinished"], ["INBOX", "SENT"])  # still refusing when Sent's turn came
        self.assertIn("the rest on the next sync", account["mailboxes"]["INBOX"])
        read_first = {r["external_id"] for r in db.all("SELECT external_id FROM messages")}
        self.assertEqual(len(read_first), 2)  # stored, not thrown away
        self.assertIsNone(db.get_state("gmail", "api_after:INBOX"))  # the position did not move

        self.google.reads_allowed = None
        self.google.requests.clear()
        second = self.app.sync("gmail")
        self.assertNotIn("unfinished", second["accounts"][0])
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM messages"), 4)  # 3 inbox + 1 sent, each once
        fetched_again = [p.split("?")[0].rsplit("/", 1)[1] for p in self.google.gmail_gets() if "/messages/" in p]
        stored_ids = {"m1": "<m1@mail.example>", "m2": "<m2@mail.example>", "m3": "<p1@studio.example>", "s1": "<s1@mail.example>"}
        self.assertFalse([i for i in fetched_again if stored_ids.get(i) in read_first])  # nothing read twice
        self.assertEqual(db.get_state("gmail", "api_after:INBOX"), "1763200200")  # newest across both syncs
        self.assertEqual(db.get_state("gmail", "api_partial_newest:INBOX"), "")

    def test_app_password_accounts_still_use_imap(self) -> None:
        account = GmailAccount(2, "home@example.com", "ponmlkjihgfedcba", "Home")
        self.assertNotIsInstance(self.app.gmail_connector(account), GmailApi)
        self.assertFalse(account.signed_in_with_google)


class MimeTests(unittest.TestCase):
    def test_payload_rebuilds_into_a_message_the_imap_parser_reads(self) -> None:
        msg = mime_from_payload({
            "mimeType": "multipart/alternative",
            "headers": headers(Subject="=?UTF-8?Q?Caf=C3=A9?=", Content_Type='multipart/alternative; boundary="x"'),
            "parts": [
                {"mimeType": "text/plain", "headers": headers(Content_Type="text/plain; charset=utf-8",
                                                              Content_Transfer_Encoding="quoted-printable"),
                 "body": {"data": b64("Plain wins ✓")}},
                {"mimeType": "text/html", "headers": headers(Content_Type="text/html"), "body": {"data": b64("<p>html</p>")}},
            ],
        })
        self.assertEqual(body_text(msg), "Plain wins ✓")
        self.assertEqual(str(msg["Subject"]), "Café")


if __name__ == "__main__":
    unittest.main()
