"""Editor-facing wording for a cyclotron status, with no engine concepts.

The engine's own ``reason``, ``nextStep`` and ``summary`` strings name
calibration gaps, versions and refits. They stay as they are for operators
and audit; these functions derive plain companions from the same structured
facts, so an embedding application can show them to editors unchanged.
"""

from __future__ import annotations


def _percent(rate: float) -> str:
    return f"{round(rate * 100)}%"


def plain_reason(state: str, rate: float, override_set_by: str | None = None) -> str:
    if state == "manual":
        who = f" by {override_set_by}" if override_set_by else ""
        return f"The review rate was set to {_percent(rate)}{who}."
    if state == "raised":
        return "Some suggestions were off recently, so every suggestion is being reviewed again."
    if state in ("tapering", "steady") and rate < 1:
        return f"Suggestions have been reliable, so about {_percent(rate)} of confident ones need a review."
    return "Every suggestion is being reviewed while suggestions are still being checked against your reviews."


def plain_next_step(state: str, rate: float) -> str:
    if state == "manual":
        return "Normal review resumes when the manual setting ends."
    if state == "steady" and rate < 1:
        return "Review goes back to every suggestion if suggestions stop being reliable."
    if state == "tapering":
        return "Fewer suggestions will need review if they stay reliable."
    return "Fewer suggestions will need review once they prove reliable."


def plain_change_summary(kind: str, labels: int | None = None) -> str:
    if kind == "refit":
        return f"Learned from {labels} reviews." if labels else "Learned from new reviews."
    if kind == "dropped":
        return "Tried an update and kept the current approach."
    return "Suggestions were updated."
