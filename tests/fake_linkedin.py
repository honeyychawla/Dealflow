"""A stand-in for linkedin.com, for testing the browser collector without touching LinkedIn.

It serves just enough of the real site's shape: a login page, a messaging page
that loads its conversation list from /voyager/api/voyagerMessagingGraphQL,
and a page per conversation that loads that conversation's messages. The JSON
follows the field names LinkedIn's messaging data uses (conversation and
message ids, participants, body.text, deliveredAt in milliseconds).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

ME = "ME123"
ME_URN = f"urn:li:fsd_profile:{ME}"


def participant(profile_id: str, first: str, last: str, headline: str = "") -> dict[str, Any]:
    return {
        "entityUrn": f"urn:li:msg_messagingParticipant:urn:li:fsd_profile:{profile_id}",
        "hostIdentityUrn": f"urn:li:fsd_profile:{profile_id}",
        "participantType": {"member": {
            "firstName": {"text": first}, "lastName": {"text": last}, "headline": {"text": headline},
            "profileUrl": f"https://www.linkedin.com/in/{profile_id}", "distance": "DISTANCE_2",
        }},
    }


MYSELF = participant(ME, "Test", "Owner")


def message(message_id: str, sender: dict[str, Any], text: str, at_ms: int, **extra: Any) -> dict[str, Any]:
    return {
        "entityUrn": f"urn:li:msg_message:({ME_URN},{message_id})",
        "body": {"text": text, "attributes": []},
        "deliveredAt": at_ms,
        "sender": sender,
        "renderContent": [],
        **extra,
    }


def conversation(thread_id: str, other: dict[str, Any], messages: list[dict[str, Any]], *, read: bool,
                 categories: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    """`messages` is the whole history; the inbox list only ever carries the newest one."""
    return {
        "entityUrn": f"urn:li:msg_conversation:({ME_URN},{thread_id})",
        "lastActivityAt": max(m["deliveredAt"] for m in messages),
        "read": read,
        "groupChat": False,
        "categories": categories or ["INBOX", "PRIMARY_INBOX"],
        "conversationParticipants": [MYSELF, other],
        "_history": messages,
        **extra,
    }


def flatten(conversations: list[dict[str, Any]]) -> dict[str, Any]:
    """The same inbox list in LinkedIn's other shape: one flat "included" list joined by "*name" references."""
    included: list[dict[str, Any]] = []
    urns = []
    for item in conversations:
        newest = item["_history"][-1]
        included.append({**{k: v for k, v in newest.items() if k != "sender"}, "*sender": newest["sender"]["entityUrn"],
                         "*conversation": item["entityUrn"]})
        for person in item["conversationParticipants"]:
            included.append(person)
        included.append({
            **{k: v for k, v in item.items() if k not in ("_history", "conversationParticipants")},
            "*conversationParticipants": [p["entityUrn"] for p in item["conversationParticipants"]],
            "messages": {"*elements": [newest["entityUrn"]]},
        })
        urns.append(item["entityUrn"])
    return {"data": {"data": {"messengerConversationsBySyncToken": {"*elements": urns}}}, "included": included}


PAGE = """<!doctype html><meta charset="utf-8"><title>{title}</title><p>{title}</p>
<script>
const load = (path) => fetch(path, {{ headers: {{ accept: "application/graphql" }} }}).then((r) => r.json());
{script}
</script>"""


class FakeLinkedIn:
    def __init__(self, conversations: list[dict[str, Any]]):
        self.conversations = conversations
        self.signed_out = False      # True: the session has expired, every page sends the browser to /login
        self.checkpoint = False      # True: LinkedIn wants a security check
        self.silent = False          # True: the messaging page loads no data at all (as after a redesign)
        self.auto_sign_in = True     # the "owner" finishes signing in a moment after /login opens
        self.paths: list[str] = []   # every page the browser asked for, in order
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def send(self, status: int, body: str, kind: str = "text/html; charset=utf-8", headers: dict[str, str] | None = None) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def redirect(self, where: str, headers: dict[str, str] | None = None) -> None:
                self.send(302, "", headers={"Location": where, **(headers or {})})

            def has_session(self) -> bool:
                return "li_at=" in (self.headers.get("Cookie") or "") and not outer.signed_out

            def do_GET(self) -> None:  # noqa: N802 - the name http.server expects
                parts = urlsplit(self.path)
                path = parts.path
                if path == "/favicon.ico":
                    return self.send(404, "", "text/plain")
                if not path.startswith("/voyager/"):
                    outer.paths.append(path)
                if path == "/login":
                    if self.has_session():
                        return self.redirect("/feed/")
                    script = 'setTimeout(() => { location = "/login-submit"; }, 500);' if outer.auto_sign_in else ""
                    return self.send(200, PAGE.format(title="Sign in", script=script))
                if path == "/login-submit":
                    outer.signed_out = False
                    return self.redirect("/feed/", {"Set-Cookie": "li_at=stand-in-session; Path=/; Max-Age=86400"})
                if path == "/feed/":
                    return self.send(200, PAGE.format(title="Feed", script=""))
                if path.startswith("/checkpoint/"):
                    return self.send(200, PAGE.format(title="Security check", script=""))
                if path.startswith("/messaging/"):
                    if outer.checkpoint:
                        return self.redirect("/checkpoint/challenge/abc")
                    if not self.has_session():
                        return self.redirect("/login?session_redirect=" + path)
                    variables = f"(mailboxUrn:urn%3Ali%3Afsd_profile%3A{ME})"
                    script = "" if outer.silent else (
                        f'load("/voyager/api/voyagerMessagingGraphQL/graphql?queryId=messengerConversations.abc&variables={variables}");'
                    )
                    thread_id = path.removeprefix("/messaging/thread/").strip("/")
                    if thread_id and thread_id != "new" and not outer.silent:
                        urn = f"urn%3Ali%3Amsg_conversation%3A%28urn%3Ali%3Afsd_profile%3A{ME}%2C{thread_id.replace('=', '%3D')}%29"
                        script += (
                            f'\nsetTimeout(() => load("/voyager/api/voyagerMessagingGraphQL/graphql?queryId=messengerMessages.def'
                            f'&variables=(conversationUrn:{urn})"), 300);'
                        )
                    return self.send(200, PAGE.format(title="Messaging", script=script))
                if path == "/voyager/api/voyagerMessagingGraphQL/graphql":
                    if not self.has_session():
                        return self.send(401, "{}", "application/json")
                    query = parse_qs(parts.query)
                    query_id = (query.get("queryId") or [""])[0]
                    if query_id.startswith("messengerConversations"):
                        elements = [
                            {**{k: v for k, v in item.items() if k != "_history"}, "messages": {"elements": item["_history"][-1:]}}
                            for item in outer.conversations
                        ]
                        body = {"data": {"messengerConversationsBySyncToken": {"metadata": {"newSyncToken": "t"}, "elements": elements}}}
                        return self.send(200, json.dumps(body), "application/graphql+json")
                    if query_id.startswith("messengerMessages"):
                        wanted = unquote((query.get("variables") or [""])[0])
                        for item in outer.conversations:
                            if item["entityUrn"] in wanted:
                                body = {"data": {"messengerMessagesByAnchorTimestamp": {"metadata": {}, "elements": item["_history"]}}}
                                return self.send(200, json.dumps(body), "application/graphql+json")
                        return self.send(200, json.dumps({"data": {"messengerMessagesByAnchorTimestamp": {"elements": []}}}), "application/json")
                return self.send(404, "not found", "text/plain")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, name="fake-linkedin", daemon=True).start()

    def opened(self) -> list[str]:
        """Thread ids of the conversations the browser opened."""
        return [
            path.removeprefix("/messaging/thread/").strip("/") for path in self.paths
            if path.startswith("/messaging/thread/") and not path.startswith("/messaging/thread/new")
        ]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
