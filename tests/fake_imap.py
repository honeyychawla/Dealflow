"""A small IMAP4rev1 server for tests: enough of the protocol for imaplib to sign in,
list mailboxes, open one read-only, search by UID and fetch messages.

It records every command it receives, so a test can assert that the app never
sends anything that would change a mailbox.
"""

from __future__ import annotations

import re
import socketserver
import threading


class FakeMailbox:
    def __init__(self, uidvalidity: int = 7) -> None:
        self.uidvalidity = uidvalidity
        self.messages: list[dict] = []  # {"uid", "raw", "flags", "primary", "flags_after_body"}

    def add(self, raw: bytes, flags: str = "", primary: bool = True, flags_after_body: bool = False) -> int:
        uid = (self.messages[-1]["uid"] + 1) if self.messages else 1
        self.messages.append({"uid": uid, "raw": raw, "flags": flags, "primary": primary, "flags_after_body": flags_after_body})
        return uid


class FakeImapServer:
    def __init__(self, user: str, password: str, sent_name: str = "[Gmail]/Sent Mail") -> None:
        self.user, self.password, self.sent_name = user, password, sent_name
        self.mailboxes: dict[str, FakeMailbox] = {"INBOX": FakeMailbox(), sent_name: FakeMailbox(uidvalidity=9)}
        self.commands: list[str] = []  # every command line received, tag removed, password masked
        self.login_reply: str | None = None  # set to a "NO ..." text to refuse logins
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def send(self, text: str) -> None:
                self.wfile.write(text.encode("utf-8") + b"\r\n")

            def handle(self) -> None:
                self.send("* OK Gimap ready")
                selected: FakeMailbox | None = None
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    text = line.decode("utf-8", "replace").rstrip("\r\n")
                    tag, _, rest = text.partition(" ")
                    command, _, args = rest.partition(" ")
                    command = command.upper()
                    outer.commands.append(rest if command != "LOGIN" else "LOGIN " + args.split(" ")[0] + " <password>")
                    if command == "CAPABILITY":
                        self.send("* CAPABILITY IMAP4rev1 UNSELECT IDLE X-GM-EXT-1 SPECIAL-USE")
                        self.send(f"{tag} OK Thats all she wrote!")
                    elif command == "LOGIN":
                        user, _, password = args.partition(" ")
                        password = password.strip('"')
                        if outer.login_reply:
                            self.send(f"{tag} NO {outer.login_reply}")
                        elif user.strip('"') == outer.user and password == outer.password:
                            self.send(f"{tag} OK {outer.user} authenticated (Success)")
                        else:
                            self.send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)")
                    elif command == "LIST":
                        self.send('* LIST (\\HasNoChildren) "/" "INBOX"')
                        self.send('* LIST (\\HasChildren \\Noselect) "/" "[Gmail]"')
                        self.send('* LIST (\\All \\HasNoChildren) "/" "[Gmail]/All Mail"')
                        self.send(f'* LIST (\\HasNoChildren \\Sent) "/" "{outer.sent_name}"')
                        self.send(f"{tag} OK Success")
                    elif command in ("EXAMINE", "SELECT"):
                        name = args.strip().strip('"')
                        selected = outer.mailboxes.get(name)
                        if selected is None:
                            self.send(f"{tag} NO [NONEXISTENT] Unknown Mailbox: {name} (Failure)")
                            continue
                        self.send(f"* {len(selected.messages)} EXISTS")
                        self.send("* 0 RECENT")
                        self.send(f"* OK [UIDVALIDITY {selected.uidvalidity}] UIDs valid.")
                        self.send(f"{tag} OK [READ-ONLY] {name} selected. (Success)")
                    elif command == "UID" and selected is not None:
                        sub, _, params = args.partition(" ")
                        if sub.upper() == "SEARCH":
                            uids = [m["uid"] for m in selected.messages]
                            if 'X-GM-RAW "category:primary"' in params:
                                uids = [m["uid"] for m in selected.messages if m["primary"]]
                            match = re.search(r"\bUID (\d+):\*", params)
                            if match:
                                floor = int(match.group(1))
                                hits = [u for u in uids if u >= floor]
                                # Real servers answer "n:*" with the newest message even when it is below n.
                                uids = hits or uids[-1:]
                            self.send("* SEARCH " + " ".join(map(str, uids)))
                            self.send(f"{tag} OK SEARCH completed (Success)")
                        elif sub.upper() == "FETCH":
                            wanted_text, _, _items = params.partition(" ")
                            wanted = {int(u) for u in wanted_text.split(",") if u.isdigit()}
                            for index, message in enumerate(selected.messages, start=1):
                                if message["uid"] not in wanted:
                                    continue
                                raw = message["raw"]
                                flags = f"FLAGS ({message['flags']})"
                                if message["flags_after_body"]:
                                    head = f"* {index} FETCH (UID {message['uid']} BODY[]<0> {{{len(raw)}}}"
                                    tail = f" {flags})"
                                else:
                                    head = f"* {index} FETCH (UID {message['uid']} {flags} BODY[]<0> {{{len(raw)}}}"
                                    tail = ")"
                                self.wfile.write(head.encode() + b"\r\n" + raw + tail.encode() + b"\r\n")
                            self.send(f"{tag} OK Success")
                        else:
                            self.send(f"{tag} BAD unsupported")
                    elif command == "LOGOUT":
                        self.send("* BYE LOGOUT Requested")
                        self.send(f"{tag} OK 73 good day (Success)")
                        return
                    else:
                        self.send(f"{tag} BAD Unknown command")

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def verbs(self) -> set[str]:
        """The distinct commands seen, with UID sub-commands spelled out ("UID FETCH")."""
        out = set()
        for line in self.commands:
            parts = line.split(" ")
            out.add(" ".join(parts[:2]).upper() if parts[0].upper() == "UID" else parts[0].upper())
        return out

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
