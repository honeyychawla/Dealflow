"""Settings, read from the process environment and a local .env file.

Secrets live in .env (git-ignored). This module only reads it; the Settings
page edits it through settings_store. Nothing here logs a value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _list(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


MAX_ACCOUNTS = 8  # per channel
MAX_INSTAGRAM_ACCOUNTS = MAX_ACCOUNTS

# The settings each kind of account has: role -> the end of its .env key.
ACCOUNT_KEY_SUFFIXES = {
    "instagram": {"token": "ACCESS_TOKEN", "user_id": "USER_ID", "label": "LABEL"},
    # refresh_token is written by "Sign in with Google", never typed in.
    "gmail": {"address": "ADDRESS", "password": "APP_PASSWORD", "label": "LABEL", "refresh_token": "REFRESH_TOKEN"},
}


def account_keys(kind: str, slot: int) -> dict[str, str]:
    """The .env keys of one account: role -> key.

    Account 1 uses unnumbered names (INSTAGRAM_ACCESS_TOKEN, GMAIL_ADDRESS), so
    an Instagram setup made before several accounts were supported keeps
    working unchanged. Accounts 2 and up are numbered: INSTAGRAM_2_ACCESS_TOKEN,
    GMAIL_2_ADDRESS, ...
    """
    prefix = f"{kind.upper()}_" if slot == 1 else f"{kind.upper()}_{slot}_"
    return {role: prefix + suffix for role, suffix in ACCOUNT_KEY_SUFFIXES[kind].items()}


def instagram_keys(slot: int) -> dict[str, str]:
    return account_keys("instagram", slot)


@dataclass(frozen=True)
class InstagramAccount:
    slot: int  # position in the settings, 1..MAX_INSTAGRAM_ACCOUNTS; not an identity
    token: str
    user_id: str = ""  # Instagram account id (Unified Login) or Page id (Facebook Login)
    label: str = ""  # optional name the owner gave it

    @property
    def namespace(self) -> str:
        """Where this account's sync state lives. Slot 1 keeps the pre-multi-account name."""
        return "instagram" if self.slot == 1 else f"instagram:{self.slot}"


@dataclass(frozen=True)
class GmailAccount:
    slot: int
    address: str
    password: str  # a Google app password, never the account password
    label: str = ""
    refresh_token: str = ""  # from "Sign in with Google"; when set, the app password is not used

    @property
    def namespace(self) -> str:
        return "gmail" if self.slot == 1 else f"gmail:{self.slot}"

    @property
    def signed_in_with_google(self) -> bool:
        return bool(self.refresh_token)


@dataclass
class Settings:
    # App
    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    env_path: Path = ROOT / ".env"
    rubric_file: Path | None = None  # defaults to rubric.json next to the code
    host: str = "127.0.0.1"
    port: int = 4747
    sync_interval_minutes: int = 15
    auto_triage: bool = True

    # Who "me" is. Used to tell inbound from outbound.
    owner_name: str = ""
    owner_emails: list[str] = field(default_factory=list)

    # OpenRouter / Jev
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai"
    jev_model: str = "typesafe/jev-1.14"
    writer_model: str = ""  # optional chat model for summaries and reply drafts
    triage_concurrency: int = 4

    # Instagram. The three fields below are account 1; further accounts are in
    # instagram_more_accounts. Read them all through `instagram_accounts`.
    instagram_access_token: str = ""
    instagram_user_id: str = ""  # Instagram account id (Unified Login) or Page id; blank for IGAA tokens
    instagram_label: str = ""
    instagram_more_accounts: list[InstagramAccount] = field(default_factory=list)
    instagram_api_host: str = "graph.instagram.com"
    instagram_api_version: str = "v27.0"
    instagram_max_conversations: int = 200
    instagram_enrich_profiles: bool = True

    # Gmail: per account either "Sign in with Google" (Gmail API, read-only
    # scope) or IMAP with an app password. Account 1 is the four fields below.
    gmail_address: str = ""
    gmail_app_password: str = ""
    gmail_label: str = ""
    gmail_refresh_token: str = ""
    gmail_more_accounts: list[GmailAccount] = field(default_factory=list)
    # The owner's own OAuth client for "Sign in with Google" (Google Cloud > Credentials).
    google_client_id: str = ""
    google_client_secret: str = ""
    gmail_mailboxes: list[str] = field(default_factory=lambda: ["INBOX"])
    gmail_sync_sent: bool = True
    gmail_primary_only: bool = False
    gmail_lookback_days: int = 30
    gmail_max_messages: int = 1500

    # Proton Mail Bridge (IMAP on localhost)
    proton_imap_host: str = "127.0.0.1"
    proton_imap_port: int = 1143
    proton_imap_security: str = "starttls"  # starttls | ssl
    proton_username: str = ""
    proton_bridge_password: str = ""
    proton_cert_path: str = ""  # exported Bridge cert.pem; the only certificate trusted
    proton_allow_unverified_tls: bool = False  # loopback-only escape hatch, off by default
    proton_mailboxes: list[str] = field(default_factory=lambda: ["INBOX"])
    proton_sent_mailbox: str = "Sent"
    proton_lookback_days: int = 30
    proton_max_messages: int = 1500

    # LinkedIn: read in the owner's own browser when they press Sync (connectors/linkedin_browser.py),
    # by Claude (collectors/linkedin-sync.md), or imported from LinkedIn's data export.
    linkedin_stale_hours: int = 24
    linkedin_browser_visible: bool = True  # show the browser window while it syncs
    linkedin_open_threads: int = 5  # conversations opened per sync for their earlier messages; 0 = the list only
    linkedin_open_unread: bool = False  # opening marks a conversation read on LinkedIn
    linkedin_browser_path: str = ""  # blank: the first of Chrome, Chromium, Brave, Edge found

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dealflow.db"

    @property
    def rubric_path(self) -> Path:
        return self.rubric_file or self.root / "rubric.json"

    @property
    def jev_ready(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def instagram_accounts(self) -> list[InstagramAccount]:
        """Every Instagram account that has a token, in slot order."""
        accounts = []
        if self.instagram_access_token:
            accounts.append(
                InstagramAccount(1, self.instagram_access_token, self.instagram_user_id, self.instagram_label)
            )
        accounts.extend(a for a in self.instagram_more_accounts if a.token and a.slot != 1)
        return sorted(accounts, key=lambda account: account.slot)

    @property
    def instagram_ready(self) -> bool:
        return bool(self.instagram_accounts)

    @property
    def gmail_accounts(self) -> list[GmailAccount]:
        """Every Gmail account with an address and a way in (Google sign-in or app password), in slot order."""
        accounts = []
        if self.gmail_address and (self.gmail_refresh_token or self.gmail_app_password):
            accounts.append(GmailAccount(
                1, self.gmail_address, self.gmail_app_password, self.gmail_label, self.gmail_refresh_token
            ))
        accounts.extend(
            a for a in self.gmail_more_accounts if a.address and (a.refresh_token or a.password) and a.slot != 1
        )
        return sorted(accounts, key=lambda account: account.slot)

    @property
    def gmail_ready(self) -> bool:
        return bool(self.gmail_accounts)

    @property
    def google_ready(self) -> bool:
        """True when "Sign in with Google" can be used: the owner's OAuth client is saved."""
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def proton_ready(self) -> bool:
        return bool(self.proton_username and self.proton_bridge_password)


def load_settings(env_path: Path | None = None) -> Settings:
    env_path = Path(env_path or os.environ.get("DEALFLOW_ENV_FILE") or ROOT / ".env").expanduser()
    if not env_path.is_absolute():
        env_path = ROOT / env_path
    file_values = read_env_file(env_path)

    def get(key: str, default: str | None = None) -> str | None:
        # Real environment wins over .env so one-off overrides work.
        if key in os.environ and os.environ[key] != "":
            return os.environ[key]
        return file_values.get(key, default)

    base = Settings()
    data_dir = Path(get("DEALFLOW_DATA_DIR") or base.data_dir).expanduser()
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir

    rubric_file = os.environ.get("DEALFLOW_RUBRIC_FILE")
    settings = Settings(
        data_dir=data_dir,
        env_path=env_path,
        rubric_file=Path(rubric_file).expanduser() if rubric_file else None,
        host=get("DEALFLOW_HOST") or base.host,
        port=_int(get("DEALFLOW_PORT"), base.port),
        sync_interval_minutes=_int(get("SYNC_INTERVAL_MINUTES"), base.sync_interval_minutes),
        auto_triage=_bool(get("AUTO_TRIAGE"), base.auto_triage),
        owner_name=get("OWNER_NAME") or "",
        owner_emails=[e.lower() for e in _list(get("OWNER_EMAILS"))],
        openrouter_api_key=get("OPENROUTER_API_KEY") or "",
        openrouter_base_url=(get("OPENROUTER_BASE_URL") or base.openrouter_base_url).rstrip("/"),
        jev_model=get("JEV_MODEL") or base.jev_model,
        writer_model=get("WRITER_MODEL") or "",
        triage_concurrency=max(1, _int(get("TRIAGE_CONCURRENCY"), base.triage_concurrency)),
        instagram_access_token=get("INSTAGRAM_ACCESS_TOKEN") or "",
        instagram_user_id=(get("INSTAGRAM_USER_ID") or "").strip(),
        instagram_label=(get("INSTAGRAM_LABEL") or "").strip(),
        instagram_more_accounts=[
            InstagramAccount(
                slot,
                get(instagram_keys(slot)["token"]) or "",
                (get(instagram_keys(slot)["user_id"]) or "").strip(),
                (get(instagram_keys(slot)["label"]) or "").strip(),
            )
            for slot in range(2, MAX_INSTAGRAM_ACCOUNTS + 1)
            if get(instagram_keys(slot)["token"])
        ],
        instagram_api_host=get("INSTAGRAM_API_HOST") or base.instagram_api_host,
        instagram_api_version=get("INSTAGRAM_API_VERSION") or base.instagram_api_version,
        instagram_max_conversations=_int(
            get("INSTAGRAM_MAX_CONVERSATIONS"), base.instagram_max_conversations
        ),
        instagram_enrich_profiles=_bool(
            get("INSTAGRAM_ENRICH_PROFILES"), base.instagram_enrich_profiles
        ),
        gmail_address=(get("GMAIL_ADDRESS") or "").strip().lower(),
        gmail_app_password=get("GMAIL_APP_PASSWORD") or "",
        gmail_label=(get("GMAIL_LABEL") or "").strip(),
        gmail_refresh_token=(get("GMAIL_REFRESH_TOKEN") or "").strip(),
        gmail_more_accounts=[
            GmailAccount(
                slot,
                (get(account_keys("gmail", slot)["address"]) or "").strip().lower(),
                get(account_keys("gmail", slot)["password"]) or "",
                (get(account_keys("gmail", slot)["label"]) or "").strip(),
                (get(account_keys("gmail", slot)["refresh_token"]) or "").strip(),
            )
            for slot in range(2, MAX_ACCOUNTS + 1)
            if get(account_keys("gmail", slot)["address"]) or get(account_keys("gmail", slot)["password"])
            or get(account_keys("gmail", slot)["refresh_token"])
        ],
        google_client_id=(get("GOOGLE_CLIENT_ID") or "").strip(),
        google_client_secret=(get("GOOGLE_CLIENT_SECRET") or "").strip(),
        gmail_mailboxes=_list(get("GMAIL_MAILBOXES")) or base.gmail_mailboxes,
        gmail_sync_sent=_bool(get("GMAIL_SYNC_SENT"), base.gmail_sync_sent),
        gmail_primary_only=_bool(get("GMAIL_PRIMARY_ONLY"), base.gmail_primary_only),
        gmail_lookback_days=_int(get("GMAIL_LOOKBACK_DAYS"), base.gmail_lookback_days),
        gmail_max_messages=_int(get("GMAIL_MAX_MESSAGES"), base.gmail_max_messages),
        proton_imap_host=get("PROTON_IMAP_HOST") or base.proton_imap_host,
        proton_imap_port=_int(get("PROTON_IMAP_PORT"), base.proton_imap_port),
        proton_imap_security=(get("PROTON_IMAP_SECURITY") or base.proton_imap_security).lower(),
        proton_username=get("PROTON_USERNAME") or "",
        proton_bridge_password=get("PROTON_BRIDGE_PASSWORD") or "",
        proton_cert_path=get("PROTON_CERT_PATH") or "",
        proton_allow_unverified_tls=_bool(get("PROTON_ALLOW_UNVERIFIED_TLS"), False),
        proton_mailboxes=_list(get("PROTON_MAILBOXES")) or base.proton_mailboxes,
        proton_sent_mailbox=get("PROTON_SENT_MAILBOX", base.proton_sent_mailbox) or "",
        proton_lookback_days=_int(get("PROTON_LOOKBACK_DAYS"), base.proton_lookback_days),
        proton_max_messages=_int(get("PROTON_MAX_MESSAGES"), base.proton_max_messages),
        linkedin_stale_hours=_int(get("LINKEDIN_STALE_HOURS"), base.linkedin_stale_hours),
        linkedin_browser_visible=_bool(get("LINKEDIN_BROWSER_VISIBLE"), base.linkedin_browser_visible),
        linkedin_open_threads=max(0, min(15, _int(get("LINKEDIN_OPEN_THREADS"), base.linkedin_open_threads))),
        linkedin_open_unread=_bool(get("LINKEDIN_OPEN_UNREAD"), base.linkedin_open_unread),
        linkedin_browser_path=(get("LINKEDIN_BROWSER_PATH") or "").strip(),
    )
    # Every address the owner syncs is the owner: mail sent from one of them is "mine".
    own = [settings.proton_username.lower()] if "@" in settings.proton_username else []
    own += [account.address for account in settings.gmail_accounts]
    for address in own:
        if address and address not in settings.owner_emails:
            settings.owner_emails.append(address)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
