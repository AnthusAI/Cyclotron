"""Shadow screening from a blank starting rubric: the incumbent is provisional and development is tiny.
Fake decision model and fake optimizer only, no network."""
import json

import pytest

from .classifier_config import ClassifierConfig
from .cycle_replay import run_cycle_replay
from .flywheel import DecisionFlywheel
from .idea_screening import IdeaScreeningConfig
from .replay import plan_replay
from .shadow_evaluation_test import RIGHT, TASK, Fake, RubricModel, base_right, replay_rows, run

pytestmark = pytest.mark.slow


class BlankStartOptimizer(Fake):
    """propose() is the legacy single proposal that initializes the first rubric; propose_many feeds screening."""
    def propose(self, briefing):
        return {"rationale": "first rubric", "rubric": "base"}


def events(wheel, kind):
    return [e for e in wheel.history(1000000) if e["kind"] == kind]


def blank_wheel(tmp_path, config, name="w.sqlite", optimizer=None):
    RIGHT.setdefault("", base_right)   # the blank rubric decides like the base rubric
    return DecisionFlywheel(tmp_path / name, ClassifierConfig(TASK, rubric=""), RubricModel(),
                            optimizer or BlankStartOptimizer(), max_requests=100000, idea_screening=config)


def replay(wheel, count=240, floor=2):
    plan = plan_replay(TASK, replay_rows(count), seed="blank", batch_size=40)
    return plan, run(run_cycle_replay(wheel, plan, optimize_every=10 ** 6, retrain_every=10 ** 6, stages=("rubric",),
                                      rubric_changes_every=12, rubric_trigger_basis="revealed_feedback_count",
                                      min_evaluation_per_class=floor))


def stages(wheel):
    return [json.loads(p) for (p,) in wheel.db.execute("SELECT payload FROM optimization_stages WHERE status='complete'")]


def test_shadow_screening_engages_from_a_blank_rubric_with_a_development_set_below_the_per_class_minimum(tmp_path):
    wheel = blank_wheel(tmp_path, IdeaScreeningConfig(enabled=True, shadow_min_items=10, shadow_evict_after=8))
    plan, report = replay(wheel, floor=10 ** 6)   # the per-class minimum can never be met: the old gate blocked this
    assert len(report["cycles"]) == 240 and "stopped_reason" not in report
    assert all(s.get("minimum_development_per_class", 10 ** 6) == 10 ** 6 for s in stages(wheel))
    reasons = [s.get("reason", "") for s in stages(wheel)]
    assert any("initialized provisionally" in r for r in reasons)
    assert events(wheel, "hypothesis-discovered")
    assert "shadow-stage-completed" in [e["kind"] for e in wheel.history(1000000)]
    assert report["shadow"]["calls"] > 0
    # shadow ideas were scored on forward revealed stories, never on a scoreboard (protected) story
    scoreboard = {r.item.id for r in plan.scoreboard}
    scored = {s for (s,) in wheel.db.execute("SELECT item_id FROM idea_evaluations WHERE context_version='shadow'")}
    assert scored and not scored & scoreboard
    wheel.close()


def test_a_truly_better_idea_is_promoted_by_shadow_evidence_and_the_status_is_evaluated(tmp_path):
    wheel = blank_wheel(tmp_path, IdeaScreeningConfig(enabled=True, shadow_min_items=10, shadow_evict_after=8))
    replay(wheel, 400)
    promoted = [e for e in events(wheel, "promoted") if e.get("promotion_path") == "shadow-evaluation"]
    assert promoted and events(wheel, "idea-promoted-by-shadow")
    assert wheel.active.config.rubric == "better"
    # forward shadow evidence justified it, not development coverage: the context leaves 'provisional'
    assert wheel.active.validation_status == "evaluated"
    assert promoted[0]["shadow_evidence"]["stage"] == "shadow"
    wheel.close()


def test_the_legacy_refine_branch_does_not_also_fire_when_shadow_screening_engages(tmp_path):
    wheel = blank_wheel(tmp_path, IdeaScreeningConfig(enabled=True, shadow_min_items=10, shadow_evict_after=8))
    replay(wheel)
    reasons = " | ".join(s.get("reason", "") for s in stages(wheel))
    assert "decaying recency preference" not in reasons
    assert not events(wheel, "context-refined")
    wheel.close()


def test_diagnosis_runs_from_a_blank_start_when_enabled(tmp_path):
    from .diagnosis_test import ElementModel, default_independent, default_reconcile
    from .idea_screening import DiagnosisConfig
    from .optimizer_agent import OptimizerAgent, OptimizerReply
    prompts = []

    def complete(messages):
        current = json.loads(messages[1]["content"])["current"]
        prompts.append(current)
        if current.get("diagnosis_phase") == "independent":
            return OptimizerReply(json.dumps(default_independent(current)), "fake")
        if current.get("diagnosis_phase") == "reconcile":
            return OptimizerReply(json.dumps(default_reconcile(current)), "fake")
        if "operator_requests" not in current:
            return OptimizerReply(json.dumps({"rationale": "plain", "rubric": "base"}), "fake")
        proposals = []
        for operator, spec in sorted(current["operator_requests"].items()):
            for k in range(spec["count"]):
                n = len(prompts) * 10 + k
                proposals.append({"operator": operator, "rationale": f"{operator}{n}",
                                  "rubric": "PATCH_OK " + " ".join(f"w{n}x{j}" for j in range(30))})
        return OptimizerReply(json.dumps({"proposals": proposals}), "fake")
    config = IdeaScreeningConfig(enabled=True, shadow_min_items=10, shadow_evict_after=8,
                                 proposal_mix={"bold": 2, "target": 2, "combine": 2},
                                 diagnosis=DiagnosisConfig(enabled=True, samples=4, min_cell=3, element_sample=40,
                                                           max_jev_calls=120))
    wheel = DecisionFlywheel(tmp_path / "d.sqlite", ClassifierConfig(TASK, rubric=""), ElementModel(),
                             OptimizerAgent(complete), max_requests=100000, idea_screening=config)
    replay(wheel)
    ran = [s for s in events(wheel, "diagnosis-summary") if s["skipped"] is None]
    assert ran and events(wheel, "hypothesis-discovered") and events(wheel, "proposal-operator")
    wheel.close()


def test_dev_screen_mode_with_a_provisional_incumbent_still_does_not_screen(tmp_path):
    wheel = blank_wheel(tmp_path, IdeaScreeningConfig(enabled=True, mode="dev_screen"))
    _, report = replay(wheel)
    assert len(report["cycles"]) == 240
    assert not events(wheel, "hypothesis-discovered") and not events(wheel, "proposal-operator")
    assert not any(e["kind"].startswith(("shadow", "screen")) for e in wheel.history(1000000))
    wheel.close()


def test_with_screening_off_a_blank_start_uses_only_the_legacy_paths(tmp_path):
    wheel = blank_wheel(tmp_path, IdeaScreeningConfig())
    _, report = replay(wheel)
    assert len(report["cycles"]) == 240
    assert not events(wheel, "hypothesis-discovered") and not events(wheel, "proposal-operator")
    assert not events(wheel, "diagnosis-summary")
    assert any("initialized provisionally" in s.get("reason", "") for s in stages(wheel))
    wheel.close()
