"""Backs the Settings page: describes every setting, and edits .env in place.

.env stays the single source of truth, so editing the file by hand and using
the Settings page are interchangeable. Two rules protect the secrets in it:

  * a secret is never sent to the browser. The page only learns whether one is
    saved and its last four characters;
  * the file is written atomically and kept readable by the owner only (0600).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .config import MAX_ACCOUNTS, Settings, account_keys, read_env_file

SECTIONS = [
    {"id": "sorting", "label": "Sorting", "title": "Sorting: Jev on OpenRouter"},
    {"id": "instagram", "label": "Instagram", "title": "Instagram"},
    {"id": "gmail", "label": "Gmail", "title": "Gmail"},
    {"id": "email", "label": "Proton Mail", "title": "Proton Mail Bridge"},
    {"id": "linkedin", "label": "LinkedIn", "title": "LinkedIn"},
    {"id": "general", "label": "General", "title": "General"},
]

# kind: text | secret | number | toggle | choice
FIELDS: list[dict[str, Any]] = [
    # -- sorting
    {"key": "OPENROUTER_API_KEY", "section": "sorting", "label": "OpenRouter API key", "kind": "secret",
     "help": "Create one at openrouter.ai/keys. Without it, conversations are sorted by keyword rules."},
    {"key": "JEV_MODEL", "section": "sorting", "label": "Decision model", "kind": "text", "default": "typesafe/jev-1.14",
     "help": "The model that sorts. typesafe/jev-1.14, or ~typesafe/jev-latest to follow new releases."},
    {"key": "WRITER_MODEL", "section": "sorting", "label": "Writer model (optional)", "kind": "text", "default": "",
     "placeholder": "a chat model slug from openrouter.ai/models",
     "help": "Jev returns decisions, not text. Set a chat model here to get one-line briefs and reply drafts."},
    {"key": "TRIAGE_CONCURRENCY", "section": "sorting", "label": "Parallel requests", "kind": "number", "default": "4",
     "min": 1, "max": 16, "help": "How many conversations are sent to Jev at once."},
    # -- instagram
    {"key": "INSTAGRAM_API_VERSION", "section": "instagram", "label": "Graph API version", "kind": "text", "default": "v27.0"},
    {"key": "INSTAGRAM_MAX_CONVERSATIONS", "section": "instagram", "label": "Conversations on first sync", "kind": "number",
     "default": "200", "min": 10, "max": 2000, "help": "How many recent conversations the first sync reads."},
    {"key": "INSTAGRAM_ENRICH_PROFILES", "section": "instagram", "label": "Look up follower count and verification", "kind": "toggle",
     "default": "true", "help": "One extra request per new person. Feeds the credibility signal."},
    {"key": "INSTAGRAM_API_HOST", "section": "instagram", "label": "API host", "kind": "choice", "default": "graph.instagram.com",
     "options": [["graph.instagram.com", "graph.instagram.com"], ["graph.facebook.com", "graph.facebook.com"]],
     "advanced": True, "help": "Chosen automatically from the token. Only used for a token with an unrecognised prefix."},
    # -- gmail (settings shared by every Gmail account)
    {"key": "GOOGLE_CLIENT_ID", "section": "gmail", "label": "Google OAuth client ID", "kind": "text", "default": "",
     "placeholder": "…apps.googleusercontent.com",
     "help": "For Sign in with Google: from your own OAuth client in Google Cloud (Credentials, type Desktop app). See the steps below."},
    {"key": "GOOGLE_CLIENT_SECRET", "section": "gmail", "label": "Google OAuth client secret", "kind": "secret",
     "help": "From the same OAuth client. Removing it stops every Google-signed-in account from syncing."},
    {"key": "GMAIL_LOOKBACK_DAYS", "section": "gmail", "label": "Days of history on first sync", "kind": "number",
     "default": "30", "min": 1, "max": 3650},
    {"key": "GMAIL_PRIMARY_ONLY", "section": "gmail", "label": "Only the Primary tab", "kind": "toggle", "default": "false",
     "help": "Skip what Gmail files under Promotions, Social, Updates and Forums. Off, everything in the inbox is read and newsletters are filed as Noise for free."},
    {"key": "GMAIL_SYNC_SENT", "section": "gmail", "label": "Read sent mail too", "kind": "toggle", "default": "true",
     "help": "So the app knows which conversations you already answered."},
    {"key": "GMAIL_MAILBOXES", "section": "gmail", "label": "Mailboxes to read", "kind": "text", "default": "INBOX",
     "advanced": True, "help": "Comma-separated. A Gmail label is a mailbox of the same name, e.g. INBOX,Deals."},
    # -- email
    {"key": "PROTON_USERNAME", "section": "email", "label": "Username", "kind": "text", "default": "",
     "placeholder": "you@proton.me", "help": "Shown in Bridge under your account > Mailbox details."},
    {"key": "PROTON_BRIDGE_PASSWORD", "section": "email", "label": "Bridge password", "kind": "secret",
     "help": "The password Bridge generated, from Mailbox details. Not your Proton account password."},
    {"key": "PROTON_IMAP_PORT", "section": "email", "label": "IMAP port", "kind": "number", "default": "1143",
     "min": 1, "max": 65535, "help": "Bridge uses 1143 unless it was taken. Copy the port Bridge shows."},
    {"key": "PROTON_CERT_PATH", "section": "email", "label": "Bridge certificate (cert.pem)", "kind": "text", "default": "",
     "placeholder": "/Users/you/bridge/cert.pem",
     "help": "Bridge > Settings > Advanced settings > Export TLS certificates. The only certificate the app will trust."},
    {"key": "PROTON_MAILBOXES", "section": "email", "label": "Mailboxes to read", "kind": "text", "default": "INBOX",
     "help": "Comma-separated. Folders look like Folders/Deals, labels like Labels/Press."},
    {"key": "PROTON_SENT_MAILBOX", "section": "email", "label": "Sent mailbox", "kind": "text", "default": "Sent",
     "help": "Read so the app knows what you already answered. Leave blank to skip."},
    {"key": "PROTON_LOOKBACK_DAYS", "section": "email", "label": "Days of history on first sync", "kind": "number",
     "default": "30", "min": 1, "max": 3650},
    {"key": "PROTON_IMAP_SECURITY", "section": "email", "label": "Connection security", "kind": "choice", "default": "starttls",
     "options": [["starttls", "STARTTLS (Bridge default)"], ["ssl", "SSL"]], "advanced": True},
    {"key": "PROTON_IMAP_HOST", "section": "email", "label": "IMAP host", "kind": "text", "default": "127.0.0.1", "advanced": True},
    # -- linkedin
    {"key": "LINKEDIN_OPEN_THREADS", "section": "linkedin", "label": "Conversations to open per sync", "kind": "number",
     "default": "5", "min": 0, "max": 15,
     "help": "The inbox list already carries each conversation's newest message. Opening one adds its earlier "
             "messages. Only conversations you have already read on LinkedIn are opened, so nothing gets marked as "
             "read. 0 reads the list only."},
    {"key": "LINKEDIN_BROWSER_VISIBLE", "section": "linkedin", "label": "Show the browser window while it syncs",
     "kind": "toggle", "default": "true",
     "help": "On, a browser window opens for about half a minute so you can see what it does. Off, it runs out of sight."},
    {"key": "LINKEDIN_STALE_HOURS", "section": "linkedin", "label": "Flag as stale after (hours)", "kind": "number",
     "default": "24", "min": 1, "max": 720, "help": "How long since the last collection before the app warns you."},
    {"key": "LINKEDIN_OPEN_UNREAD", "section": "linkedin", "label": "Also open unread conversations", "kind": "toggle",
     "default": "false", "advanced": True,
     "help": "Opening a conversation marks it as read on LinkedIn, and the sender may see a read receipt. "
             "Leave this off to keep your LinkedIn inbox exactly as you left it."},
    {"key": "LINKEDIN_BROWSER_PATH", "section": "linkedin", "label": "Browser to use", "kind": "text", "default": "",
     "advanced": True, "placeholder": "/Applications/Google Chrome.app",
     "help": "Blank uses the first of Chrome, Chromium, Brave or Edge found on this computer."},
    # -- general
    {"key": "OWNER_NAME", "section": "general", "label": "Your name", "kind": "text", "default": "",
     "help": "As it appears on LinkedIn. Used to tell your messages from theirs in the export, and to sign email drafts."},
    {"key": "OWNER_EMAILS", "section": "general", "label": "Your email addresses", "kind": "text", "default": "",
     "placeholder": "you@proton.me, alias@pm.me", "help": "Comma-separated, including aliases. Mail from these counts as sent by you."},
    {"key": "SYNC_INTERVAL_MINUTES", "section": "general", "label": "Sync every (minutes)", "kind": "number", "default": "15",
     "min": 0, "max": 1440, "help": "0 turns the timer off: sync only when you press Sync."},
    {"key": "AUTO_TRIAGE", "section": "general", "label": "Sort new messages automatically", "kind": "toggle", "default": "true"},
    {"key": "DEALFLOW_PORT", "section": "general", "label": "App port", "kind": "number", "default": "4747",
     "min": 1024, "max": 65535, "help": "Takes effect the next time the app is started.", "advanced": True},
]
BY_KEY = {field["key"]: field for field in FIELDS}
KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")

# Connectors that hold several of the owner's accounts. Each account has a few
# settings, stored under keys that depend on its slot (see config.account_keys).
ACCOUNT_KINDS: dict[str, dict[str, dict[str, Any]]] = {
    "instagram": {
        "token": {"label": "Access token", "kind": "secret",
                  "help": "From your Meta app dashboard, under Instagram > API setup. Unified login gives a token starting with EAA (also fill in the account ID); Instagram login gives one starting with IGAA. The app detects which."},
        "user_id": {"label": "Instagram account ID", "kind": "text", "placeholder": "1784…",
                    "help": "Needed with a Unified login token (EAA…): the number shown next to this account in the Meta app dashboard. Leave blank for an IGAA token."},
        "label": {"label": "Name in the inbox (optional)", "kind": "text", "placeholder": "e.g. Personal, Brand",
                  "help": "Shown on this account's conversations. Defaults to its @username."},
    },
    "gmail": {
        "address": {"label": "Gmail address", "kind": "text", "placeholder": "you@gmail.com",
                    "help": "A Gmail or Google Workspace address."},
        "password": {"label": "App password", "kind": "secret",
                     "help": "Only if you are not using Sign in with Google: 16 characters from myaccount.google.com/apppasswords, made for this address. Not your Google password. 2-Step Verification must be on."},
        "label": {"label": "Name in the inbox (optional)", "kind": "text", "placeholder": "e.g. Work, Personal",
                  "help": "Shown on this account's conversations. Defaults to the address."},
        # Written by the Sign in with Google round trip, never typed in; shown as a status, not a field.
        "refresh_token": {"label": "Google sign-in", "kind": "secret", "managed": True},
    },
}


def account_field(key: str) -> dict[str, Any] | None:
    """The spec for an account key such as INSTAGRAM_2_ACCESS_TOKEN or GMAIL_ADDRESS, or None."""
    for connector, roles in ACCOUNT_KINDS.items():
        for slot in range(1, MAX_ACCOUNTS + 1):
            for role, name in account_keys(connector, slot).items():
                if name == key:
                    return {"key": key, "section": connector, "connector": connector, "role": role, "slot": slot, **roles[role]}
    return None


class SettingsError(ValueError):
    pass


def _locked(key: str) -> bool:
    """True when the process environment sets this key, which outranks .env."""
    return bool(os.environ.get(key))


def describe(settings: Settings) -> dict[str, Any]:
    """Everything the Settings page needs, with secrets reduced to set/not-set."""
    stored = read_env_file(settings.env_path)
    fields = []
    for field in FIELDS:
        key = field["key"]
        raw = os.environ.get(key) if _locked(key) else stored.get(key, "")
        entry = {k: v for k, v in field.items() if k != "default"}
        entry["locked"] = _locked(key)
        entry["default"] = field.get("default", "")
        if field["kind"] == "secret":
            entry["set"] = bool(raw)
            entry["hint"] = raw[-4:] if raw and len(raw) >= 12 else ""
        else:
            entry["value"] = raw if raw not in (None, "") else field.get("default", "")
            entry["is_default"] = raw in (None, "")
        fields.append(entry)

    def raw(key: str) -> str:
        return (os.environ.get(key) if _locked(key) else stored.get(key, "")) or ""

    accounts: dict[str, Any] = {}
    for connector, roles in ACCOUNT_KINDS.items():
        slots = []
        for slot in range(1, MAX_ACCOUNTS + 1):
            keys = account_keys(connector, slot)
            raws = {role: raw(keys[role]) for role in roles}
            if not any(value.strip() for value in raws.values()):
                continue
            slots.append({
                "slot": slot,
                "keys": keys,
                # a secret is reduced to "is one saved" and its last four characters
                "values": {role: raws[role].strip() for role, spec in roles.items() if spec["kind"] != "secret"},
                "secrets": {
                    role: {
                        "set": bool(raws[role]),
                        "hint": raws[role][-4:] if len(raws[role]) >= 12 and not spec.get("managed") else "",
                    }
                    for role, spec in roles.items() if spec["kind"] == "secret"
                },
                "locked": {role: _locked(keys[role]) for role in roles},
            })
        accounts[connector] = {
            "slots": slots,
            "fields": roles,
            "key_names": {str(slot): account_keys(connector, slot) for slot in range(1, MAX_ACCOUNTS + 1)},
            "max": MAX_ACCOUNTS,
        }
    return {"sections": SECTIONS, "fields": fields, "env_file": str(settings.env_path), "accounts": accounts}


def validate(changes: dict[str, Any]) -> dict[str, str | None]:
    """Normalise a {KEY: value} patch. None or "" for a key removes it."""
    clean: dict[str, str | None] = {}
    for key, value in changes.items():
        field = BY_KEY.get(key) or account_field(key)
        if field is None:
            raise SettingsError(f"Unknown setting {key}")
        if _locked(key):
            raise SettingsError(f"{field['label']} is set in the environment and cannot be changed here")
        if value is None or value == "":
            clean[key] = None
            continue
        if field.get("managed"):
            raise SettingsError(f"{field['label']} is saved by the Sign in with Google button, not typed in")
        kind = field["kind"]
        if kind == "toggle":
            clean[key] = "true" if value in (True, "true", "1", "on", "yes") else "false"
            continue
        text = str(value).strip()
        if "\n" in text or "\r" in text or "\x00" in text:
            raise SettingsError(f"{field['label']} cannot contain line breaks")
        if len(text) > 4096:
            raise SettingsError(f"{field['label']} is too long")
        if kind == "number":
            try:
                number = int(text)
            except ValueError:
                raise SettingsError(f"{field['label']} must be a whole number") from None
            low, high = field.get("min"), field.get("max")
            if (low is not None and number < low) or (high is not None and number > high):
                raise SettingsError(f"{field['label']} must be between {low} and {high}")
            text = str(number)
        elif kind == "choice":
            if text not in [option[0] for option in field["options"]]:
                raise SettingsError(f"{field['label']}: {text!r} is not one of the options")
        elif key == "PROTON_CERT_PATH":
            path = Path(text).expanduser()
            if not path.is_file():
                raise SettingsError(f"No file at {path}. Export the certificate from Bridge first, then paste its full path.")
            text = str(path)
        elif field.get("role") == "user_id":
            if not text.isdigit():
                raise SettingsError("The Instagram account ID is a number, for example 17841400000000000")
        elif field.get("role") == "token":
            if re.search(r"\s", text):
                raise SettingsError("The access token cannot contain spaces. Paste it again in one piece.")
        elif field.get("role") == "address":
            text = text.lower()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
                raise SettingsError(f"{text!r} is not an email address")
        elif field.get("role") == "password":
            text = re.sub(r"\s+", "", text)  # Google shows it as four groups of four
            if len(text) < 8:
                raise SettingsError(
                    "That is too short to be a Google app password: it is 16 characters, from myaccount.google.com/apppasswords"
                )
        elif field.get("role") == "label":
            if len(text) > 40:
                raise SettingsError("Keep the account name under 40 characters")
        elif key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
            if re.search(r"\s", text):
                raise SettingsError(f"{field['label']} cannot contain spaces. Paste it again in one piece.")
            if key == "GOOGLE_CLIENT_ID" and not text.endswith(".apps.googleusercontent.com"):
                raise SettingsError("A Google OAuth client ID ends in .apps.googleusercontent.com")
        elif key in ("JEV_MODEL", "WRITER_MODEL", "INSTAGRAM_API_VERSION"):
            if re.search(r"\s", text):
                raise SettingsError(f"{field['label']} cannot contain spaces")
        if '"' in text and "'" in text:
            raise SettingsError(f"{field['label']} cannot contain both kinds of quote")
        clean[key] = text
    return clean


def _format(key: str, value: str) -> str:
    if re.search(r"[\s#\"']", value):
        quote = "'" if '"' in value else '"'
        return f"{key}={quote}{value}{quote}"
    return f"{key}={value}"


def write_env(path: Path, changes: dict[str, str | None]) -> None:
    """Apply changes to .env, keeping comments, order and unrelated keys."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(changes)
    out: list[str] = []
    for line in lines:
        match = KEY_LINE.match(line)
        key = match.group(1) if match else None
        if key in pending:
            value = pending.pop(key)
            if value is not None:
                out.append(_format(key, value))
            continue  # removed, or a duplicate of a key we already rewrote
        if key in changes:
            continue
        out.append(line)
    additions = [(key, value) for key, value in pending.items() if value is not None]
    if additions:
        if out and out[-1].strip():
            out.append("")
        out.extend(_format(key, value) for key, value in additions)

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp:
            temp.write("\n".join(out) + "\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


# -- sorting policy (rubric.json) -----------------------------------------------


def describe_rubric(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        "owner_context": raw.get("owner_context", ""),
        "categories": [
            {"id": c["id"], "label": c.get("label", c["id"]), "value": c.get("value", 0), "criteria": c.get("criteria", "")}
            for c in raw.get("categories", [])
        ],
        "tiers": (raw.get("scoring") or {}).get("tiers", {}),
        "file": str(path),
    }


def update_rubric(path: Path, patch: dict[str, Any]) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "owner_context" in patch:
        context = str(patch["owner_context"]).strip()
        if not context:
            raise SettingsError("Describe yourself in a sentence or two: it is what Jev judges every message against")
        if len(context) > 3000:
            raise SettingsError("Keep the description under 3,000 characters")
        raw["owner_context"] = context
    values = patch.get("category_values") or {}
    criteria = patch.get("category_criteria") or {}
    known = {c["id"] for c in raw.get("categories", [])}
    for category_id in [*values, *criteria]:
        if category_id not in known:
            raise SettingsError(f"Unknown category {category_id}")
    for category in raw.get("categories", []):
        if category["id"] in values:
            try:
                value = float(values[category["id"]])
            except (TypeError, ValueError):
                raise SettingsError("Category importance must be a number") from None
            if not 0 <= value <= 1:
                raise SettingsError("Category importance must be between 0 and 1")
            category["value"] = round(value, 2)
        if category["id"] in criteria:
            text = str(criteria[category["id"]]).strip()
            if not text:
                raise SettingsError(f"{category.get('label', category['id'])} needs a description")
            category["criteria"] = text[:1200]
    tiers = patch.get("tiers")
    if tiers:
        try:
            p0, p1, p2 = (float(tiers[t]) for t in ("P0", "P1", "P2"))
        except (KeyError, TypeError, ValueError):
            raise SettingsError("Priority cut-offs need a number for each of P0, P1 and P2") from None
        if not 100 >= p0 > p1 > p2 >= 0:
            raise SettingsError("Priority cut-offs must descend: Act now above This week above Worth a look")
        raw.setdefault("scoring", {})["tiers"] = {"P0": p0, "P1": p1, "P2": p2}

    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".rubric.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp:
            json.dump(raw, temp, indent=2, ensure_ascii=False)
            temp.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise
