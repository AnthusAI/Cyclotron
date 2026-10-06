"""Persistent single-control experiments over one shared incumbent per cycle."""
import json

from .flywheel import _hash, _json
from .optimizer_agent import FeedbackBriefing


CONTROLS = ("rubric", "example_ids", "tasks")


class ControlScheduler:
    def __init__(self, wheel):
        self.wheel = wheel
        wheel.db.executescript("""
            CREATE TABLE IF NOT EXISTS control_ideas (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS control_cycles (id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT);
        """)
        # Older bundled proposals remain ideas. Their old aggregate outcome is
        # not falsely presented as a test of any individual control.
        if not self.ideas():
            for old in wheel.hypotheses():
                for control in CONTROLS:
                    value = old["proposal"][control]
                    if not value:
                        continue
                    partial = {control: value}
                    idea_id = _hash({"control": control, "proposal": partial})
                    self.save({"id": idea_id, "control": control, "proposal": partial,
                               "rationale": old["rationale"], "attempts": [], "last_cycle": 0,
                               "created_cycle": 0, "source": "historical bundled hypothesis",
                               "bundled_attempts": old["attempts"]})

    def ideas(self):
        return tuple(json.loads(row[0]) for row in self.wheel.db.execute("SELECT payload FROM control_ideas ORDER BY id"))

    def save(self, idea):
        with self.wheel.db:
            self.wheel.db.execute("INSERT OR REPLACE INTO control_ideas VALUES (?,?)", (idea["id"], _json(idea)))

    async def run(self, training, development, *, protected, propensities, retry_interrupted=False):
        wheel = self.wheel
        wheel._validate_partitions(training, development, protected, propensities)
        wheel.reconcile_feedback(training, development=development)
        evidence = {"training": wheel._evidence(training), "development": wheel._evidence(development),
                    "propensities": propensities, "protected": sorted(row.id for row in protected),
                    "evaluation_weighting": wheel.evaluation_weighting,
                    "training_class_weighting": wheel.training_class_weighting,
                    "coverage": wheel.min_evaluation_per_class}
        key = _hash(evidence)
        saved = wheel.db.execute("SELECT status,payload FROM control_cycles WHERE id=?", (key,)).fetchone()
        if saved and saved[0] == "complete":
            return json.loads(saved[1])
        if saved and not retry_interrupted:
            result = {"promoted": False, "reason": "interrupted control cycle requires explicit retry authorization"}
            wheel._emit({"kind": "round-interrupted", **result})
            return result
        counts = {label: sum(row.label == label for row in training) for label in wheel.initial.task.labels}
        dev_counts = {label: sum(row.label == label for row in development) for label in wheel.initial.task.labels}
        if min(counts.values()) < 3 or min(dev_counts.values()) < wheel.min_evaluation_per_class:
            result = {"promoted": False, "reason": "waiting for training and development class coverage",
                      "development_counts": dev_counts, "minimum_development_per_class": wheel.min_evaluation_per_class}
            wheel._emit({"kind": "waiting-for-labels", "counts": counts, **result})
            return result
        baseline = wheel.active.fingerprint
        progress = json.loads(saved[1]) if saved and saved[1] else {}
        if progress.get("baseline_version", baseline) != baseline:
            raise ValueError("interrupted cycle incumbent changed; do not reuse its comparisons")
        results = progress.get("trials", [])
        completed = set(progress.get("completed_controls", []))
        def checkpoint():
            with wheel.db:
                wheel.db.execute("INSERT OR REPLACE INTO control_cycles VALUES (?, 'pending', ?)",
                    (key, _json({"baseline_version": baseline, "trials": results,
                                  "completed_controls": sorted(completed)})))
        checkpoint()
        cycle = wheel.db.execute("SELECT count(*) FROM control_cycles").fetchone()[0]
        try:
            for control in CONTROLS:
                if control in completed:
                    continue
                briefing = FeedbackBriefing.build(wheel.initial.task, training,
                    current={**wheel.active.config.briefing_state(), "control_under_test": control,
                             "request_budget_bytes": wheel.max_request_bytes,
                             "prior_control_ideas": [idea for idea in self.ideas() if idea["control"] == control]},
                    protected=tuple(row.item for row in development)+tuple(protected))
                proposal = wheel.optimizer.propose(briefing)
                if set(proposal) - {"rationale", control} or control not in proposal:
                    raise ValueError("discovery must propose exactly the requested control")
                # Validate before persisting. Empty/no-change proposals remain
                # visible explanations, not fabricated feature discoveries.
                candidate = wheel.active.config.apply(proposal, training)
                original = wheel.active.config.briefing_state()[control]
                changed = candidate.briefing_state()[control] != original
                if not changed:
                    wheel._emit({"kind": "discovery-no-change", "control": control,
                                 "rationale": proposal.get("rationale", "")})
                else:
                    partial = {control: proposal[control]}
                    idea_id = _hash({"control": control, "proposal": partial})
                    if not any(idea["id"] == idea_id for idea in self.ideas()):
                        self.save({"id": idea_id, "control": control, "proposal": partial,
                                   "rationale": proposal.get("rationale", ""), "attempts": [], "last_cycle": 0,
                                   "created_cycle": cycle, "source": "single-control discovery"})
                        wheel._emit({"kind": "hypothesis-discovered", "idea_id": idea_id, "control": control,
                                     "proposal": partial, "rationale": proposal.get("rationale", "")})
                candidates = [idea for idea in self.ideas() if idea["control"] == control
                              and not any(attempt["feedback_fingerprint"] == key for attempt in idea["attempts"])]
                valid = []
                for idea in candidates:
                    try:
                        wheel.active.config.apply(idea["proposal"], training)
                        valid.append(idea)
                    except ValueError:
                        wheel._emit({"kind": "control-trial-deferred", "idea_id": idea["id"],
                                     "control": control, "reason": "idea references currently ineligible examples"})
                candidates = valid
                if not candidates:
                    completed.add(control)
                    checkpoint()
                    continue
                # Every third cycle, overdue ideas outrank fresh discoveries.
                # No score threshold permanently eliminates an idea.
                candidates.sort(key=lambda idea: (0 if cycle-max(idea["last_cycle"], idea.get("created_cycle", 0)) >= 3
                                                   else 1 if not idea["attempts"] else 2,
                                                   0 if idea["proposal"] == {control: proposal[control]} else 1,
                                                   idea["last_cycle"], idea["id"]))
                idea = candidates[0]
                if wheel.max_requests-wheel.requests < len(training)+2*len(development):
                    wheel._emit({"kind": "control-trial-deferred", "control": control, "reason": "session request budget"})
                    continue
                wheel._emit({"kind": "control-trial-started", "control": control, "idea_id": idea["id"],
                             "baseline_version": baseline, "training_count": len(training),
                             "previous_attempts": len(idea["attempts"]),
                             "selection_reason": "overdue retained idea" if idea["attempts"] else "untested idea"})
                result = await wheel.improve(training, development, protected=protected, propensities=propensities,
                                             candidate_proposal=idea["proposal"], apply_promotion=False,
                                             retry_interrupted=retry_interrupted)
                idea["attempts"].append({"feedback_fingerprint": key, "training_count": len(training),
                                         "by_label": counts, "result": result})
                idea["last_cycle"] = cycle
                self.save(idea)
                results.append({"control": control, "idea_id": idea["id"], **result})
                completed.add(control)
                checkpoint()
                wheel._emit({"kind": "control-trial-completed", "control": control, "idea_id": idea["id"], **result})
                if wheel.active.fingerprint != baseline:
                    raise RuntimeError("isolated trials must retain one shared incumbent")
            eligible = [result for result in results if result.get("improved")]
            metric = "balanced_brier" if wheel.evaluation_weighting == "equal_class" else "brier"
            best = min(eligible, key=lambda result: (result["candidate"][metric], result["idea_id"])) if eligible else None
            if best:
                wheel.promote_trial(best["trial_fingerprint"], training, development)
            result = {"promoted": best is not None, "trials": results, "baseline_version": baseline,
                      "selected_control": best["control"] if best else None,
                      "reason": "best isolated candidate improved" if best else "no isolated candidate qualified"}
            with wheel.db:
                wheel.db.execute("UPDATE control_cycles SET status='complete',payload=? WHERE id=?", (_json(result), key))
            wheel._emit({"kind": "control-cycle-completed", **result})
            return result
        except Exception as error:
            wheel._emit({"kind": "round-failed", "error_type": type(error).__name__,
                         "reason": "control cycle failed; retained ideas and completed feature cache preserved"})
            return {"promoted": False, "reason": "control cycle failed", "error_type": type(error).__name__, "trials": results}
