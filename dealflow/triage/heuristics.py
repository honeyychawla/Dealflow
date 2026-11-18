"""Keyword triage and fact extraction that need no API.

Two jobs:
  1. A stand-in sorter that produces the same signals as Jev, so the app is
     usable before an OpenRouter key is configured. It is crude on purpose and
     every verdict it makes is labelled "rules" in the UI.
  2. Cheap regex extraction of the facts people scan for (amounts, links,
     deadlines), which Jev cannot return because it emits no text.
"""

from __future__ import annotations

import re
from typing import Any

from ..util import FREE_MAIL, clamp, one_line

_FLAGS = re.IGNORECASE

CATEGORY_PATTERNS: dict[str, list[str]] = {
    "brand_deal": [
        r"\bsponsor", r"\bpaid (collab|partnership|promotion|campaign|integration|post)",
        r"\bbrand (deal|collab|partnership|integration)", r"\bad read", r"\bambassador",
        r"\baffiliate", r"\bugc\b", r"\bcampaign\b", r"\binfluencer", r"\bbarter\b",
        r"\brate card", r"\bmedia kit", r"\bdeliverables?\b", r"\bcommercials\b",
        r"\bintegration\b", r"\bdedicated (video|reel)",
    ],
    "investment": [
        r"\b(pre-?seed|seed round|series [a-d])\b", r"\braising\b", r"\bfund ?rais",
        r"\binvest(or|ors|ment|ing)?\b", r"\bpitch deck", r"\bangel\b", r"\bcap table",
        r"\bvaluation", r"\b(cheque|check) size", r"\b(ARR|MRR)\b", r"\bterm sheet",
        r"\bSAFE\b", r"\bround\b", r"\bsyndicate",
    ],
    "partnership": [
        r"\bpartner(ship|ing)?\b", r"\bcollaborat", r"\bco-?(create|host|market|brand|build)",
        r"\bjoint venture", r"\bcross-?promot", r"\btogether\b", r"\brevenue share",
    ],
    "customer": [
        r"\b(enrol|enroll|join|buy|purchase|sign up)\b.{0,40}\b(course|cohort|program|workshop|masterclass)",
        r"\bcorporate (training|workshop|session)", r"\bfor (my|our) (team|company|employees)",
        r"\bbulk (seats|licen|enrol)", r"\bpricing\b", r"\bconsult(ing|ation)\b",
        r"\bhow much (does|is|for)", r"\binvoice for\b",
    ],
    "speaking": [
        r"\bpodcast", r"\bkeynote", r"\bspeak(er|ing)\b", r"\bpanel(list)?\b", r"\binterview\b",
        r"\bsummit\b", r"\bconference\b", r"\bfireside", r"\bjournalist", r"\breporter",
        r"\bas a guest\b", r"\bjury\b", r"\bjudge\b", r"\bfeature you\b",
    ],
    "vendor": [
        r"\bwe (help|offer|provide|specialis|specializ|build|work with)",
        r"\bour (agency|services|studio|team (can|of))", r"\b(video|thumbnail|reel) edit",
        r"\blead gen", r"\bSEO\b", r"\bfree (audit|trial|sample|demo)", r"\bwhite-?label",
        r"\boutsourc", r"\bbook a (call|demo)", r"\bgrow your\b", r"\bportfolio\b",
        r"\b10x\b", r"\bquick call\b",
    ],
    "talent": [
        r"\b(resume|cv)\b", r"\binternship", r"\bjob (opening|opportunit|role)", r"\bhiring\b",
        r"\blooking for (a |an )?(job|role|opportunit|internship)", r"\breferral", r"\brecruit",
        r"\bopen to work", r"\bfresher\b",
    ],
    "community": [
        r"\bbig fan", r"\blove your (content|videos|work|posts)", r"\bthank(s| you)\b",
        r"\bguidance", r"\bmentor", r"\badvice", r"\binspir", r"\bhow (do|can|should) i\b",
        r"\byour (video|post|reel) (helped|changed)", r"\bstudent\b",
    ],
    "noise": [
        r"\b(crypto|bitcoin|forex|usdt|airdrop)\b", r"\bverify your account",
        r"\bclick (here|the link)", r"\bcongratulations.{0,30}\bwon\b", r"\bunsubscribe",
        r"\bview (this email )?in (your )?browser", r"\bOTP\b", r"\bgift card",
        r"\bdouble your\b", r"\bguaranteed returns?\b", r"\baccount (suspended|will be)",
    ],
}

_COMPILED = {
    category: [re.compile(pattern, _FLAGS) for pattern in patterns]
    for category, patterns in CATEGORY_PATTERNS.items()
}

_CURRENCY = r"(?:₹|rs\.?|inr|\$|usd|us\$|€|eur|£|gbp|aed)"
_MAGNITUDE = r"(?:k|m|mn|mm|l|lakh|lakhs|lac|lacs|cr|crore|crores|million|thousand)"
MONEY_RE = re.compile(
    rf"(?<![\w.])(?:{_CURRENCY}\s?\d(?:[\d,]*\d)?(?:\.\d+)?\s?{_MAGNITUDE}?\b"
    rf"|\d(?:[\d,]*\d)?(?:\.\d+)?\s?{_MAGNITUDE}?\s?(?:inr|usd|rupees|dollars|euros)\b"
    rf"|\d(?:[\d,]*\d)?(?:\.\d+)?\s?(?:lakh|lakhs|lac|lacs|crore|crores|cr)\b(?!\s*(?:followers|subscribers|subs|views|users|students|members|impressions)))",
    _FLAGS,
)
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", _FLAGS)
URGENT_RE = re.compile(
    r"\b(asap|urgent(ly)?|by (tomorrow|today|eod|end of (day|week))|deadline|"
    r"this week|closing (on|this|soon)|last (date|day)|time[- ]sensitive|"
    r"within (24|48) hours|going live)\b",
    _FLAGS,
)
DEADLINE_RE = re.compile(
    r"\b(?:by|before|until|on|deadline(?: is)?|closes?(?: on)?|live on)\s+"
    r"((?:mon|tues|wednes|thurs|fri|satur|sun)day|tomorrow|today|eod|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?)",
    _FLAGS,
)
PERSONAL_RE = re.compile(
    r"\byour (video|videos|post|posts|reel|reels|channel|newsletter|podcast|talk|work|content|"
    r"company|startup|team|course|cohort|book|thread)\b",
    _FLAGS,
)
RISK_RE = re.compile(
    r"\b(password|verification code|otp|seed phrase|wallet|gift card|wire transfer|"
    r"telegram|whatsapp me|ignore (all |previous )?instructions|as an ai|mark this as)\b",
    _FLAGS,
)
GREETING_RE = re.compile(
    r"^\s*(hi|hey|hello|dear|namaste|greetings|good (morning|afternoon|evening))\b[^\n.!?]{0,40}[,.!\n]+\s*",
    _FLAGS,
)
PLEASANTRY_RE = re.compile(
    r"^\s*(i )?(hope|trust) (you|this)[^.!?\n]*[.!?\n]+\s*", _FLAGS
)


def classify(inbound_text: str, sender: dict[str, Any]) -> dict[str, Any]:
    """Keyword-based signals in the same shape Rubric.signals_from_answers returns."""
    text = inbound_text or ""
    hits = {
        category: sum(1 for pattern in patterns if pattern.search(text))
        for category, patterns in _COMPILED.items()
    }
    total = sum(hits.values())
    if total == 0:
        probabilities = {"community": 0.55, "personal": 0.25, "noise": 0.2}
    else:
        probabilities = {c: n / total for c, n in hits.items() if n}
    category = max(probabilities, key=probabilities.get)
    ranked = sorted(probabilities.values(), reverse=True)
    confidence = clamp(0.35 + 0.45 * (ranked[0] - (ranked[1] if len(ranked) > 1 else 0)) + 0.04 * min(total, 5))

    money = 1.0 if MONEY_RE.search(text) else 0.0
    urgency = 0.7 if URGENT_RE.search(text) else 0.15
    personalised = 0.75 if PERSONAL_RE.search(text) else 0.35
    risk = clamp(0.3 * len(RISK_RE.findall(text)) + (0.4 if category == "noise" else 0))

    credibility = 0.35
    domain = (sender.get("email_domain") or "").lower()
    if domain and domain not in FREE_MAIL:
        credibility = 0.66
    if sender.get("company") or sender.get("title"):
        credibility = max(credibility, 0.66)
    if sender.get("verified"):
        credibility = 0.95
    followers = sender.get("followers") or 0
    if isinstance(followers, (int, float)) and followers >= 50_000:
        credibility = max(credibility, 0.85)
    if category == "noise":
        credibility = min(credibility, 0.2)

    dealish = sum(probabilities.get(c, 0) for c in ("brand_deal", "investment", "partnership", "customer", "speaking"))
    opportunity = clamp(0.15 + 0.75 * dealish)
    value = clamp(0.15 + 0.4 * dealish + 0.2 * money + 0.2 * (credibility - 0.35) + 0.1 * personalised)
    length = len(text)
    if length < 40:
        value = min(value, 0.25)

    return {
        "category": category,
        "category_probs": {k: round(v, 3) for k, v in probabilities.items()},
        "category_confidence": round(confidence, 3),
        "opportunity": round(opportunity, 3),
        "value": round(value, 3),
        "credibility": round(credibility, 3),
        "urgency": urgency,
        "personalised": personalised,
        "reply_expected": 0.75 if "?" in text[-400:] else 0.45,
        "money": money,
        "risk": round(risk, 3),
        "declined": [],
    }


def extract_facts(text: str) -> dict[str, Any]:
    """Things worth showing next to a conversation without calling a model."""
    text = text or ""
    amounts = _unique(m.group(0).strip() for m in MONEY_RE.finditer(text))
    links = _unique(u.rstrip(".,;") for u in URL_RE.findall(text))
    deadlines = _unique(m.group(0).strip() for m in DEADLINE_RE.finditer(text))
    return {"amounts": amounts[:5], "links": links[:6], "deadlines": deadlines[:3]}


def summarize(first_inbound: str, latest_inbound: str = "") -> str:
    """A one-line gist without a model: the opening message minus the throat-clearing."""
    body = first_inbound or latest_inbound or ""
    for _ in range(2):
        body = GREETING_RE.sub("", body, count=1)
        body = PLEASANTRY_RE.sub("", body, count=1)
    body = body.strip()
    return one_line(body[:1].upper() + body[1:], 220)


def _unique(items: Any) -> list[str]:
    """De-duplicate case-insensitively, keeping first-seen order and original casing."""
    seen: dict[str, str] = {}
    for item in items:
        key = item.lower()
        if key and key not in seen:
            seen[key] = item
    return list(seen.values())
