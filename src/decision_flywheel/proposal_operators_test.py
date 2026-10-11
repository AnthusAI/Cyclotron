"""Proposal operators (Stage 2c): fake optimizers and a fake decision model only, no network."""
import hashlib
import json

import pytest

from .classifier_config import ClassifiedAnswers, ClassifierConfig
from .control_scheduler import ControlScheduler
from .cycle_replay import run_cycle_replay
from .flywheel import DecisionFlywheel
from .hypothesis_ledger import HypothesisLedger
from .idea_screening import (DEFAULT_SHADOW_MIX, IdeaScreeningConfig, OPERATOR_CAPS, incumbent_id,
                             parse_proposal_mix)
from .models import DecisionResult, Item, LabeledItem
from .optimizer_agent import FeedbackBriefing, OptimizerAgent, OptimizerReply, parse_operator_proposals
from .proposal_operators import ask_round, nearest_novelty, target_stories, text_novelty
from .replay import plan_replay
from .shadow_evaluation_test import TASK, TRAIN, make, replay_rows, run, truth

BASE = ("Include an item when it reports a completed outcome that readers can verify in the text. "
        "Exclude previews and opinion pieces.")
OTHER = ("Sports results: include only a final score. Animals and nature: include sightings that are confirmed. "
         "Boundary case: a rumor is never an outcome. Counter-rule: a recap of old news is excluded. "
         "For exclude, require speculation without a stated result.")


def bold(i):
    return " ".join(f"alpha{i}x{k}" for k in range(30))


def builder(wheel, training=TRAIN, development=(), protected=()):
    def build(extra):
        return FeedbackBriefing.build(TASK, training, current={**wheel.active.config.briefing_state(),
                                      "control_under_test": "rubric", **extra},
                                      protected=tuple(r.item for r in development) + tuple(protected))
    return build


class Operators:
    """Fake optimizer with propose_operators: replies come from a scripted list of lists."""
    def __init__(self, *replies):
        self.replies, self.briefings, self.observer = list(replies), [], lambda event: None

    def request_messages(self, briefing):
        return []

    def propose_operators(self, briefing):
        self.briefings.append(briefing.payload["current"])
        requested = {op: s["count"] for op, s in briefing.payload["current"]["operator_requests"].items()}
        return parse_operator_proposals(json.dumps({"proposals": self.replies.pop(0)}), requested)


def shadow_config(**kwargs):
    return IdeaScreeningConfig(enabled=True, **kwargs)


def events(wheel, kind):
    return [e for e in wheel.history(1000000) if e["kind"] == kind]


# -- parsing ----------------------------------------------------------------------------------------------

def test_operator_replies_are_parsed_strictly():
    requested = {"bold": 2, "target": 1}
    good = [{"operator": "bold", "rationale": "r", "rubric": "x"}, {"operator": "target", "rationale": "r", "rubric": "y"}]
    assert parse_operator_proposals(json.dumps({"proposals": good}), requested) == good
    bad = (
        [{"rationale": "r", "rubric": "x"}],                                          # operator missing
        [{"operator": "wild", "rationale": "r", "rubric": "x"}],                      # unknown operator
        [{"operator": "combine", "rationale": "r", "rubric": "x"}],                   # not requested
        [{"operator": "target", "rationale": "r", "rubric": str(i)} for i in range(2)],  # over its count
        [{"operator": "bold", "rationale": "r", "rubric": str(i)} for i in range(4)],  # over the total
        [],                                                                           # none at all
        ["bold"],
    )
    for proposals in bad:
        with pytest.raises(ValueError):
            parse_operator_proposals(json.dumps({"proposals": proposals}), requested)
    with pytest.raises(ValueError):
        parse_operator_proposals(json.dumps({"proposals": good, "extra": 1}), requested)
    with pytest.raises(ValueError):
        parse_operator_proposals("not json", requested)


def test_the_real_agent_sends_one_call_with_every_operator_block_and_parses_the_labels():
    seen = []

    def complete(messages):
        seen.append(messages)
        return OptimizerReply(json.dumps({"proposals": [{"operator": "bold", "rationale": "a", "rubric": "x"}]}), "fake")
    briefing = FeedbackBriefing.build(TASK, TRAIN, protected=(), current={
        "control_under_test": "rubric", "operator_requests": {"bold": {"count": 2}, "mutate": {"count": 1}}})
    assert OptimizerAgent(complete).propose_operators(briefing)[0]["operator"] == "bold"
    assert len(seen) == 1
    system = seen[0][0]["content"]
    assert "STRUCTURALLY" in system and "mutate:" in system and "target:" not in system
    with pytest.raises(ValueError):
        OptimizerAgent(complete).request_messages(FeedbackBriefing.build(TASK, TRAIN, protected=(), current={
            "control_under_test": "tasks", "operator_requests": {"bold": {"count": 1}}}))


# -- configuration ----------------------------------------------------------------------------------------

def test_defaults_mix_and_validation():
    config = shadow_config()
    assert config.shadow_ideas == 4 and config.proposal_mix is None and config.min_novelty == 0.35
    assert config.effective_mix() == DEFAULT_SHADOW_MIX and config.round_total() == 6
    assert IdeaScreeningConfig(enabled=True, mode="dev_screen").effective_mix() is None
    assert IdeaScreeningConfig(enabled=True, mode="dev_screen", proposals_per_round=5).round_total() == 5
    custom = shadow_config(proposal_mix={"bold": 3})
    assert custom.proposal_mix == {"mutate": 0, "bold": 3, "target": 0, "combine": 0} and custom.round_total() == 3
    assert IdeaScreeningConfig.from_json(custom.as_json()) == custom
    for bad in ({"proposal_mix": {"wild": 1}}, {"proposal_mix": {"bold": -1}}, {"proposal_mix": {"bold": 0}},
                {"proposal_mix": {"target": 2, "combine": 2}}, {"proposal_mix": {"bold": True}}, {"proposal_mix": []},
                {"min_novelty": 1.0}, {"min_novelty": True}, {"target_sample": 0}, {"target_sample": 99},
                {"novelty_retries": -1}, {"proposal_mix": {"bold": 1}, "mode": "dev_screen"}):
        with pytest.raises(ValueError):
            IdeaScreeningConfig(enabled=True, **bad)
    old = {k: v for k, v in config.as_json().items() if k not in ("proposal_mix", "min_novelty", "target_sample",
                                                                   "novelty_retries")}
    assert IdeaScreeningConfig.from_json(old) == config
    assert parse_proposal_mix("bold=2,target=1") == {"mutate": 0, "bold": 2, "target": 1, "combine": 0}
    for bad in ("bold", "bold=x", "wild=1", "bold=1,bold=2", "target=2", ""):
        with pytest.raises(ValueError):
            parse_proposal_mix(bad)


def test_an_optimizer_without_operators_gets_the_plain_request_with_the_mix_total(tmp_path):
    from .shadow_evaluation_test import Fake
    wheel, _ = make(tmp_path)
    fake = wheel.optimizer
    proposals, lineage = ask_round(wheel, builder(wheel), {}, "rubric", [], TRAIN, (), ())
    assert isinstance(fake, Fake) and lineage is None and len(proposals) == 3  # Fake offers three rubrics
    assert fake.briefings[0]["proposals_requested"] == 6
    assert events(wheel, "proposal-operator-skipped")[0]["operator"] == "all"
    dev, _ = make(tmp_path, IdeaScreeningConfig(enabled=True, mode="dev_screen", proposals_per_round=2), name="d.sqlite")
    ask_round(dev, builder(dev), {}, "rubric", [], TRAIN, (), ())
    assert dev.optimizer.briefings[0]["proposals_requested"] == 2 and "operator_requests" not in dev.optimizer.briefings[0]


# -- novelty ----------------------------------------------------------------------------------------------

def test_novelty_distance_is_one_minus_shingle_jaccard():
    assert text_novelty(BASE, BASE) == 0 and text_novelty("", "") == 0
    assert text_novelty("a b c d", "w x y z") == 1
    assert text_novelty(BASE, BASE.upper()) == 0
    assert text_novelty(BASE, BASE.replace("verify", "check")) < 0.35
    assert text_novelty(BASE, OTHER) > 0.9
    assert nearest_novelty(OTHER, [BASE, OTHER]) == 0 and nearest_novelty("x", []) == 1.0


def test_the_novelty_gate_rejects_near_duplicates_retries_once_and_accepts_different_proposals(tmp_path):
    wheel, _ = make(tmp_path, shadow_config(proposal_mix={"bold": 2}), optimizer=Operators(
        [{"operator": "bold", "rationale": "reword", "rubric": BASE.replace("verify", "check")},
         {"operator": "bold", "rationale": "new", "rubric": OTHER}],
        [{"operator": "bold", "rationale": "again", "rubric": BASE + " Also exclude previews."}]))
    wheel.active.config.rubric  # incumbent is "base"; use BASE as the incumbent text via a prior idea
    ControlScheduler(wheel).admit_proposals("rubric", [{"rationale": "p", "rubric": BASE}], TRAIN, 0)
    ideas = [i for i in ControlScheduler(wheel).ideas()]
    proposals, lineage = ask_round(wheel, builder(wheel), {}, "rubric", ideas, TRAIN, (), ())
    # one call plus one retry; the retry asks only for the rejected count; the survivor is the different one
    assert len(wheel.optimizer.briefings) == 2
    assert wheel.optimizer.briefings[1]["operator_requests"]["bold"]["count"] == 1
    assert [p["rubric"] for p in proposals] == [OTHER]
    assert lineage[0]["operator"] == "bold" and lineage[0]["novelty"] > 0.35
    assert len(events(wheel, "proposal-novelty-rejected")) == 2 and len(events(wheel, "proposal-operator-retry")) == 1
    assert events(wheel, "proposal-operator-skipped")[-1]["operator"] == "bold"
    # no retries configured: a single call
    wheel2, _ = make(tmp_path, shadow_config(proposal_mix={"bold": 1}, novelty_retries=0), name="n.sqlite",
                     optimizer=Operators([{"operator": "bold", "rationale": "r", "rubric": "base"}]))
    assert ask_round(wheel2, builder(wheel2), {}, "rubric", [], TRAIN, (), ())[0] == []
    assert len(wheel2.optimizer.briefings) == 1


def test_one_call_per_round_when_nothing_is_rejected(tmp_path):
    wheel, _ = make(tmp_path, shadow_config(proposal_mix={"mutate": 1, "bold": 2}), optimizer=Operators([
        {"operator": "mutate", "rationale": "m", "rubric": "base plus a clause about sports results being final"},
        {"operator": "bold", "rationale": "b1", "rubric": bold(1)}, {"operator": "bold", "rationale": "b2", "rubric": bold(2)},
        ]))
    proposals, lineage = ask_round(wheel, builder(wheel), {}, "rubric", [], TRAIN, (), ())
    assert len(wheel.optimizer.briefings) == 1 and len(proposals) == 3
    assert [m["operator"] for m in lineage] == ["mutate", "bold", "bold"]
    assert wheel.optimizer.briefings[0]["proposals_requested"] == 3
    assert set(wheel.optimizer.briefings[0]["operator_requests"]) == {"mutate", "bold"}


# -- target -----------------------------------------------------------------------------------------------

def story(i, label="include", text=None, comment=None):
    return LabeledItem(Item(f"g{i}", {"title": f"Title {i}", "text": text or f"Story body number {i}"}), label,
                       context={"human_feedback": comment or f"reason {i}"})


def seed_errors(wheel, rows):
    """rows: (item_id, label, wrong_choice); every idea and the incumbent got these wrong."""
    ledger = HypothesisLedger(wheel.db)
    for idea in ("i1", "i2", "i3", incumbent_id(wheel)):
        ledger.record_evaluations(idea, [{"item_id": i, "correct": False, "label": l, "choice": c} for i, l, c in rows],
                                  "shadow")
    return ledger


def test_the_target_sample_has_revealed_training_stories_only_with_labels_and_explanations(tmp_path):
    wheel, _ = make(tmp_path)
    training = [story(1, "include", comment="Outcome was established in the text"), story(2, "exclude"),
                story(3, "exclude"), story(4, "include", text="x" * 900)]
    development = [story(5, "include")]
    protected = [Item("g6", {"title": "Hidden", "text": "protected story"})]
    copy = story(7, "include", text="protected story")  # a training row whose text equals a protected story
    ledger = seed_errors(wheel, [("g1", "include", "exclude"), ("g2", "exclude", "include"), ("g3", "exclude", "include"),
                                 ("g4", "include", "exclude"), ("g5", "include", "exclude"), ("g6", "include", "exclude"),
                                 ("g7", "include", "exclude"), ("g8", "include", "exclude")])  # g8 never revealed
    stories = target_stories(wheel, ledger, [*training, copy], development, protected, 8)
    ids = [s["id"] for s in stories]
    assert sorted(ids) == ["g1", "g2", "g3", "g4"]
    by_id = {s["id"]: s for s in stories}
    assert by_id["g1"]["true_label"] == "include" and by_id["g1"]["answered"] == "exclude"
    assert by_id["g1"]["human_explanation"] == "Outcome was established in the text"
    assert by_id["g1"]["title"] == "Title 1" and by_id["g1"]["error_direction"] == "true include, answered exclude"
    assert len(by_id["g4"]["excerpt"]) <= OPERATOR_CAPS["story_chars"]
    # the sample is capped and balanced across error directions
    many = [story(i, "include" if i < 20 else "exclude") for i in range(10, 40)]
    ledger = seed_errors(wheel, [(f"g{i}", "include" if i < 20 else "exclude", "exclude" if i < 20 else "include")
                                 for i in range(10, 40)])
    sample = target_stories(wheel, ledger, many, (), (), 8)
    assert len(sample) == 8
    assert {s["error_direction"] for s in sample} == {"true include, answered exclude", "true exclude, answered include"}
    assert sum(s["true_label"] == "include" for s in sample) == 4


def test_the_target_operator_sends_the_stories_in_the_call_and_skips_without_any(tmp_path):
    wheel, _ = make(tmp_path, shadow_config(proposal_mix={"bold": 1, "target": 2}, target_sample=2), optimizer=Operators(
        [{"operator": "bold", "rationale": "b", "rubric": bold(1)}],
        [{"operator": "bold", "rationale": "b", "rubric": bold(2)}, {"operator": "target", "rationale": "t",
                                                                      "rubric": bold(3)}]))
    ask_round(wheel, builder(wheel), {}, "rubric", [], TRAIN, (), ())
    assert "target" not in wheel.optimizer.briefings[0]["operator_requests"]
    skipped = events(wheel, "proposal-operator-skipped")
    assert skipped[0]["operator"] == "target" and "wrong" in skipped[0]["reason"]
    training = [story(i, "include" if i % 2 else "exclude") for i in range(1, 5)]
    seed_errors(wheel, [(f"g{i}", "include" if i % 2 else "exclude", "exclude" if i % 2 else "include")
                        for i in range(1, 5)])
    proposals, lineage = ask_round(wheel, builder(wheel, training), {}, "rubric", [], training, (), ())
    sent = wheel.optimizer.briefings[1]["operator_requests"]["target"]
    assert len(sent["stories"]) == 2 and {"true_label", "human_explanation", "excerpt"} <= set(sent["stories"][0])
    assert [m["operator"] for m in lineage] == ["bold", "target"]


# -- combine and complementarity --------------------------------------------------------------------------

def record(ledger, idea, right, items=range(1, 13), context="shadow"):
    ledger.record_evaluations(idea, [{"item_id": f"s{i}", "correct": i in right, "label": "include", "choice": "x"}
                                     for i in items], context)


def test_the_complementary_pair_maximizes_the_smaller_one_sided_gain(tmp_path):
    wheel, _ = make(tmp_path)
    ledger = HypothesisLedger(wheel.db)
    record(ledger, "A", {1, 2, 3, 9, 10, 11, 12})
    record(ledger, "B", {3, 4, 5, 9, 10, 11, 12})
    record(ledger, "C", {1, 2, 3, 9, 10, 11, 12})
    pair = ledger.complementary_pair(min_items=10, min_fix=2)
    assert (pair["a"], pair["b"]) == ("A", "B") and pair["score"] == 2
    assert pair["fixes_a_not_b"] == ["s1", "s2"] and pair["fixes_b_not_a"] == ["s4", "s5"]
    # (A, C) never qualifies: they fix exactly the same stories
    assert ledger.complementary_pair(ids=["A", "C"], min_items=10)["skipped"]
    # a more balanced pair beats a lopsided one
    record(ledger, "D", {1, 2, 3, 4, 5, 6, 7, 8})
    best = ledger.complementary_pair(ids=["A", "B", "D"], min_items=10, min_fix=2)
    assert best["score"] == 4 and (best["a"], best["b"]) == ("A", "D")   # min(4, 5) beats (A, B)'s min(2, 2)
    assert ledger.complementary_pair(ids=["A", "B"], min_items=10, exclude_pairs={frozenset(("A", "B"))})["skipped"]
    assert "fewer than two" in ledger.complementary_pair(ids=["A"], min_items=10)["skipped"]
    assert "fewer than two" in ledger.complementary_pair(ids=["A", "B"], min_items=50)["skipped"]


def test_combine_sends_both_parents_and_records_them_and_is_skipped_with_an_event_otherwise(tmp_path):
    wheel, _ = make(tmp_path, shadow_config(proposal_mix={"bold": 1, "combine": 1}), optimizer=Operators(
        [{"operator": "bold", "rationale": "b", "rubric": bold(1)}],
        [{"operator": "bold", "rationale": "b", "rubric": bold(2)},
         {"operator": "combine", "rationale": "c", "rubric": bold(3)}]))
    ask_round(wheel, builder(wheel), {}, "rubric", [], TRAIN, (), ())
    skipped = events(wheel, "proposal-operator-skipped")
    assert skipped[0]["operator"] == "combine" and "fewer than two eligible ideas" in skipped[0]["reason"]
    assert "combine" not in wheel.optimizer.briefings[0]["operator_requests"]
    # two ideas with different strengths, plus the incumbent
    sched = ControlScheduler(wheel)
    sched.admit_proposals("rubric", [{"rationale": "one", "rubric": OTHER}, {"rationale": "two", "rubric": bold(9)}],
                          TRAIN, 1)
    ideas = sched.ideas()
    one, two = (next(i["id"] for i in ideas if i["proposal"]["rubric"] == r) for r in (OTHER, bold(9)))
    ledger = HypothesisLedger(wheel.db)
    record(ledger, one, {1, 2, 3, 9, 10})
    record(ledger, two, {3, 4, 5, 9, 10})
    record(ledger, incumbent_id(wheel), {9, 10})
    proposals, lineage = ask_round(wheel, builder(wheel), {}, "rubric", ideas, TRAIN, (), ())
    block = wheel.optimizer.briefings[1]["operator_requests"]["combine"]
    assert {p["id"] for p in block["parents"]} <= {one, two, incumbent_id(wheel)} and len(block["parents"]) == 2
    assert all(p["rubric"] and "fixes_that_the_other_misses" in p for p in block["parents"])
    combined = [m for m in lineage if m["operator"] == "combine"][0]
    assert sorted(combined["parents"]) == sorted(p["id"] for p in block["parents"])


def test_the_prompt_payload_is_capped(tmp_path):
    wheel, _ = make(tmp_path, shadow_config(proposal_mix={"bold": 1, "target": 1, "combine": 1}, target_sample=20),
                    optimizer=Operators([{"operator": "bold", "rationale": "b", "rubric": bold(1)}]))
    huge = "word " * 5000
    sched = ControlScheduler(wheel)
    sched.admit_proposals("rubric", [{"rationale": "one", "rubric": huge}, {"rationale": "three", "rubric": huge + "z"}],
                          TRAIN, 1)
    training = [story(i, text="long " * 2000) for i in range(1, 40)]
    seed_errors(wheel, [(f"g{i}", "include", "exclude") for i in range(1, 40)])
    ledger = HypothesisLedger(wheel.db)
    for idea in sched.ideas():
        record(ledger, idea["id"], {1, 2, 3, 9} if idea["rationale"] == "one" else {3, 4, 5, 9})
    record(ledger, incumbent_id(wheel), {9})
    ask_round(wheel, builder(wheel, training), {}, "rubric", sched.ideas(), training, (), ())
    requests = wheel.optimizer.briefings[0]["operator_requests"]
    assert len(requests["target"]["stories"]) == 20
    assert all(len(s["excerpt"]) <= OPERATOR_CAPS["story_chars"] for s in requests["target"]["stories"])
    assert all(len(p["rubric"]) <= OPERATOR_CAPS["parent_rubric_chars"] for p in requests["combine"]["parents"])
    assert len(json.dumps(requests)) < 60000


# -- lineage ----------------------------------------------------------------------------------------------

def test_lineage_is_persisted_shown_to_the_optimizer_and_walkable(tmp_path):
    wheel, _ = make(tmp_path)
    sched = ControlScheduler(wheel)
    sched.admit_proposals("rubric", [{"rationale": "plain", "rubric": bold(1)}], TRAIN, 1)  # no lineage: old shape
    sched.admit_proposals("rubric", [{"rationale": "a", "rubric": bold(2)}, {"rationale": "b", "rubric": bold(3)}], TRAIN, 1,
                          [{"operator": "bold", "parents": [], "novelty": 0.9},
                           {"operator": "target", "parents": [], "novelty": 0.5}])
    by_text = {i["proposal"]["rubric"]: i for i in sched.ideas()}
    a, b, plain = (by_text[bold(n)]["id"] for n in (2, 3, 1))
    assert "operator" not in by_text[bold(1)] and by_text[bold(2)]["novelty"] == 0.9
    sched.admit_proposals("rubric", [{"rationale": "c", "rubric": bold(4)}], TRAIN, 2,
                          [{"operator": "combine", "parents": [a, b], "novelty": 0.7}])
    child = next(i["id"] for i in sched.ideas() if i["proposal"]["rubric"] == bold(4))
    created = events(wheel, "proposal-operator")
    assert len(created) == 3 and set(created[-1]) >= {"idea_id", "operator", "parents", "novelty"}
    assert created[-1]["parents"] == [a, b]
    assert len(events(wheel, "hypothesis-discovered")) == 4
    ledger = HypothesisLedger(wheel.db)
    inc = incumbent_id(wheel)
    for idea in (a, b, child, plain):
        record(ledger, idea, {1, 2, 3}, items=range(1, 6))
    record(ledger, inc, {1}, items=range(1, 6))
    rows = {r["idea_id"]: r for r in ledger.summary_for_optimizer(incumbent_id=inc, context_version="shadow")}
    assert rows[child]["operator"] == "combine" and rows[child]["parents"] == [a[:8], b[:8]]
    assert rows[child]["outcome"] == "untested" and "operator" not in rows[plain]
    walk = ledger.lineage(child)
    assert walk["operator"] == "combine" and walk["parents"] == [a, b]
    assert [(r["idea_id"], r["operator"], r["depth"]) for r in walk["ancestors"]] == [(a, "bold", 1), (b, "target", 1)]
    assert ledger.lineage(plain)["operator"] is None and ledger.lineage("nothing")["ancestors"] == []
    # the persisted idea still loads for older readers (extra keys only)
    assert all({"id", "control", "proposal", "rationale", "attempts"} <= set(i) for i in sched.ideas())


def test_operator_outcomes_aggregate_per_operator(tmp_path):
    wheel, _ = make(tmp_path)
    sched = ControlScheduler(wheel)
    marks = [{"operator": "bold", "parents": [], "novelty": 0.9}, {"operator": "bold", "parents": [], "novelty": 0.8},
             {"operator": "target", "parents": [], "novelty": 0.5}, {"operator": "mutate", "parents": [], "novelty": 0.1}]
    sched.admit_proposals("rubric", [{"rationale": str(n), "rubric": bold(n)} for n in range(4)], TRAIN, 1, marks)
    ids = [next(i["id"] for i in sched.ideas() if i["proposal"]["rubric"] == bold(n)) for n in range(4)]
    ledger = HypothesisLedger(wheel.db)
    ledger.record_verdict(ids[0], "advanced", "r", {"net": 4})
    ledger.record_verdict(ids[0], "promoted", "r", {"net": 6})
    ledger.record_verdict(ids[1], "screened_out", "r", {"net": -2})
    ledger.record_verdict(ids[2], "advanced", "r", {"net": 3})
    out = ledger.operator_outcomes()
    assert out["bold"] == {"proposed": 2, "advanced": 1, "promoted": 1, "mean_forward_net": 2.0}
    assert out["target"] == {"proposed": 1, "advanced": 1, "promoted": 0, "mean_forward_net": 3.0}
    assert out["mutate"] == {"proposed": 1, "advanced": 0, "promoted": 0, "mean_forward_net": None}
    assert "combine" not in out
    from .idea_screening import optimizer_context
    assert optimizer_context(wheel, ledger, sched.ideas(), set())["operator_outcomes"] == out


# -- study script -----------------------------------------------------------------------------------------

def test_the_study_script_parses_validates_and_records_the_operator_flags(capsys):
    import importlib.util
    import pathlib
    from argparse import Namespace
    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "run_editorial_prequential_study.py"
    spec = importlib.util.spec_from_file_location("study_script_operators", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as error:
        pytest.skip(str(error))
    with pytest.raises(SystemExit):
        module.main(["--help"])
    help_text = capsys.readouterr().out
    assert all(flag in help_text for flag in ("--proposal-mix", "--min-novelty", "--target-sample"))
    for bad in ("wild=1", "bold=x", "target=2"):
        with pytest.raises(SystemExit):
            module.main(["--proposal-mix", bad])
        capsys.readouterr()
    base = dict(idea_screening=True, screening_max_candidates=8, screening_proposals=3, screening_finalists=2,
                screening_reserve=.2, screening_alpha=.05, screening_max_calls=2000, screening_mode="shadow",
                shadow_ideas=4, shadow_min_items=40, shadow_evict_after=30, shadow_looks=10, max_shadow_calls=3000)
    config = module.idea_screening_config(Namespace(**base, proposal_mix=module.proposal_mix_type("bold=1,combine=3"),
                                                    min_novelty=0.5, target_sample=5))
    recorded = config.as_json()
    assert recorded["proposal_mix"] == {"mutate": 0, "bold": 1, "target": 0, "combine": 3}
    assert (recorded["min_novelty"], recorded["target_sample"]) == (0.5, 5)
    unset = module.idea_screening_config(Namespace(**base, proposal_mix=None, min_novelty=None, target_sample=None))
    assert unset == IdeaScreeningConfig(enabled=True) and unset.as_json()["proposal_mix"] is None


# -- end to end -------------------------------------------------------------------------------------------

class HashModel:
    model_identity = "hash-model"

    def __init__(self):
        self.calls = []

    async def classify(self, config, target, training, *, now=None, event_sink=None):
        self.calls.append((config.rubric, target.id))
        truth_label = "include" if int(target.id[1:]) % 2 else "exclude"
        draw = int(hashlib.sha256(f"{config.rubric}:{target.id}".encode()).hexdigest()[:8], 16) / 16 ** 8
        label = truth_label if draw < .7 else ("exclude" if truth_label == "include" else "include")
        other = "exclude" if label == "include" else "include"
        return ClassifiedAnswers({"decision": DecisionResult(label, {label: .9, other: .1})}, "hash-model",
                                 {"input_tokens": 1}, 1)


def test_a_fresh_replay_with_the_new_mix_starts_completes_and_records_lineage(tmp_path):
    prompts, counter = [], [0]

    def complete(messages):
        prompts.append(messages)
        current = json.loads(messages[1]["content"])["current"]
        if "operator_requests" not in current:  # the stage's plain path before screening has enough evidence
            return OptimizerReply(json.dumps({"rationale": "plain", "rubric": "base"}), "fake")
        proposals = []
        for operator, spec in sorted(current["operator_requests"].items()):
            for _ in range(spec["count"]):
                counter[0] += 1
                proposals.append({"operator": operator, "rationale": f"{operator} {counter[0]}", "rubric": bold(counter[0])})
        return OptimizerReply(json.dumps({"proposals": proposals}), "fake")
    wheel = DecisionFlywheel(tmp_path / "w.sqlite", ClassifierConfig(TASK, rubric="base"), HashModel(),
                             OptimizerAgent(complete), max_requests=100000,
                             idea_screening=shadow_config(shadow_min_items=10, shadow_evict_after=8,
                                                          proposal_mix={"bold": 2, "target": 2, "combine": 2}))
    plan = plan_replay(TASK, replay_rows(240), seed="mix", batch_size=40)
    report = run(run_cycle_replay(wheel, plan, optimize_every=10 ** 6, retrain_every=10 ** 6, stages=("rubric",),
                                  rubric_changes_every=12, rubric_trigger_basis="revealed_feedback_count",
                                  min_evaluation_per_class=2))
    assert len(report["cycles"]) == 240 and "stopped_reason" not in report
    created = events(wheel, "proposal-operator")
    assert created and {e["operator"] for e in created} <= {"bold", "target", "combine"}
    assert {e["operator"] for e in created} == {"bold", "target", "combine"}
    assert all(0 <= e["novelty"] <= 1 for e in created)
    operator_calls = [p for p in prompts if "operator_requests" in json.loads(p[1]["content"])["current"]]
    assert len(operator_calls) >= 2   # one optimizer call per screening round (no retries: nothing was rejected)
    assert all(len(e["parents"]) == 2 for e in created if e["operator"] == "combine")
    assert "STRUCTURALLY" in operator_calls[0][0]["content"]
    wheel.close()
