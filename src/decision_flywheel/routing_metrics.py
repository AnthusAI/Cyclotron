"""Routing metrics: what a review rule built on confidence would have caught and approved.

Each row is one recorded decision: the label it gave, the confidence it stated
for that label, and the reviewer's label. Two questions are answered per
window of rows:

* Least-confident share. If reviewers looked only at the least-confident N
  percent, what share of the window's mistakes would they have seen? Rows
  tied at the cut are credited in proportion, which is the expected count
  under a random order among the ties; recorded confidence is often tied
  (a decision model that says 0.95 for almost everything).
* Threshold rule. Auto-approve every decision at least ``t`` sure: how many
  are approved, what they promised on average, how many were right, and how
  many the whole system gets right when every decision sent to a reviewer
  counts as right (the reviewer's label is the answer).
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Sequence

LEAST_CONFIDENT_PERCENTS = (10, 15, 20, 25, 30)
THRESHOLDS = ("0.75", "0.80", "0.85", "0.90", "0.95")


@dataclass(frozen=True)
class Decision:
    label: str
    confidence: float
    actual: str

    @property
    def right(self) -> bool:
        return self.label == self.actual


def _checked(rows: Iterable[Decision]) -> list[Decision]:
    rows = list(rows)
    for row in rows:
        if not isinstance(row, Decision) or not 0 <= row.confidence <= 1:
            raise ValueError("routing rows need a label, a confidence between 0 and 1, and the reviewer's label")
    return rows


def least_confident_capture(rows: Sequence[Decision], percents: Sequence[int] = LEAST_CONFIDENT_PERCENTS) -> list[dict]:
    """Share of the window's mistakes among its least-confident ``percent`` of rows."""
    rows = _checked(rows)
    mistakes = sum(not row.right for row in rows)
    result = []
    for percent in percents:
        if not 0 < percent < 100:
            raise ValueError("percent must be between 0 and 100")
        size = len(rows) * percent // 100
        caught, tied = Fraction(0), []
        if size:
            cut = sorted(row.confidence for row in rows)[size - 1]
            below = [row for row in rows if row.confidence < cut]
            tied = [row for row in rows if row.confidence == cut]
            caught = sum(not row.right for row in below) + Fraction(size - len(below), len(tied)) * sum(not row.right for row in tied)
        result.append({"percent": percent, "reviewed": size, "mistakes": mistakes,
                       "mistakes_caught": float(caught),
                       "share_of_mistakes": float(caught / mistakes) if mistakes else None,
                       "tied_at_cut": len(tied)})
    return result


def threshold_rule(rows: Sequence[Decision], thresholds: Sequence[str] = THRESHOLDS) -> list[dict]:
    """Auto-approve at or above each threshold; everything else goes to a reviewer."""
    rows = _checked(rows)
    result = []
    for text in thresholds:
        threshold = float(text)
        approved = [row for row in rows if row.confidence >= threshold - 1e-12]
        right = sum(row.right for row in approved)
        result.append({
            "threshold": threshold, "items": len(rows), "approved": len(approved), "approved_right": right,
            "promised": sum(row.confidence for row in approved) / len(approved) if approved else None,
            "got": right / len(approved) if approved else None,
            "to_reviewers": len(rows) - len(approved),
            "whole_system_right": right + len(rows) - len(approved),
            "whole_system_accuracy": (right + len(rows) - len(approved)) / len(rows) if rows else None,
        })
    return result


def routing(rows: Sequence[Decision]) -> dict:
    return {"least_confident": least_confident_capture(rows), "threshold_rule": threshold_rule(rows)}
