"""The triage policy: loads rubric.json, builds Jev's questions, turns answers into a verdict."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..util import clamp
from . import jev

TIERS = ("P0", "P1", "P2", "P3")
NOUL_SIGNALS = ("opportunity", "personalised", "reply_expected", "money", "risk")
SCORE_SIGNALS = ("value", "credibility", "urgency")

# What a signal counts as when the model declined to answer it.
NEUTRAL = {
    "opportunity": 0.5, "value": 0.4, "credibility": 0.5, "urgency": 0.2,
    "personalised": 0.5, "reply_expected": 0.5, "money": 0.0, "risk": 0.0,
}


@dataclass
class Verdict:
    category: str
    category_confidence: float | None
    score: float
    priority: str
    needs_review: bool = False
    review_reason: str = ""
    signals: dict[str, Any] = field(default_factory=dict)


class Rubric:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.categories: list[dict[str, Any]] = list(raw.get("categories") or [])
        if not self.categories:
            raise ValueError("rubric.json needs at least one category")
        self.by_id = {c["id"]: c for c in self.categories}
        self.question_defs: dict[str, dict[str, Any]] = dict(raw.get("questions") or {})
        scoring = raw.get("scoring") or {}
        self.weights: dict[str, float] = dict(scoring.get("weights") or {})
        self.risk_penalty = float(scoring.get("risk_penalty", 0.75))
        self.tiers: dict[str, float] = dict(scoring.get("tiers") or {"P0": 70, "P1": 52, "P2": 32})
        self.urgency_bump: dict[str, float] = dict(scoring.get("urgency_bump") or {})
        self.review: dict[str, float] = dict(raw.get("review") or {})
        self.tier_labels: dict[str, str] = dict(raw.get("tier_labels") or {})
        self.owner_context: str = str(raw.get("owner_context") or "")
        self.email_prefilter: dict[str, Any] = dict(raw.get("email_prefilter") or {})

    @classmethod
    def load(cls, path: Path) -> "Rubric":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    # -- lookups ----------------------------------------------------------

    def label(self, category_id: str) -> str:
        return self.by_id.get(category_id, {}).get("label") or category_id or "Unsorted"

    def value(self, category_id: str) -> float:
        return float(self.by_id.get(category_id, {}).get("value", 0.0))

    def public(self) -> dict[str, Any]:
        """What the UI needs to render filters and legends."""
        return {
            "categories": [
                {"id": c["id"], "label": c.get("label", c["id"]), "value": c.get("value", 0)}
                for c in self.categories
            ],
            "tiers": [{"id": t, "label": self.tier_labels.get(t, t)} for t in TIERS],
            "scales": {
                name: list(self.question_defs.get(name, {}).get("criteria") or [])
                for name in SCORE_SIGNALS
            },
        }

    # -- Jev request ------------------------------------------------------

    def questions(self) -> dict[str, dict[str, Any]]:
        """The `questions` map for the Decisions API."""
        out: dict[str, dict[str, Any]] = {}
        for name, definition in self.question_defs.items():
            kind = definition.get("type")
            question: dict[str, Any] = {"type": kind}
            if definition.get("instructions"):
                question["instructions"] = definition["instructions"]
            if name == "category":
                question["criteria"] = {c["id"]: c["criteria"] for c in self.categories}
            elif definition.get("criteria") is not None:
                question["criteria"] = definition["criteria"]
            out[name] = question
        if "category" not in out:
            out["category"] = {
                "type": "choice",
                "criteria": {c["id"]: c["criteria"] for c in self.categories},
            }
        return out

    def signals_from_answers(self, answers: dict[str, Any]) -> dict[str, Any]:
        """Normalise raw Decisions answers into 0..1 signals."""
        chosen, probabilities, confidence = jev.choice_distribution(answers.get("category"))
        signals: dict[str, Any] = {
            "category": chosen,
            "category_probs": probabilities,
            "category_confidence": confidence,
            "declined": [],
        }
        for name in NOUL_SIGNALS:
            if name in self.question_defs:
                signals[name] = jev.noul_probability(answers.get(name))
        for name in SCORE_SIGNALS:
            if name in self.question_defs:
                levels = len(self.question_defs[name].get("criteria") or [])
                fraction = jev.score_fraction(answers.get(name), levels)
                signals[name] = None if fraction is None else round(fraction, 4)
        # Any extra yes/no question added to rubric.json becomes a flag chip in the UI.
        flags = {}
        for name, definition in self.question_defs.items():
            if definition.get("type") == "noul" and name not in NOUL_SIGNALS:
                flags[name] = jev.noul_probability(answers.get(name))
        if flags:
            signals["flags"] = flags
        for name, answer in answers.items():
            if isinstance(answer, dict) and answer.get("type") == "refusal":
                signals["declined"].append(name)
        return signals

    # -- scoring ----------------------------------------------------------

    def verdict(self, signals: dict[str, Any]) -> Verdict:
        def get(name: str) -> float:
            value = signals.get(name)
            return NEUTRAL[name] if value is None else float(value)

        probabilities: dict[str, float] = signals.get("category_probs") or {}
        category = signals.get("category") or ""
        known = {k: v for k, v in probabilities.items() if k in self.by_id}
        total = sum(known.values())
        if total > 0:
            category_value = sum(self.value(k) * v for k, v in known.items()) / total
        else:
            category_value = self.value(category)

        w = self.weights
        raw = (
            w.get("value", 0) * get("value")
            + w.get("category", 0) * category_value
            + w.get("opportunity", 0) * get("opportunity")
            + w.get("credibility", 0) * get("credibility")
            + w.get("personalised", 0) * get("personalised")
            + w.get("urgency", 0) * get("urgency")
            + w.get("money", 0) * get("money")
        )
        weight_sum = sum(w.get(k, 0) for k in
                         ("value", "category", "opportunity", "credibility", "personalised", "urgency", "money"))
        if weight_sum > 0:
            raw /= weight_sum
        score = round(100 * clamp(raw) * (1 - self.risk_penalty * get("risk")), 1)

        priority = self.tier_for(score)
        bump = self.urgency_bump
        if bump and get("urgency") >= bump.get("min_urgency", 2) and score >= bump.get("min_score", 101):
            priority = TIERS[max(0, TIERS.index(priority) - 1)]

        needs_review, reason = self._review(signals, category, known, score)
        return Verdict(
            category=category,
            category_confidence=signals.get("category_confidence"),
            score=score,
            priority=priority,
            needs_review=needs_review,
            review_reason=reason,
            signals=signals,
        )

    def tier_for(self, score: float) -> str:
        for tier in ("P0", "P1", "P2"):
            if score >= float(self.tiers.get(tier, 101)):
                return tier
        return "P3"

    def _review(
        self, signals: dict[str, Any], category: str, probabilities: dict[str, float], score: float
    ) -> tuple[bool, str]:
        """Flag only the uncertainty that could change what the owner does."""
        if signals.get("declined"):
            return True, "Model declined: " + ", ".join(signals["declined"])
        if not category:
            return True, "No category returned"
        ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
        runner_up = ranked[1] if len(ranked) > 1 else None
        stakes = max(self.value(category), self.value(runner_up[0]) if runner_up else 0)
        if stakes < 0.5 and score < float(self.tiers.get("P2", 32)):
            return False, ""  # torn between two kinds of low-value mail: nobody cares
        confidence = signals.get("category_confidence")
        if confidence is not None and confidence < float(self.review.get("min_category_confidence", 0)):
            if runner_up:
                return True, f"Unsure: {self.label(category)} or {self.label(runner_up[0])}"
            return True, f"Low confidence in {self.label(category)}"
        if runner_up and ranked[0][1] - runner_up[1] < float(self.review.get("min_category_margin", 0)):
            if abs(self.value(ranked[0][0]) - self.value(runner_up[0])) >= 0.3:
                return True, f"Torn: {self.label(ranked[0][0])} or {self.label(runner_up[0])}"
        edge = float(self.review.get("tier_edge", 0))
        if edge:
            for tier in ("P0", "P1"):
                if abs(score - float(self.tiers.get(tier, 999))) < edge:
                    return True, f"Borderline {tier}"
        return False, ""
