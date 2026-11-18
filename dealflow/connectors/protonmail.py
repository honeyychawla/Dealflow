"""Email through Proton Mail Bridge: plain IMAP on localhost. Read-only.

Bridge decrypts mail locally and serves it over IMAP (127.0.0.1:1143,
STARTTLS, by default). The syncing itself is in imap.py; this file is the
part that is Proton's: reaching Bridge, and trusting only Bridge's own
certificate.

Bridge quirk handled in imap.py: it appends each message's own
<id@protonmail.internalid> to References.
"""

from __future__ import annotations

import imaplib
import os
import re
import ssl
from typing import Any

from ..config import Settings
from ..db import DB
from ..ingest import remember_account
from . import ConnectorError, NotConfigured
from .imap import NAME, ImapMail, _text, body_text, html_to_text, strip_quoted

__all__ = ["ProtonMail", "body_text", "html_to_text", "strip_quoted"]  # the helpers are re-exported

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ProtonMail(ImapMail):
    progress_name = "Proton Mail"

    def __init__(self, settings: Settings, db: DB, prefilter: dict[str, Any] | None = None):
        super().__init__(settings, db, prefilter)
        self.ns = NAME  # "email": unchanged from before several mail accounts were possible
        self.account = settings.proton_username.lower() if "@" in settings.proton_username else ""
        self.mailboxes = list(settings.proton_mailboxes)
        self.sent_mailbox = settings.proton_sent_mailbox
        self.lookback_days = settings.proton_lookback_days
        self.max_messages = settings.proton_max_messages

    def _after_login(self, conn: imaplib.IMAP4) -> None:
        if self.account:
            remember_account(self.db, NAME, self.account, self.account, "")

    # -- connection ---------------------------------------------------------

    @property
    def host(self) -> str:
        # Bridge's certificate is issued for the IP 127.0.0.1 only, so "localhost"
        # would fail hostname verification. Always dial the literal address.
        host = self.settings.proton_imap_host
        return "127.0.0.1" if host == "localhost" else host

    def _ssl_context(self, relaxed: bool = False) -> ssl.SSLContext:
        """Trust exactly one certificate: the one exported from Bridge."""
        cert = self.settings.proton_cert_path
        if cert:
            path = os.path.expanduser(cert)
            if not os.path.exists(path):
                raise ConnectorError(f"PROTON_CERT_PATH does not exist: {path}")
            context = ssl.create_default_context(cafile=path)  # verification and hostname check stay on
            if relaxed:
                # Bridge's self-signed cert can trip Python's strict RFC 5280 checks.
                # This relaxes only those; the certificate is still pinned and verified.
                context.verify_flags &= ~ssl.VERIFY_X509_STRICT
            return context
        if self.settings.proton_allow_unverified_tls and self.host in LOOPBACK:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        raise ConnectorError(
            "No Bridge certificate is set. In Bridge: Settings > Advanced settings > Export TLS certificates, "
            "then enter the full path of the exported cert.pem under Settings > Proton Mail (PROTON_CERT_PATH). "
            "Without it, any local program that grabbed Bridge's port could collect your Bridge password."
        )

    def _open(self, relaxed: bool) -> imaplib.IMAP4:
        s = self.settings
        context = self._ssl_context(relaxed)
        if s.proton_imap_security == "ssl":
            return imaplib.IMAP4_SSL(self.host, s.proton_imap_port, ssl_context=context, timeout=30)
        conn = imaplib.IMAP4(self.host, s.proton_imap_port, timeout=30)
        conn.starttls(ssl_context=context)  # never bare starttls(): that would skip verification
        return conn

    def _connect(self) -> imaplib.IMAP4:
        s = self.settings
        if not s.proton_ready:
            raise NotConfigured("PROTON_USERNAME and PROTON_BRIDGE_PASSWORD are not set")
        try:
            try:
                conn = self._open(relaxed=False)
            except ssl.SSLCertVerificationError:
                if not s.proton_cert_path:
                    raise
                conn = self._open(relaxed=True)  # still pinned and verified, minus the strict extras
        except ConnectionRefusedError:
            raise ConnectorError(
                f"Nothing is listening on {self.host}:{s.proton_imap_port}. Open Proton Mail Bridge and sign in. "
                "Bridge picks the next free port if 1143 is taken, so check the port it shows under Mailbox details."
            ) from None
        except ssl.SSLCertVerificationError as error:
            raise ConnectorError(
                f"The certificate Bridge presented does not match PROTON_CERT_PATH ({error.verify_message}). "
                "Re-export it from Bridge (Settings > Advanced settings > Export TLS certificates)."
            ) from None
        except ssl.SSLError as error:
            raise ConnectorError(
                f"TLS handshake with Bridge failed ({error.reason or error}). Bridge defaults to STARTTLS; "
                "if you switched it to SSL, set PROTON_IMAP_SECURITY=ssl (and the reverse)."
            ) from None
        except (OSError, imaplib.IMAP4.error) as error:
            raise ConnectorError(f"Could not reach Bridge over IMAP: {_text(error)}") from None
        try:
            conn.login(s.proton_username, s.proton_bridge_password)
        except imaplib.IMAP4.error as error:
            raise ConnectorError(
                "Bridge rejected the login. Use the Bridge password shown in the Bridge app for this "
                f"account (not your Proton password); it changes if you sign out of Bridge. [{_text(error)}]"
            ) from None
        return conn

    # -- diagnostics --------------------------------------------------------

    def check(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            status, listing = conn.list()
            names = []
            if status == "OK":
                for line in listing or []:
                    match = re.search(rb'"([^"]*)"\s*$|(\S+)\s*$', line or b"")
                    if match:
                        names.append((match.group(1) or match.group(2)).decode("utf-8", "replace"))
            status, capability = conn.capability()
            capabilities = (capability[0] or b"").decode("ascii", "replace") if status == "OK" and capability else ""
            return {
                "mailboxes": names,
                "pinned_cert": bool(self.settings.proton_cert_path),
                "capabilities": capabilities,
            }
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
