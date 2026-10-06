"""Persistent single-control experiments over one shared incumbent per cycle."""
import json
from datetime import datetime, timezone

from .flywheel import _hash, _json
from .optimizer_agent import FeedbackBriefing
from .feature_bank import FeatureBank


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
        self.features = FeatureBank(wheel.db)
        # Import old grouped questions without mislabeling the group's score as
        # an individual feature test. Old records remain available for audit.
        for idea in self.ideas():
            if idea["control"] == "tasks" and not idea.get("feature_id"):
                for question in idea["proposal"]["tasks"]:
                    self._admit(question, idea["rationale"], {}, idea.get("created_cycle", 0))

    def _admit(self, question, rationale, evidence, cycle):
        feature_id = self.features.register(question, rationale=rationale, evidence=evidence)
        idea_id = _hash({"control": "tasks", "feature_id": feature_id})
        if not any(idea["id"] == idea_id for idea in self.ideas()):
            self.save({"id": idea_id, "control": "tasks", "feature_id": feature_id,
                       "proposal": {"tasks": [question]}, "rationale": rationale,
                       "attempts": [], "last_cycle": 0, "created_cycle": cycle,
                       "source": "individual feature discovery"})
            self.wheel._emit({"kind": "feature-discovered", "feature_id": feature_id,
                              "question": question, "rationale": rationale})
        return idea_id

    def _proposal(self, idea):
        if not idea.get("feature_id"):
            return idea["proposal"]
        question = idea["proposal"]["tasks"][0]
        # One added question, or one wording revision, preserves every other
        # supporting question already deployed in the common incumbent.
        tasks = [{"name": t.name, "instructions": t.instructions, "labels": list(t.labels)}
                 for t in self.wheel.active.config.tasks if t.name != question["name"]]
        return {"tasks": [*tasks, question]}

    def ideas(self):
        return tuple(json.loads(row[0]) for row in self.wheel.db.execute("SELECT payload FROM control_ideas ORDER BY id"))

    def save(self, idea):
        with self.wheel.db:
            self.wheel.db.execute("INSERT OR REPLACE INTO control_ideas VALUES (?,?)", (idea["id"], _json(idea)))

    async def run(self, training, development, *, protected, propensities, retry_interrupted=False,
                  max_feature_trials=3):
        if isinstance(max_feature_trials, bool) or not isinstance(max_feature_trials, int) or max_feature_trials < 1:
            raise ValueError("feature trial ceiling must be a positive integer")
        wheel = self.wheel
        wheel._validate_partitions(training, development, protected, propensities)
        wheel.reconcile_feedback(training, development=development)
        evidence = {"training": wheel._evidence(training), "development": wheel._evidence(development),
                    "optimizer_context": wheel.optimizer_context,
                    "propensities": propensities, "protected": sorted(row.id for row in protected),
                    "evaluation_weighting": wheel.evaluation_weighting,
                    "training_class_weighting": wheel.training_class_weighting,
                    "coverage": wheel.min_evaluation_per_class,
                    "scheduler_version": "individual-features-v1", "max_feature_trials": max_feature_trials}
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
        evaluation_time = datetime.fromisoformat(progress["evaluation_time"]) if progress.get("evaluation_time") \
            else datetime.now(timezone.utc)
        if progress.get("baseline_version", baseline) != baseline:
            raise ValueError("interrupted cycle incumbent changed; do not reuse its comparisons")
        results = progress.get("trials", [])
        completed = set(progress.get("completed_controls", []))
        discovered = set(progress.get("discovered_controls", []))
        def checkpoint():
            with wheel.db:
                wheel.db.execute("INSERT OR REPLACE INTO control_cycles VALUES (?, 'pending', ?)",
                    (key, _json({"baseline_version": baseline, "trials": results,
                                  "completed_controls": sorted(completed), "discovered_controls": sorted(discovered),
                                  "evaluation_time": evaluation_time.isoformat()})))
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
                    protected=tuple(row.item for row in development)+tuple(protected),
                    human_explanations=wheel.optimizer_context["human_explanations"])
                proposal = {control: wheel.active.config.briefing_state()[control]}
                if control not in discovered:
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
                    if control == "tasks":
                        for question in proposal[control]:
                            self._admit(question, proposal.get("rationale", ""), wheel._evidence(training), cycle)
                    elif not any(idea["id"] == idea_id for idea in self.ideas()):
                        self.save({"id": idea_id, "control": control, "proposal": partial,
                                   "rationale": proposal.get("rationale", ""), "attempts": [], "last_cycle": 0,
                                   "created_cycle": cycle, "source": "single-control discovery"})
                        wheel._emit({"kind": "hypothesis-discovered", "idea_id": idea_id, "control": control,
                                     "proposal": partial, "rationale": proposal.get("rationale", "")})
                discovered.add(control)
                checkpoint()
                candidates = [idea for idea in self.ideas() if idea["control"] == control
                              and (control != "tasks" or idea.get("feature_id"))
                              and not any(attempt["feedback_fingerprint"] == key for attempt in idea["attempts"])]
                valid = []
                for idea in candidates:
                    try:
                        candidate_config = wheel.active.config.apply(self._proposal(idea), training)
                        if candidate_config.briefing_state()[control] != wheel.active.config.briefing_state()[control]:
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
                remaining_trials = max_feature_trials - sum(r["control"] == "tasks" for r in results)
                selected = candidates[:max(0, remaining_trials)] if control == "tasks" else candidates[:1]
                for idea in selected:
                    if wheel.max_requests-wheel.requests < len(training)+2*len(development):
                        wheel._emit({"kind": "control-trial-deferred", "control": control,
                                     "idea_id": idea["id"], "reason": "session request budget"})
                        if idea.get("feature_id"):
                            self.features.record(idea["feature_id"], {"feedback_fingerprint": key,
                                                 "reason": "session request budget"}, {})
                        continue
                    wheel._emit({"kind": "control-trial-started", "control": control, "idea_id": idea["id"],
                                 "feature_id": idea.get("feature_id"), "proposal": self._proposal(idea),
                                 "baseline_version": baseline, "training_count": len(training),
                                 "previous_attempts": len(idea["attempts"]),
                                 "selection_reason": "retained idea" if idea["attempts"] else "untested idea"})
                    result = await wheel.improve(training, development, protected=protected, propensities=propensities,
                                                 candidate_proposal=self._proposal(idea), apply_promotion=False,
                                                 retry_interrupted=retry_interrupted, evaluation_time=evaluation_time)
                    idea["attempts"].append({"feedback_fingerprint": key, "training_count": len(training),
                                             "by_label": counts, "result": result})
                    idea["last_cycle"] = cycle
                    self.save(idea)
                    if idea.get("feature_id"):
                        name = idea["proposal"]["tasks"][0]["name"]
                        self.features.record(idea["feature_id"], {"feedback_fingerprint": key, **result},
                                             result.get("feature_diagnostics", {}).get(name, {}))
                    results.append({"control": control, "idea_id": idea["id"],
                                    "feature_id": idea.get("feature_id"), **result})
                    checkpoint()
                    wheel._emit({"kind": "control-trial-completed", "control": control, "idea_id": idea["id"], **result})
                    if wheel.active.fingerprint != baseline:
                        raise RuntimeError("isolated trials must retain one shared incumbent")
                completed.add(control)
                checkpoint()
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
