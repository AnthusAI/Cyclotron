"""Idea screening in the live loop: fake decision model and fake optimizer only, no network."""
import asyncio
import json
import random
import sqlite3

import pytest

from .classifier_config import ClassifiedAnswers, ClassifierConfig
from .flywheel import DecisionFlywheel
from .hypothesis_ledger import HypothesisLedger
from .idea_screening import (CAPS, DevelopmentScorer, IdeaScreeningConfig, confirm, parse_proposals,
                             reserve_member, validate_proposals)
from .models import DecisionResult, DecisionTask, Item, LabeledItem
from .optimizer_agent import FeedbackBriefing, OptimizerAgent, OptimizerReply
from .selection_policy import SelectionPolicy

TASK = DecisionTask("inclusion", ("include", "exclude"), "Should this item be included?")
RULES = {  # rubric -> which item indexes the fake decision model gets right
    "base": lambda i: i % 4 != 0,
    "same": lambda i: i % 4 != 0,
    "also same": lambda i: i % 4 != 0,
    "better": lambda i: i % 8 != 0,
    "worse": lambda i: i % 2 == 0,
}


class RubricModel:
    model_identity = "rubric-model"

    def __init__(self):
        self.seen = []

    async def classify(self, config, target, training, *, now=None, event_sink=None):
        self.seen.append(target.id)
        index = int(target.id[1:])
        truth = "include" if index % 2 else "exclude"
        right = RULES[config.rubric](index)
        label = truth if right else ("exclude" if truth == "include" else "include")
        other = "exclude" if label == "include" else "include"
        return ClassifiedAnswers({"decision": DecisionResult(label, {label: .9, other: .1})}, "rubric-model",
                                 {"input_tokens": 1}, 1)


class Fake:
    """Multi-proposal optimizer; example_ids and tasks controls propose no change."""
    def __init__(self, rubrics):
        self.rubrics, self.observer, self.briefings, self.single_calls = rubrics, lambda event: None, [], 0

    def request_messages(self, briefing):
        return []

    def propose_many(self, briefing):
        self.briefings.append(briefing.payload["current"])
        control = briefing.payload["current"]["control_under_test"]
        n = briefing.payload["current"]["proposals_requested"]
        if control == "rubric":
            return [{"rationale": f"try {r}", "rubric": r} for r in self.rubrics[:n]]
        return [{"rationale": "keep", control: briefing.payload["current"][control]}]

    def propose(self, briefing):
        self.single_calls += 1
        control = briefing.payload["current"]["control_under_test"]
        if control == "rubric":
            return {"rationale": "single", "rubric": self.rubrics[0]}
        return {"rationale": "keep", control: briefing.payload["current"][control]}


def rows(start, stop):
    return tuple(LabeledItem(Item(f"d{i}", {"text": f"dev item number {i}"}), "include" if i % 2 else "exclude")
                 for i in range(start, stop))


TRAIN = tuple(LabeledItem(Item(f"t{i}", {"text": f"train item number {i}"}), "include" if i % 2 else "exclude")
              for i in range(8))
PROPS = {row.item.id: 1. for row in TRAIN}
PROTECTED = tuple(Item(f"p{i}", {"text": f"protected item number {i}"}) for i in range(10))
ON = IdeaScreeningConfig(enabled=True, mode="dev_screen", stages=((60, .5), (None, None)), reserve_fraction=.3)


def make(tmp_path, rubrics, *, screening=ON, optimizer=None, name="w.sqlite", **kwargs):
    model = RubricModel()
    wheel = DecisionFlywheel(tmp_path / name, ClassifierConfig(TASK, rubric="base"), model,
                             optimizer or Fake(rubrics), max_requests=100000, idea_screening=screening, **kwargs)
    return wheel, model


def run(wheel, dev):
    return asyncio.run(wheel.improve_controls(TRAIN, dev, protected=PROTECTED, propensities=PROPS))


def verdicts(wheel):
    ledger = HypothesisLedger(wheel.db)
    return {i: ledger.verdicts(i) for (i,) in wheel.db.execute("SELECT DISTINCT idea_id FROM idea_verdicts")}


def kinds(wheel):
    return [e["kind"] for e in wheel.history(100000)]


def test_default_is_off_and_uses_the_one_proposal_path(tmp_path):
    wheel, model = make(tmp_path, ["better"], screening=None)
    assert wheel.idea_screening == IdeaScreeningConfig() and not wheel.screening_enabled()
    result = run(wheel, rows(0, 40))
    assert wheel.optimizer.single_calls == 3 and not wheel.optimizer.briefings
    assert "idea-screening-completed" not in kinds(wheel)
    assert wheel.db.execute("SELECT count(*) FROM sqlite_master WHERE name='idea_evaluations'").fetchone()[0] == 0
    assert result["promoted"]
    wheel.close()


def test_three_proposals_are_screened_confirmed_and_the_better_one_is_promoted(tmp_path):
    wheel, model = make(tmp_path, ["worse", "better", "same"])
    result = run(wheel, rows(0, 300))
    assert result["promoted"] and wheel.active.config.rubric == "better"
    assert kinds(wheel).count("hypothesis-discovered") == 3
    ledger = wheel.db.execute("SELECT count(DISTINCT idea_id) FROM idea_evaluations").fetchone()[0]
    assert ledger == 4  # three ideas plus the incumbent
    seen = verdicts(wheel)
    ideas = {i["proposal"]["rubric"]: i["id"] for i in
             (json.loads(p) for (p,) in wheel.db.execute("SELECT payload FROM control_ideas")) if "rubric" in i["proposal"]}
    assert seen[ideas["better"]][-1]["verdict"] == "promoted"
    assert seen[ideas["better"]][-1]["evidence"]["gained"] > 0 and seen[ideas["better"]][-1]["evidence"]["lost"] == 0
    assert seen[ideas["worse"]][-1]["verdict"] == "screened_out"
    assert all(v for v in seen.values()) and wheel.db.execute("SELECT count(*) FROM control_ideas").fetchone()[0] == 3
    briefing = wheel.optimizer.briefings[0]
    assert briefing["proposals_requested"] == 3 and briefing["hypothesis_ledger"] == []
    assert wheel.optimizer_context["evaluation_context_exposed"]
    wheel.close()


def test_confirmation_accumulates_over_rounds_as_reserve_items_arrive(tmp_path):
    wheel, model = make(tmp_path, ["better", "same"])
    small = run(wheel, rows(0, 60))
    assert not small["promoted"]
    ledger = HypothesisLedger(wheel.db)
    better = next(i for i, v in verdicts(wheel).items() if "reserve confirmation" in v[-1]["reason"]
                  and v[-1]["evidence"]["net"] > 0)
    assert ledger.verdicts(better)[-1]["verdict"] == "advanced"
    seen_before = len(model.seen)
    big = rows(0, 400)
    result = run(wheel, big)
    assert result["promoted"] and wheel.active.config.rubric == "better"
    evidence = ledger.verdicts(better)[-1]["evidence"]
    assert evidence["shared"] > 18  # more reserve items than the first round had
    salt = f"{TASK.fingerprint}:idea-screening"
    early = {r.item.id for r in rows(0, 60) if reserve_member(salt, r.item.id, .3)}
    assert early <= set(ledger.items_evaluated(better))  # earlier reserve evidence was kept, not redone
    assert len(model.seen) > seen_before
    wheel.close()


def test_equal_proposals_promote_nothing(tmp_path):
    wheel, model = make(tmp_path, ["same", "also same"])
    result = run(wheel, rows(0, 300))
    assert not result["promoted"] and wheel.active.config.rubric == "base"
    assert not any(v[-1]["verdict"] == "promoted" for v in verdicts(wheel).values())
    wheel.close()


def test_protected_and_training_items_are_never_scored_by_the_screen(tmp_path, monkeypatch):
    log = []
    original = DevelopmentScorer.__call__

    async def spy(self, idea, row):
        log.append(row.item.id)
        return await original(self, idea, row)
    monkeypatch.setattr(DevelopmentScorer, "__call__", spy)
    wheel, model = make(tmp_path, ["better", "same"])
    dev = rows(0, 200)
    run(wheel, dev)
    assert log and set(log) <= {r.item.id for r in dev}
    assert not set(model.seen) & {i.id for i in PROTECTED}
    scorer = DevelopmentScorer(wheel, TRAIN, None, {}, ["d1"])
    with pytest.raises(ValueError, match="development-eligible"):
        asyncio.run(scorer({"id": "x"}, LabeledItem(PROTECTED[0], "include")))
    wheel.close()


def test_the_screen_call_budget_truncates_and_promotes_nothing(tmp_path):
    tight = IdeaScreeningConfig(enabled=True, mode="dev_screen", max_screen_calls=40, stages=((60, .5), (None, None)))
    wheel, model = make(tmp_path, ["better", "same"], screening=tight)
    result = run(wheel, rows(0, 300))
    assert not result["promoted"] and wheel.active.config.rubric == "base"
    done = [e for e in wheel.history(1000) if e["kind"] == "idea-screening-completed"]
    assert done and done[0]["truncated"] and done[0]["calls"] <= 40
    assert not any(v[-1]["verdict"] == "promoted" for v in verdicts(wheel).values())
    wheel.close()


def test_screen_calls_count_against_the_wheel_request_ceiling(tmp_path):
    wheel, model = make(tmp_path, ["better", "same"])
    wheel.max_requests = 200
    result = run(wheel, rows(0, 300))
    assert wheel.requests <= 200 and not result["promoted"]
    wheel.close()


def test_a_non_accuracy_objective_falls_back_to_the_legacy_path_with_one_event(tmp_path):
    wheel, model = make(tmp_path, ["better"], selection_policy=SelectionPolicy("f1", positive_class="include"))
    run(wheel, rows(0, 40))
    run(wheel, rows(0, 42))
    assert kinds(wheel).count("idea-screening-unsupported-objective") == 1
    assert wheel.optimizer.single_calls >= 3 and not wheel.optimizer.briefings
    assert "idea-screening-completed" not in kinds(wheel)
    wheel.close()


def test_multi_proposal_parsing_is_strict():
    ok = json.dumps({"proposals": [{"rationale": "a", "rubric": "x"}, {"rationale": "b", "rubric": "y"}]})
    assert len(validate_proposals(parse_proposals(ok, 3), "rubric", 3)) == 2
    for bad in ("not json", "[]", '{"proposals": []}', '{"proposals": "x"}', '{"proposals": [1]}',
                '{"proposals": [{}], "extra": 1}', json.dumps({"proposals": [{"rubric": "x"}] * 4})):
        with pytest.raises(ValueError):
            parse_proposals(bad, 3)
    with pytest.raises(ValueError, match="duplicate"):
        validate_proposals([{"rubric": "x"}, {"rubric": "x", "rationale": "other"}], "rubric", 3)
    with pytest.raises(ValueError):
        validate_proposals([{"rubric": "x", "tasks": []}], "rubric", 3)
    with pytest.raises(ValueError):
        validate_proposals([{"rationale": "no control"}], "rubric", 3)


def test_the_real_agent_asks_for_distinct_proposals_and_parses_the_reply():
    seen = []

    def complete(messages):
        seen.append(messages)
        return OptimizerReply(json.dumps({"proposals": [{"rationale": "a", "rubric": "x"},
                                                        {"rationale": "b", "rubric": "y"}]}), "fake")
    briefing = FeedbackBriefing.build(TASK, TRAIN, current={"control_under_test": "rubric", "proposals_requested": 3,
                                                            "hypothesis_ledger": [], "stubborn_items": []},
                                      protected=())
    assert len(OptimizerAgent(complete).propose_many(briefing)) == 2
    assert "3 DISTINCT proposals" in seen[0][0]["content"] and "hypothesis_ledger" in seen[0][0]["content"]
    single = FeedbackBriefing.build(TASK, TRAIN, current={"control_under_test": "rubric"}, protected=())
    assert "DISTINCT" not in OptimizerAgent(complete).request_messages(single)[0]["content"]


def test_the_configuration_survives_a_restart_and_can_be_replaced(tmp_path):
    wheel, _ = make(tmp_path, [], screening=IdeaScreeningConfig(enabled=True, mode="dev_screen", finalists=1, proposals_per_round=4))
    wheel.close()
    wheel, _ = make(tmp_path, [], screening=None)
    assert wheel.idea_screening.enabled and wheel.idea_screening.finalists == 1
    assert wheel.idea_screening.stages == IdeaScreeningConfig().stages
    wheel.close()
    wheel, _ = make(tmp_path, [], screening=IdeaScreeningConfig())
    wheel.close()
    wheel, _ = make(tmp_path, [], screening=None)
    assert not wheel.idea_screening.enabled
    wheel.close()
    with pytest.raises(ValueError):
        IdeaScreeningConfig(finalists=0)
    with pytest.raises(ValueError):
        IdeaScreeningConfig(reserve_fraction=1.)


def test_the_optimizer_digest_is_capped_and_hides_reserve_outcomes(tmp_path):
    wheel, model = make(tmp_path, ["better", "same", "worse"])
    run(wheel, rows(0, 300))
    briefing = wheel.optimizer.briefings[1]  # the example_ids request, after the rubric screen
    assert briefing["control_under_test"] == "example_ids"
    assert len(briefing["hypothesis_ledger"]) <= CAPS["ledger_ideas"]
    assert len(briefing["stubborn_items"]) <= CAPS["stubborn_items"]
    assert len(briefing["prior_control_ideas"]) <= CAPS["prior_ideas"]
    salt = f"{TASK.fingerprint}:idea-screening"
    shown = {i for row in briefing["hypothesis_ledger"] for i in row.get("fixed", []) + row.get("broke", [])}
    assert shown and not any(reserve_member(salt, i, ON.reserve_fraction) for i in shown)
    wheel.close()


# Sequential-confirmation behavior on the reserve, simulated with the real confirm() and ledger.
def simulate(seed, gain, loss, rounds=6, per_round=20):
    db = sqlite3.connect(":memory:")
    ledger, rng = HypothesisLedger(db), random.Random(seed)
    truth = {}

    async def scorer(idea, row):
        mine, base = truth[row]
        return {"correct": mine if idea["id"] == "cand" else base, "label": "x", "choice": "x", "confidence": .5}

    async def go():
        for r in range(rounds):
            reserve = list(range((r + 1) * per_round))  # the reserve only grows
            for i in reserve[r * per_round:]:
                u = rng.random()
                truth[i] = (True, False) if u < gain else (False, True) if u < gain + loss else \
                    ((x := rng.random() < .7), x)
            out = await confirm(["cand"], {"cand": {"id": "cand"}}, {"id": "inc"}, reserve, scorer, ledger,
                                alpha=.05, max_calls=10 ** 6, calls_used=0, item_id=lambda row: str(row))
            if out["cleared"]:
                return r + 1
        return None
    return asyncio.run(go())


def test_a_real_paired_advantage_is_promoted_within_a_few_rounds_and_a_null_one_rarely():
    real = [simulate(s, .20, .05, rounds=10) for s in range(60)]   # about 20 gained and 5 lost per 100 reserve items
    null = [simulate(s, .10, .10, rounds=6) for s in range(300)]   # six looks at the growing reserve
    assert sum(r is not None for r in real) / len(real) >= .9
    assert sorted(r for r in real if r)[len(real) // 2] <= 6
    false_rate = sum(r is not None for r in null) / len(null)
    assert false_rate <= .10, false_rate


def test_an_exact_pattern_of_four_gained_and_one_lost_per_round_clears_in_three_rounds():
    db = sqlite3.connect(":memory:")
    ledger, outcomes = HypothesisLedger(db), []

    async def go():
        for r in range(5):
            reserve = list(range(r * 20 + 20))

            async def scorer(idea, row):
                k = row % 20
                cand = k >= 1  # lose item 0 of each block, gain items 1..4, tie the rest
                base = k == 0 or k > 4
                if k > 4:
                    cand = base = k % 2 == 0
                return {"correct": cand if idea["id"] == "cand" else base, "label": "x", "choice": "x", "confidence": .5}
            out = await confirm(["cand"], {"cand": {"id": "cand"}}, {"id": "inc"}, reserve, scorer, ledger,
                                alpha=.05, max_calls=10 ** 6, calls_used=0, item_id=lambda row: str(row))
            outcomes.append((out["paired"]["cand"]["gained"], out["paired"]["cand"]["lost"], bool(out["cleared"])))
    asyncio.run(go())
    assert [o[:2] for o in outcomes[:3]] == [(4, 1), (8, 2), (12, 3)]
    assert [o[2] for o in outcomes] == [False, False, True, True, True]


def test_the_staged_rubric_path_screens_several_proposals_and_promotes_through_improve(tmp_path):
    wheel, model = make(tmp_path, ["worse", "better", "same"])
    result = asyncio.run(wheel.optimize_stage("rubric", TRAIN, rows(0, 300), protected=PROTECTED, propensities=PROPS))
    assert result["promoted"] and result["promoted_idea"] and wheel.active.config.rubric == "better"
    assert result["idea_screening"]["reserve_size"] > 0 and not result["idea_screening"]["truncated"]
    assert kinds(wheel).count("hypothesis-discovered") == 3 and wheel.optimizer.single_calls == 0
    assert not set(model.seen) & {i.id for i in PROTECTED}
    wheel.close()
    wheel, _ = make(tmp_path, ["better"], screening=None, name="legacy.sqlite")
    legacy = asyncio.run(wheel.optimize_stage("rubric", TRAIN, rows(0, 300), protected=PROTECTED, propensities=PROPS))
    assert "idea_screening" not in legacy and wheel.optimizer.single_calls == 1
    wheel.close()


def test_the_same_inputs_repeat_the_same_round_exactly(tmp_path):
    def snapshot(name):
        wheel, _ = make(tmp_path, ["worse", "better", "same"], name=name)
        run(wheel, rows(0, 300))
        out = (wheel.db.execute("SELECT idea_id,item_id,correct,context_version FROM idea_evaluations ORDER BY 1,2,4").fetchall(),
               sorted((v, r) for rs in verdicts(wheel).values() for _, v, r, _ in
                      [(x["at"], x["verdict"], x["reason"], 0) for x in rs]))
        wheel.close()
        return out
    assert snapshot("a.sqlite") == snapshot("b.sqlite")
