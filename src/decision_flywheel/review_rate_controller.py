"""The review-rate program of one embedded cyclotron: who is sent to review, and at what rate.

The controller owns the program's persisted state, applies expiries, version
raises and finished windows as decisions arrive, chooses which outputs of a
decision go to review, and reports the rate to status. ``Cyclotron`` delegates
to it and keeps the public methods.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from .cyclotron_status import FULL_REVIEW, ReviewRate, ReviewRateOverride, status_from_flywheel
from .embedded_records import ClassifierSpec, CyclotronDefinition, Review, ReviewSelection, _json
from .review_program import (
    Override,
    ProgramState,
    ReviewProgram,
    check_window,
    combine_evidence,
    current_rate,
    expected_item_reviews,
    next_step,
    raise_for_version,
    raise_for_versions,
    select,
    state_name,
    window_evidence,
)

FULL_REVIEW_DETAIL = "Every decision is reviewed; no review-rate program is configured."
PROGRAM_STATE_KEY = "review_program_state"


class ReviewRateController:
    def __init__(
        self,
        *,
        program: ReviewProgram | None,
        definition: CyclotronDefinition,
        specs: Mapping[str, ClassifierSpec],
        versions,
        wheels: Mapping[str, Any],
        root_storage,
        append_event: Callable[[Mapping[str, Any]], dict],
        active_review: Callable[[str, str], Review | None],
        observer: Callable[[dict], None],
        clock: Callable[[], datetime],
    ):
        self.program = program
        self.definition = definition
        self.specs = specs
        self.versions = versions
        self.wheels = wheels
        self.root_storage = root_storage
        self.append_event = append_event
        self.active_review = active_review
        self.observer = observer
        self.clock = clock

    def select(self, item_id: str, confidences: Mapping[str, float | None]) -> ReviewSelection:
        """Advance the review-rate program to this decision, then choose which outputs to review.

        With one output the whole decision is selected, as before. With
        several, each output is selected on its own confidence and its own
        random draw, so a confident output is not sent just because a sibling
        is unsure, and audit sampling and propensities stay per output. Draws
        are keyed by the item, the decision's position and the output, so a
        store replays the same selections; they never read a label.
        """
        if self.program is None:
            whole = ReviewSelection(True, "program", 1.0, FULL_REVIEW_DETAIL)
            return whole if len(confidences) == 1 else replace(whole, outputs={cid: whole for cid in confidences})
        rate, decided = self.advance()
        if len(confidences) == 1:
            confidence = next(iter(confidences.values()))
            chosen = select(self.program, rate, key=f"{item_id}:{decided + 1}", confidence=confidence)
            return ReviewSelection(chosen.selected, chosen.reason, chosen.propensity, chosen.detail)
        outputs = {}
        for cid, confidence in confidences.items():
            chosen = select(self.program, rate, key=f"{item_id}:{decided + 1}:{cid}", confidence=confidence)
            outputs[cid] = ReviewSelection(chosen.selected, chosen.reason, chosen.propensity, chosen.detail)
        requested = [cid for cid, own in outputs.items() if own.selected]
        reasons = {outputs[cid].reason for cid in requested}
        detail = (
            "Requested: " + "; ".join(f"{cid} ({outputs[cid].detail})" for cid in requested)
            if requested
            else "No output requested: " + "; ".join(f"{cid} ({own.detail})" for cid, own in outputs.items())
        )
        return ReviewSelection(
            bool(requested),
            ("program" if "program" in reasons else "audit") if requested else None,
            max(own.propensity for own in outputs.values()),
            detail,
            outputs,
        )

    def advance(self) -> tuple[float, int]:
        """Apply expired overrides, version raises and finished windows; return the rate and decision count."""
        now = self.clock()
        state = self.state()
        events = []
        if state.override and not Override(**state.override).active(now):
            expired = Override(**state.override)
            state.override = None
            events.append(
                self._rate_event(state, now, f"The manual rate of {round(expired.rate * 100)}% set by {expired.set_by} expired.")
            )
        if len(self.specs) == 1:
            state, raised = raise_for_version(self.program, state, self._primary_version())
            if raised:
                events.append(self._rate_event(state, now, state.reason))
        else:
            # Every output is watched: a new version of any one raises the rate.
            state, output = raise_for_versions(self.program, state, {cid: self.versions[cid].version for cid in self.specs})
            if output is not None:
                events.append(self._rate_event(state, now, state.reason, output=output))
        decided = self.root_storage.decision_count()
        while state.checked_windows < decided // self.program.window:
            window = state.checked_windows + 1
            evidence = self._window_evidence(window)
            state, change = check_window(self.program, state, evidence)
            if change:
                events.append(
                    self._rate_event(
                        state,
                        now,
                        state.reason,
                        evidence=evidence,
                        window=window,
                        **({"output": evidence["output"]} if "output" in evidence else {}),
                    )
                )
        with self.root_storage.transaction():
            self.root_storage.put_meta(PROGRAM_STATE_KEY, _json(state.to_json()))
            appended = [self.append_event(event) for event in events]
        for event in appended:
            self.observer(event)
        return current_rate(self.program, state, now), decided

    def set_rate(self, rate: float, *, set_by: str, expires_at: datetime | None = None) -> None:
        """An operator override of the review rate, with an optional expiry.

        Low-confidence decisions and the audit share are still reviewed.
        """
        if self.program is None:
            raise ValueError("this cyclotron has no review-rate program")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
            raise ValueError("rate must be between zero and one")
        if not isinstance(set_by, str) or not set_by.strip():
            raise ValueError("an override needs the person who set it")
        if expires_at is not None and (expires_at.tzinfo is None or expires_at <= self.clock()):
            raise ValueError("expiry must be a future, timezone-aware time")
        state = self.state()
        now = self.clock()
        state.override = {
            "rate": float(rate),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "set_by": set_by,
            "set_at": now.isoformat(),
        }
        with self.root_storage.transaction():
            self.root_storage.put_meta(PROGRAM_STATE_KEY, _json(state.to_json()))
            event = self.append_event(self._rate_event(state, now, f"Manual rate of {round(rate * 100)}% set by {set_by}."))
        self.observer(event)

    def clear_rate(self, *, set_by: str) -> None:
        """End an operator override; the program's own rate applies again."""
        state = self.state()
        if not state.override:
            return
        state.override = None
        now = self.clock()
        with self.root_storage.transaction():
            self.root_storage.put_meta(PROGRAM_STATE_KEY, _json(state.to_json()))
            event = self.append_event(self._rate_event(state, now, f"Manual rate cleared by {set_by}."))
        self.observer(event)

    def state(self) -> ProgramState:
        row = self.root_storage.meta(PROGRAM_STATE_KEY)
        return ProgramState.from_json(json.loads(row[0]) if row else None)

    def status(self) -> ReviewRate:
        if self.program is None:
            return FULL_REVIEW
        now = self.clock()
        state = self.state()
        override = Override(**state.override) if state.override else None
        active = override is not None and override.active(now)
        decided = self.root_storage.decision_count()
        until = self.program.window - decided % self.program.window
        week_ago = (now - timedelta(days=7)).isoformat()
        decisions = []
        for (payload,) in self.root_storage.decision_payloads():
            decision = json.loads(payload)
            if decision["createdAt"] >= week_ago:
                values = [r["confidence"] for r in decision["classifiers"].values()]
                # Routed decisions draw per output; older ones were selected as a whole on the lowest confidence.
                routed = "outputs" in decision["review"]
                decisions.append(tuple(values) if routed else (None if None in values else min(values),))
        rate = current_rate(self.program, state, now)
        return ReviewRate(
            state_name(self.program, state, now),
            rate,
            (f"Manual rate of {round(override.rate * 100)}% set by {override.set_by}." if active else state.reason),
            audit_floor=self.program.audit_share,
            confidence_threshold=self.program.confidence_threshold,
            override=ReviewRateOverride(override.rate, override.expires_at, override.set_by, override.set_at) if active else None,
            next_check_after_decisions=until,
            next_step=next_step(self.program, state, decisions_until_check=until, now=now),
            expected_reviews_per_week=expected_item_reviews(self.program, rate, decisions),
        )

    def _rate_event(self, state: ProgramState, now: datetime, reason: str, **details) -> dict:
        return {
            "kind": "review-rate-changed",
            "state": state_name(self.program, state, now),
            "rate": current_rate(self.program, state, now),
            "reason": reason,
            **details,
        }

    def _primary(self) -> ClassifierSpec:
        return self.definition.classifiers[0]

    def _primary_version(self) -> int:
        return self.versions[self._primary().id].version

    def _window_evidence(self, window: int) -> dict:
        """One finished window's evidence: the only output's, or the worst of every output's."""
        evidence = {
            cid: window_evidence(self.program, self._window_reviews(window, cid), self._gap_points(cid)) for cid in self.specs
        }
        return evidence[self._primary().id] if len(evidence) == 1 else combine_evidence(evidence)

    def _gap_points(self, classifier: str | None = None) -> int | None:
        spec = self.specs[classifier] if classifier else self._primary()
        return status_from_flywheel(
            self.wheels[spec.id],
            cyclotron_id=self.definition.id,
            positive_label=spec.positive_label,
            window=self.program.calibration_window,
        ).calibration.gap_points

    def _window_reviews(self, window: int, classifier: str | None = None) -> list[dict]:
        """Program and audit reviews of one output's decisions in one finished window."""
        cid = classifier or self._primary().id
        size = self.program.window
        rows = []
        for decision_id, payload in self.root_storage.window_decisions(size, (window - 1) * size):
            review = self.active_review(decision_id, cid)
            if review is None or review.selected_by not in ("program", "audit"):
                continue
            result = json.loads(payload)["classifiers"][cid]
            rows.append({"confidence": result["confidence"], "decision": result["label"], "label": review.label})
        return rows
