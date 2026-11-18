"""Gmail over IMAP, signed in with a Google app password. Read-only. One instance per account.

Why IMAP and an app password: it needs no Google Cloud project and no OAuth
consent screen, and Google still supports it for personal and Workspace
accounts (https://myaccount.google.com/apppasswords, needs 2-Step
Verification). It is not available when a Workspace admin has switched app
passwords off, when the account uses Advanced Protection, or when 2-Step
Verification is set up with security keys only.

The syncing itself is in imap.py. This file is the part that is Gmail's:

  * imap.gmail.com:993 over TLS, verified against the system's root
    certificates;
  * the Sent mailbox is found by its special-use flag, because its name is
    translated ("[Gmail]/Sent Mail", "[Gmail]/Gesendet", ...);
  * "Primary tab only" uses Gmail's own search (X-GM-RAW category:primary);
  * "open" links go to the conversation in Gmail's web app, in the right
    account.
"""

from __future__ import annotations

import imaplib
import re
import ssl
from typing import Any
from urllib.parse import quote

from ..config import GmailAccount, Settings
from ..db import DB
from ..ingest import remember_account
from ..net import explain_tls_failure, ssl_context
from . import ConnectorError, NotConfigured
from .imap import NAME, ImapMail, _quote, _text

HOST = "imap.gmail.com"
PORT = 993
APP_PASSWORD_URL = "https://myaccount.google.com/apppasswords"


class Gmail(ImapMail):
    progress_name = "Gmail"

    def __init__(self, settings: Settings, db: DB, account: GmailAccount, prefilter: dict[str, Any] | None = None):
        super().__init__(settings, db, prefilter)
        self.gmail = account
        self.ns = account.namespace  # one cursor set per account
        self.account = account.address.strip().lower()
        self.progress_name = f"Gmail {account.label or self.account}"
        self.mailboxes = list(settings.gmail_mailboxes)
        self.sent_mailbox = ""  # discovered on the server, see _boxes
        self.lookback_days = settings.gmail_lookback_days
        self.max_messages = settings.gmail_max_messages
        if settings.gmail_primary_only:
            self.extra_search = ("X-GM-RAW", '"category:primary"')
        # Mail from any of the owner's addresses counts as sent by the owner.
        self.owner_emails = sorted({*settings.owner_emails, self.account})

    @property
    def display(self) -> str:
        return self.gmail.label or self.account

    # -- connection ---------------------------------------------------------

    def _open(self) -> imaplib.IMAP4:
        return imaplib.IMAP4_SSL(HOST, PORT, ssl_context=ssl_context(), timeout=30)

    def _connect(self) -> imaplib.IMAP4:
        if not (self.account and self.gmail.password):
            raise NotConfigured("This Gmail account needs both its address and an app password")
        try:
            conn = self._open()
        except ssl.SSLError as error:
            raise ConnectorError(explain_tls_failure(error) or f"TLS handshake with Gmail failed: {error}") from None
        except OSError as error:
            raise ConnectorError(f"Could not reach {HOST}. Check the internet connection. [{error}]") from None
        # Google shows an app password as four groups of four; the spaces are not part of it.
        password = re.sub(r"\s+", "", self.gmail.password)
        try:
            conn.login(self.account, password)
        except imaplib.IMAP4.error as error:
            try:
                conn.shutdown()
            except Exception:  # noqa: BLE001
                pass
            raise ConnectorError(_explain_login(_text(error), self.account)) from None
        return conn

    def _after_login(self, conn: imaplib.IMAP4) -> None:
        remember_account(self.db, NAME, self.account, self.account, self.gmail.label)

    def _boxes(self, conn: imaplib.IMAP4) -> list[tuple[str, str]]:
        boxes = [(name, "in") for name in self.mailboxes]
        if self.settings.gmail_sync_sent:
            # Read so the app knows what the owner already answered.
            boxes.append((self.special_mailbox(conn, "\\Sent") or "[Gmail]/Sent Mail", "out"))
        return boxes

    def _thread_url(self, item: dict[str, Any], address: str, subject: str) -> str:
        """Open the conversation in Gmail, in this account, by its Message-ID."""
        message_id = item["message_id"].strip("<>")
        if message_id.startswith("generated-"):
            return f"https://mail.google.com/mail/u/{quote(self.account, safe='@')}/"
        return (
            f"https://mail.google.com/mail/u/{quote(self.account, safe='@')}/"
            f"#search/rfc822msgid:{quote(message_id, safe='')}"
        )

    # -- diagnostics --------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """Sign in and look around, without reading any message."""
        conn = self._connect()
        try:
            self._after_login(conn)
            status, capability = conn.capability()
            capabilities = (capability[0] or b"").decode("ascii", "replace") if status == "OK" and capability else ""
            names = [name for name, _attributes in self.list_mailboxes(conn)]
            missing = [name for name in self.mailboxes if name not in names and name.upper() != "INBOX"]
            status, count = conn.select(_quote("INBOX"), readonly=True)
            inbox = int(count[0]) if status == "OK" and count and count[0] else 0
            return {
                "address": self.account,
                "inbox_messages": inbox,
                "sent_mailbox": self.special_mailbox(conn, "\\Sent"),
                "gmail_extensions": "X-GM-EXT-1" in capabilities,
                "missing_mailboxes": missing,
                "mailboxes": names,
            }
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass


def _explain_login(server_said: str, address: str) -> str:
    """Turn Gmail's login refusals into what to do about them."""
    text = server_said.lower()
    if "application-specific password" in text:
        return (
            f"Gmail wants an app password for {address}, not the normal account password. "
            f"Create one at {APP_PASSWORD_URL} (2-Step Verification must be on) and paste the 16 characters here."
        )
    if "not enabled for imap" in text or "imap access is disabled" in text:
        return (
            f"IMAP is switched off for {address}. Turn it on in Gmail: Settings > See all settings > "
            "Forwarding and POP/IMAP > Enable IMAP. On a work account the admin may have disabled it."
        )
    if "web browser" in text or "webalert" in text:
        return (
            f"Google blocked this sign-in to {address} and wants it confirmed in a browser. Sign in to Gmail "
            "in your browser, approve the security prompt, then test again."
        )
    if "invalid credentials" in text or "authenticationfailed" in text:
        return (
            f"Gmail rejected the sign-in for {address}. Check the address, and that the password is an app "
            f"password from {APP_PASSWORD_URL}, not the account password. Changing the Google account password "
            "revokes app passwords, so a new one may be needed. On a work account, the admin may not allow them."
        )
    return f"Gmail refused the sign-in for {address}. [{server_said[:200]}]"
