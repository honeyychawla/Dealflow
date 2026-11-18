"""Small shared helpers. Standard library only."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().strftime(ISO_FMT)


def to_iso(dt: datetime) -> str:
    """Any datetime -> canonical UTC string. Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(ISO_FMT)


def parse_iso(value: str | None) -> datetime | None:
    """Parse the timestamp shapes the connectors hand us.

    Handles 'Z', '+0000' (Instagram), '+00:00', fractional seconds, and unix
    seconds/milliseconds given as a number or numeric string.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    text = str(value).strip()
    if re.fullmatch(r"\d{9,16}(\.\d+)?", text):
        return _from_epoch(float(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _from_epoch(number: float) -> datetime:
    if number > 1e14:  # microseconds
        number /= 1_000_000
    elif number > 1e11:  # milliseconds
        number /= 1000
    return datetime.fromtimestamp(number, tz=timezone.utc)


def norm_iso(value: Any, fallback: str | None = None) -> str:
    dt = parse_iso(value)
    if dt is None:
        return fallback or now_iso()
    return to_iso(dt)


def days_ago_iso(days: float) -> str:
    return to_iso(now() - timedelta(days=days))


def sha(*parts: Any, length: int = 20) -> str:
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:length]


def clean_text(text: str | None) -> str:
    """Normalise message text: drop zero-width chars, tame whitespace, keep line breaks."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", str(text)).translate(_ZERO_WIDTH)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def one_line(text: str | None, limit: int = 180) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    return truncate(flat, limit)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def norm_name(name: str | None) -> str:
    """Lowercased, accent-folded, punctuation-free name used for duplicate suggestions."""
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"[^a-z0-9 ]+", " ", folded.lower())
    return re.sub(r"\s+", " ", folded).strip()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def domain_of(email_address: str | None) -> str:
    if not email_address or "@" not in email_address:
        return ""
    return email_address.rsplit("@", 1)[1].lower().strip()


FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in", "outlook.com",
    "hotmail.com", "live.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
    "pm.me", "aol.com", "rediffmail.com", "zoho.com", "gmx.com", "mail.com", "hey.com",
}


def company_from_domain(domain: str) -> str:
    """'partnerships.acme.co.in' -> 'Acme'. Empty for free-mail domains."""
    if not domain or domain in FREE_MAIL:
        return ""
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "ac"}:
        core = parts[-3]
    elif len(parts) >= 2:
        core = parts[-2]
    else:
        core = parts[0]
    return core.replace("-", " ").title()
