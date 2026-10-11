"""Control trials are isolated; retained ideas are not permanently eliminated."""
import asyncio
import json

from .classifier_config import ClassifierConfig
from .control_scheduler import ControlScheduler
from .flywheel import DecisionFlywheel
from .flywheel_test import FakeModel, TASK, TRAIN, DEV
from .models import Item, LabeledItem
from .optimizer_agent import OptimizerAgent, OptimizerReply
from .selection_policy import SelectionPolicy as _SP
_BRIER = _SP('balanced_brier')


def optimizer(calls):
    def complete(messages):
        control = json.loads(messages[-1]["content"])["current"]["control_under_test"]
        calls.append(control)
        value = {"rubric": "Practical work", "example_ids": ["t0", "t1"],
                 "tasks": [{"name": "practical", "instructions": "Is this practical?", "labels": ["yes", "no"]}]}[control]
        return OptimizerReply(json.dumps({"rationale": "A testable idea", control: value}), "fake")
    return OptimizerAgent(complete)


def test_all_three_controls_are_tested_against_one_incumbent_then_only_the_best_is_promoted(tmp_path):
    calls = []
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), optimizer(calls))
    result = asyncio.run(ControlScheduler(wheel).run(TRAIN, DEV, protected=(), propensities={r.item.id: 1. for r in TRAIN}))
    assert calls == ["rubric", "example_ids", "tasks"]
    assert len(result["trials"]) == 3
    assert result["promoted"]
    assert wheel.active.config.tasks[0].name == "practical"
    assert wheel.active.config.rubric == ""
    assert wheel.active.config.example_ids == ()
    assert len(ControlScheduler(wheel).ideas()) == 3
    wheel.close()


def test_multiple_discovered_questions_are_fitted_separately_and_preserve_the_other_controls(tmp_path):
    from .feature_bank import FeatureBank
    calls = []
    normal = optimizer(calls)
    def discover(messages):
        reply = normal.complete(messages)
        proposal = json.loads(reply.content)
        if "tasks" in proposal:
            proposal["tasks"].append({"name": "novel", "instructions": "Is this novel?", "labels": ["yes", "no"]})
        return OptimizerReply(json.dumps(proposal), "fake")
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK, rubric="Frozen rubric"),
                             FakeModel(), OptimizerAgent(discover), max_requests=500)
    result = asyncio.run(wheel.improve_controls(TRAIN, DEV, protected=(),
                         propensities={r.item.id: 1. for r in TRAIN}))
    trials = [r for r in result["trials"] if r["control"] == "tasks"]
    assert len(trials) == 2
    proposals = [e for e in wheel.history(1000) if e["kind"] == "proposal-validated" and "tasks" in e["proposal"]]
    assert all(len(p["proposal"]["tasks"]) == 1 for p in proposals)
    assert all(p["candidate"]["rubric"] == "Frozen rubric" and not p["candidate"]["example_ids"] for p in proposals)
    entries = FeatureBank(wheel.db).entries()
    assert len(entries) == 2
    assert all(entry["attempts"][0]["diagnostics"]["by_class"]["include"]["count"] == 3 for entry in entries)
    assert all(e["baseline_version"] == result["baseline_version"] for e in wheel.history(1000)
               if e["kind"] == "control-trial-started")
    wheel.close()


def test_feature_trial_ceiling_retains_untried_questions_and_never_bundles_them(tmp_path):
    calls = []
    normal = optimizer(calls)
    def discover(messages):
        proposal = json.loads(normal.complete(messages).content)
        if "tasks" in proposal:
            proposal["tasks"] += [{"name": f"factor{i}", "instructions": f"Is factor {i} present?",
                                   "labels": ["yes", "no"]} for i in range(3)]
        return OptimizerReply(json.dumps(proposal), "fake")
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), OptimizerAgent(discover))
    result = asyncio.run(wheel.improve_controls(TRAIN, DEV, protected=(),
                         propensities={r.item.id: 1. for r in TRAIN}, max_feature_trials=1))
    assert len([r for r in result["trials"] if r["control"] == "tasks"]) == 1
    assert len(wheel.feature_bank()) == 4
    assert sum(bool(entry["attempts"]) for entry in wheel.feature_bank()) == 1
    requests = wheel.requests
    assert asyncio.run(wheel.improve_controls(TRAIN, DEV, protected=(),
        propensities={r.item.id: 1. for r in TRAIN}, max_feature_trials=1)) == result
    assert wheel.requests == requests
    wheel.close()


def test_a_new_question_preserves_an_existing_question_and_records_only_training_diagnostics(tmp_path):
    from .models import DecisionTask
    existing = DecisionTask("existing", ("yes", "no"), "Existing measurement")
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK, tasks=(existing,)),
                             FakeModel(), optimizer([]), max_requests=500)
    asyncio.run(wheel.improve_controls(TRAIN, DEV, protected=(), propensities={r.item.id: 1. for r in TRAIN}))
    events = [e for e in wheel.history(1000) if e["kind"] == "proposal-validated" and "tasks" in e["proposal"]]
    assert len(events) == 1
    assert {t["name"] for t in events[0]["proposal"]["tasks"]} == {"existing", "practical"}
    assert events[0]["proposal"]["tasks"][0]["instructions"] == existing.instructions
    bank = wheel.feature_bank()
    assert sum(group["count"] for group in bank[0]["attempts"][0]["diagnostics"]["by_class"].values()) == len(TRAIN)
    assert set(bank[0]["discovery_evidence"][0]) == {r.item.id for r in TRAIN}
    wheel.close()


def test_dynamic_datetime_is_frozen_for_all_isolated_trials_in_one_cycle(tmp_path):
    instants = []
    class TimedModel(FakeModel):
        async def classify(self, config, target, training, *, now=None, event_sink=None):
            instants.append(now)
            return await super().classify(config, target, training, now=now, event_sink=event_sink)
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK, dynamic_elements=("current_datetime",)),
                             TimedModel(), optimizer([]))
    asyncio.run(wheel.improve_controls(TRAIN, DEV, protected=(), propensities={r.item.id: 1. for r in TRAIN}))
    assert len(set(instants)) == 1
    wheel.close()


def test_unchanged_feedback_does_not_repeat_discovery_and_retained_ideas_retry_with_more_labels(tmp_path):
    calls = []
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), optimizer(calls))
    scheduler = ControlScheduler(wheel)
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN})
    asyncio.run(scheduler.run(TRAIN, DEV, **kwargs))
    asyncio.run(scheduler.run(TRAIN, DEV, **kwargs))
    assert len(calls) == 3
    new = (*TRAIN, LabeledItem(Item("later", {"text": "yes later"}), "include"))
    asyncio.run(scheduler.run(new, DEV, protected=(), propensities={r.item.id: 1. for r in new}))
    assert any(len(idea["attempts"]) == 2 for idea in scheduler.ideas())
    wheel.close()


def test_restart_does_not_rediscover_a_completed_control_before_resuming_the_remaining_controls(tmp_path):
    import pytest
    calls = []
    normal = optimizer(calls)
    def interrupt(messages):
        if json.loads(messages[-1]["content"])["current"]["control_under_test"] == "example_ids":
            raise KeyboardInterrupt
        return normal.complete(messages)
    path = tmp_path / "wheel.sqlite"
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), OptimizerAgent(interrupt))
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN})
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(wheel.improve_controls(TRAIN, DEV, **kwargs))
    wheel.close()
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), optimizer(calls))
    result = asyncio.run(wheel.improve_controls(TRAIN, DEV, retry_interrupted=True, **kwargs))
    assert calls == ["rubric", "example_ids", "tasks"]
    assert len(result["trials"]) == 3
    assert result["promoted"]
    wheel.close()


def test_old_unsuccessful_ideas_get_retry_priority_even_when_new_ideas_keep_arriving(tmp_path):
    calls = []
    original = optimizer(calls)
    def varied(messages):
        reply = original.complete(messages)
        proposal = json.loads(reply.content)
        if "rubric" in proposal:
            proposal["rubric"] += f" idea {len(calls)}"
        if "example_ids" in proposal:
            proposal["example_ids"] = [f"t{(len(calls)//3) % 6}"]
        if "tasks" in proposal:
            proposal["tasks"][0]["name"] += str(len(calls))
        return OptimizerReply(json.dumps(proposal), "fake")
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), OptimizerAgent(varied),
                             max_requests=500, selection_policy=_BRIER)
    original_score = wheel._score
    async def neutral(*args):
        score = await original_score(*args)
        score["balanced_brier"] = .5
        return score
    wheel._score = neutral
    first_ids = []
    for cycle in range(4):
        training = (*TRAIN, *(LabeledItem(Item(f"new{i}", {"text": f"yes newer {i}"}), "include")
                              for i in range(cycle)))
        asyncio.run(wheel.improve_controls(training, DEV, protected=(),
                                          propensities={row.item.id: 1. for row in training}))
        if not cycle:
            first_ids = [idea["id"] for idea in ControlScheduler(wheel).ideas()]
    retained = {idea["id"]: idea for idea in ControlScheduler(wheel).ideas()}
    assert all(len(retained[key]["attempts"]) >= 2 for key in first_ids)
    wheel.close()
