"""Forward shadow evaluation (Stage 2b): fake decision model and fake optimizer only, no network."""
import asyncio
import hashlib
import random
import sqlite3

import pytest

from .classifier_config import ClassifiedAnswers, ClassifierConfig
from .control_scheduler import ControlScheduler
from .cycle_replay import run_cycle_replay
from .flywheel import DecisionFlywheel
from .hypothesis_ledger import HypothesisLedger, sign_test
from .idea_screening import IdeaScreeningConfig
from .models import DecisionResult, DecisionTask, Item, LabeledItem
from .replay import plan_replay
from .shadow_evaluation import ShadowEvaluator, clears_look

TASK = DecisionTask("inclusion", ("include", "exclude"), "Should this item be included?")


def draw(item_id, salt):
    return int(hashlib.sha256(f"{salt}:{item_id}".encode()).hexdigest()[:10], 16) / 16 ** 10


def base_right(item_id):
    return draw(item_id, "a") < .75


# Correlated accuracy: an idea mostly agrees with the incumbent and differs on a minority of stories.
RIGHT = {
    "base": base_right,
    "same": base_right,
    "same too": base_right,
    "better": lambda i: draw(i, "b") < (.95 if base_right(i) else .55),   # about 85% right, net +10 points
    "worse": lambda i: draw(i, "c") < (.55 if base_right(i) else .30),
}


class RubricModel:
    model_identity = "rubric-model"

    def __init__(self):
        self.calls = []  # (rubric, item id), every real classification request

    async def classify(self, config, target, training, *, now=None, event_sink=None):
        self.calls.append((config.rubric, target.id))
        index = int(target.id[1:])
        truth = "include" if index % 2 else "exclude"
        right = RIGHT[config.rubric](target.id)
        label = truth if right else ("exclude" if truth == "include" else "include")
        other = "exclude" if label == "include" else "include"
        return ClassifiedAnswers({"decision": DecisionResult(label, {label: .9, other: .1})}, "rubric-model",
                                 {"input_tokens": 1}, 1)


class Fake:
    """Multi-proposal optimizer proposing the same rubrics every time."""
    def __init__(self, rubrics=("better", "worse", "same")):
        self.rubrics, self.observer, self.briefings = list(rubrics), lambda event: None, []

    def request_messages(self, briefing):
        return []

    def propose_many(self, briefing):
        self.briefings.append(briefing.payload["current"])
        n = briefing.payload["current"]["proposals_requested"]
        return [{"rationale": f"try {r}", "rubric": r} for r in self.rubrics[:n]]

    def propose(self, briefing):
        return {"rationale": "single", "rubric": self.rubrics[0]}


TRAIN = tuple(LabeledItem(Item(f"t{i}", {"text": f"train item number {i}"}), "include" if i % 2 else "exclude")
              for i in range(8))


OFFSET = 1000  # a fixed, typical stretch of the deterministic story stream (parity, hence the label, is unchanged)


def sid(i):
    return f"s{OFFSET + i}"


def item(i):
    return Item(sid(i), {"text": f"story number {OFFSET + i}"})


def truth(i):
    return "include" if i % 2 else "exclude"


def make(tmp_path, config=None, *, name="w.sqlite", max_requests=100000, optimizer=None):
    config = config or IdeaScreeningConfig(enabled=True)
    model = RubricModel()
    wheel = DecisionFlywheel(tmp_path / name, ClassifierConfig(TASK, rubric="base"), model, optimizer or Fake(),
                             max_requests=max_requests, idea_screening=config)
    return wheel, model


def propose(wheel, rubrics, cycle=1):
    ControlScheduler(wheel).admit_proposals("rubric", [{"rationale": f"try {r}", "rubric": r} for r in rubrics],
                                            TRAIN, cycle)
    ShadowEvaluator(wheel).fill_slots(TRAIN)   # what the rubric stage does right after admitting proposals


async def drive(wheel, start, stop, *, protected=frozenset(), unlabeled=frozenset(), on_story=None):
    """The replay's order of operations: predict, shadow-score before the label, record, then review."""
    ev = ShadowEvaluator(wheel)
    for i in range(start, stop):
        story = item(i)
        result = await wheel.predict(story, TRAIN)
        eligible = story.id not in protected and story.id not in unlabeled
        cycle = await ev.score_cycle(story, TRAIN, eligible=eligible, incumbent_choice=result.label,
                                     incumbent_confidence=result.probabilities[result.label],
                                     protected_ids=protected, reserve=stop - i)
        if eligible:
            ev.record(cycle, truth(i))
        if cycle is not None:
            await ev.review(TRAIN, (), source="test")
        if on_story:
            on_story(i, ev)
    return ev


def run(coro):
    return asyncio.run(coro)


def kinds(wheel):
    return [e["kind"] for e in wheel.history(1000000)]


def shadow_rows(wheel, idea_id):
    return wheel.db.execute("SELECT item_id,correct,choice FROM idea_evaluations WHERE idea_id=? AND "
                            "context_version='shadow' ORDER BY item_id", (idea_id,)).fetchall()


def idea_id_for(wheel, rubric):
    return next(i["id"] for i in ControlScheduler(wheel).ideas() if i["proposal"].get("rubric") == rubric)


# (a) off by default ----------------------------------------------------------------------------------------

def test_default_off_and_dev_screen_mode_leave_the_shadow_machinery_untouched(tmp_path):
    for config in (IdeaScreeningConfig(), IdeaScreeningConfig(enabled=True, mode="dev_screen")):
        wheel, model = make(tmp_path, config, name=f"{config.mode}{config.enabled}.sqlite")
        assert not wheel.shadow_enabled()
        plan = plan_replay(TASK, replay_rows(60), seed="off", batch_size=20)
        report = run(run_cycle_replay(wheel, plan, optimize_every=10 ** 6, retrain_every=10 ** 6, stages=("rubric",),
                                      rubric_changes_every=None))
        assert len(report["cycles"]) == 60 and "shadow" not in report
        assert not wheel.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'shadow%'").fetchall()
        assert "portfolio-scored" not in kinds(wheel)
        assert {rubric for rubric, _ in model.calls} == {"base"}
        wheel.close()
    assert IdeaScreeningConfig(enabled=True).mode == "shadow" and not IdeaScreeningConfig().enabled


def test_config_is_validated_and_a_stage_two_config_without_a_mode_stays_dev_screen():
    for bad in ({"mode": "both"}, {"shadow_ideas": 0}, {"shadow_min_items": True}, {"shadow_evict_after": 0},
                {"shadow_looks": -1}, {"max_shadow_calls": 1.5}):
        with pytest.raises(ValueError):
            IdeaScreeningConfig(**bad)
    config = IdeaScreeningConfig(enabled=True, shadow_ideas=2, shadow_min_items=30, shadow_evict_after=20,
                                 shadow_looks=5, max_shadow_calls=100)
    assert IdeaScreeningConfig.from_json(config.as_json()) == config
    old = {k: v for k, v in config.as_json().items() if not k.startswith("shadow") and k != "mode"
           and k != "max_shadow_calls"}
    assert IdeaScreeningConfig.from_json(old).mode == "dev_screen"


# (b) forward only ------------------------------------------------------------------------------------------

def test_an_idea_is_never_scored_on_the_story_it_was_created_in_or_any_earlier_one(tmp_path):
    wheel, model = make(tmp_path)
    run(drive(wheel, 0, 40))
    propose(wheel, ["better", "worse"])
    created = ShadowEvaluator(wheel).seq()
    assert created == 40
    ShadowEvaluator(wheel).fill_slots(TRAIN)
    run(drive(wheel, 40, 90))
    scored = {(rubric, int(story[1:])) for rubric, story in model.calls if rubric != "base"}
    assert scored and min(i for _, i in scored) == OFFSET + 40  # the first story after creation (seq 41)
    assert all(i >= OFFSET + 40 for _, i in scored)
    for rubric in ("better", "worse"):
        rows = shadow_rows(wheel, idea_id_for(wheel, rubric))
        assert rows and all(int(story[1:]) >= OFFSET + 40 for story, _, _ in rows)
    # the call log table agrees: every scoring happened at a sequence number after the idea joined
    bad = wheel.db.execute("SELECT count(*) FROM shadow_scored s JOIN shadow_set m USING (idea_id) "
                           "WHERE s.seq <= m.joined_seq").fetchone()[0]
    assert bad == 0
    wheel.close()


def test_scoring_before_the_idea_exists_is_impossible_even_if_the_slot_is_forced(tmp_path):
    wheel, model = make(tmp_path)
    propose(wheel, ["better"])
    ev = ShadowEvaluator(wheel)
    ev.fill_slots(TRAIN)
    joined = ev.active()[0]["joined_seq"]
    # a story at or before the join sequence is skipped by the guard
    wheel.db.execute("UPDATE shadow_set SET joined_seq=5")
    wheel.db.commit()
    run(drive(wheel, 0, 6))
    assert all(rubric == "base" or int(story[1:]) >= OFFSET + 5 for rubric, story in model.calls)
    assert joined == 0
    wheel.close()


# (c) a better idea is promoted ---------------------------------------------------------------------------

def test_a_better_idea_is_promoted_on_forward_evidence_with_a_verdict_and_ledger_rows(tmp_path):
    wheel, model = make(tmp_path)
    run(drive(wheel, 0, 10))
    propose(wheel, ["better", "worse", "same"])
    promoted_at = []

    def watch(i, ev):
        if not promoted_at and wheel.active.config.rubric == "better":
            promoted_at.append(i)
    run(drive(wheel, 10, 700, on_story=watch))
    assert wheel.active.config.rubric == "better"
    forward = promoted_at[0] - 10 + 1
    assert 40 <= forward <= 450, forward   # bounded number of forward stories
    better = idea_id_for(wheel, "better")
    ledger = HypothesisLedger(wheel.db)
    verdicts = ledger.verdicts(better)
    assert [v["verdict"] for v in verdicts] == ["advanced", "promoted"]
    evidence = verdicts[-1]["evidence"]
    assert evidence["gained"] > evidence["lost"] and evidence["p_value"] < evidence["threshold"] == .005
    assert evidence["shared"] >= 40 and evidence["looks_allowed"] == 10
    assert shadow_rows(wheel, better)
    events = kinds(wheel)
    assert "idea-promoted-by-shadow" in events and "promoted" in events and events.count("classifier-activated") == 1
    promoted = next(e for e in wheel.history(1000000) if e["kind"] == "promoted")
    assert promoted["promotion_path"] == "shadow-evaluation" and promoted["idea_id"] == better
    # the idea record shows the promotion and the old incumbent's rows are still there
    saved = next(i for i in ControlScheduler(wheel).ideas() if i["id"] == better)
    assert saved["attempts"][-1]["result"]["promotion_path"] == "shadow-evaluation"
    old_incumbent = [i for (i,) in wheel.db.execute("SELECT incumbent_id FROM shadow_incumbents")]
    assert len(old_incumbent) >= 1 and shadow_rows(wheel, old_incumbent[0])
    wheel.close()


def test_after_a_promotion_old_evidence_versus_the_old_incumbent_is_never_reused(tmp_path):
    wheel, _ = make(tmp_path, IdeaScreeningConfig(enabled=True, shadow_ideas=3))
    run(drive(wheel, 0, 5))
    propose(wheel, ["better", "same", "same too"])
    run(drive(wheel, 5, 700))
    assert wheel.active.config.rubric == "better"
    ev = ShadowEvaluator(wheel)
    from .idea_screening import incumbent_id
    new_inc = incumbent_id(wheel)
    old_ids = [i for (i,) in wheel.db.execute("SELECT incumbent_id FROM shadow_incumbents") if i != new_inc]
    assert old_ids
    ledger = HypothesisLedger(wheel.db)
    for member in ev.active():
        pair = ledger.paired_comparison(member["idea_id"], new_inc, context_version="shadow")
        # shared stories were all scored while the new incumbent was active
        first = wheel.db.execute("SELECT first_seq FROM shadow_incumbents WHERE incumbent_id=?", (new_inc,)).fetchone()
        if first:
            seqs = dict(wheel.db.execute("SELECT item_id,seq FROM shadow_scored WHERE idea_id=?", (member["idea_id"],)))
            shared = ledger.paired_comparison(member["idea_id"], new_inc, context_version="shadow")["shared"]
            assert shared <= sum(1 for s in seqs.values() if s >= first[0])
    wheel.close()


# (d) equal ideas ----------------------------------------------------------------------------------------

def test_equal_ideas_are_not_promoted_in_the_real_loop(tmp_path):
    wheel, _ = make(tmp_path)
    run(drive(wheel, 0, 5))
    propose(wheel, ["same", "same too"])
    run(drive(wheel, 5, 500))
    assert wheel.active.config.rubric == "base" and "idea-promoted-by-shadow" not in kinds(wheel)
    assert not [e for e in wheel.history(1000000) if e["kind"] == "shadow-look" and e["cleared"]]
    wheel.close()


def simulate(gain_p, lose_p, runs, *, alpha=.05, looks=10, min_items=40, evict_after=30, seed=1):
    """The module's own decision rule applied to simulated paired outcomes, one shared item at a time."""
    rng = random.Random(seed)
    promoted, at = 0, []
    for _ in range(runs):
        gained = lost = 0
        for n in range(1, looks * min_items + 1):
            r = rng.random()
            gained += r < gain_p
            lost += gain_p <= r < gain_p + lose_p
            if n >= evict_after and gained <= lost:
                break
            if n % min_items == 0 and clears_look(gained, lost, alpha, looks):
                promoted += 1
                at.append(n)
                break
    return promoted / runs, at


def test_false_promotion_rate_stays_below_alpha_over_many_runs_and_looks_and_power_is_reported(capsys):
    runs = 3000
    rate, _ = simulate(.10, .10, runs)
    rate_wide, _ = simulate(.25, .25, runs, seed=2)
    rate_without_spending = None
    # the same equal ideas judged at alpha per look (no spending) over 10 looks would exceed alpha: this is why
    # the threshold is alpha / looks
    rng, naive = random.Random(3), 0
    for _ in range(runs):
        gained = lost = 0
        for n in range(1, 401):
            r = rng.random()
            gained += r < .10
            lost += .10 <= r < .20
            if n % 40 == 0 and gained > lost and sign_test(gained, lost) < .05:
                naive += 1
                break
    rate_without_spending = naive / runs
    power, at = simulate(.1375, .0375, 1000, seed=4)
    median = sorted(at)[len(at) // 2] if at else None
    print(f"OBSERVED false-promotion rate (equal ideas, {runs} runs, 10 looks): {rate:.4f} / {rate_wide:.4f}; "
          f"without spending: {rate_without_spending:.4f}; power at +10 points: {power:.3f}, median n {median}")
    assert rate <= .05 and rate_wide <= .05
    assert rate_without_spending > rate
    assert power > .75   # a real +10 point idea is found within 400 forward stories most of the time


# (e) eviction --------------------------------------------------------------------------------------------

def test_an_idea_with_no_advantage_is_evicted_after_the_configured_count_and_rows_are_kept(tmp_path):
    wheel, _ = make(tmp_path, IdeaScreeningConfig(enabled=True, shadow_ideas=2, shadow_evict_after=30))
    propose(wheel, ["worse", "same", "better"])
    ev = ShadowEvaluator(wheel)
    ev.fill_slots(TRAIN)
    assert len(ev.active()) == 2
    waiting = {i["id"] for i in ControlScheduler(wheel).ideas()} - {m["idea_id"] for m in ev.active()}
    assert len(waiting) == 1

    states = {}

    def watch(i, ev):
        states[i + 1] = {m["idea_id"] for m in ev.active()}
    run(drive(wheel, 0, 80, on_story=watch))
    worse = idea_id_for(wheel, "worse")
    ledger = HypothesisLedger(wheel.db)
    last = ledger.verdicts(worse)[-1]
    assert last["verdict"] == "screened_out" and "no advantage" in last["reason"]
    assert last["evidence"]["shared"] >= 30 and last["evidence"]["gained"] <= last["evidence"]["lost"]
    # still holding the slot before the count, gone once the count was reached
    assert all(worse in states[n] for n in range(2, 29))
    evicted_at = min(n for n in states if worse not in states[n])
    assert 29 <= evicted_at <= 40
    assert len(shadow_rows(wheel, worse)) >= 29            # ledger rows are never deleted
    assert any(e["kind"] == "shadow-idea-evicted" and e["idea_id"] == worse for e in wheel.history(1000000))
    # the freed slots went to the waiting idea
    assert set(waiting) <= {m["idea_id"] for m in ev.active()} | {
        i for (i,) in wheel.db.execute("SELECT idea_id FROM shadow_set WHERE status IN ('evicted','promoted')")}
    wheel.close()


def test_ideas_that_have_not_reached_the_count_keep_their_slot(tmp_path):
    wheel, _ = make(tmp_path, IdeaScreeningConfig(enabled=True, shadow_ideas=2, shadow_evict_after=30))
    propose(wheel, ["worse", "same"])
    run(drive(wheel, 0, 25))
    assert len(ShadowEvaluator(wheel).active()) == 2
    wheel.close()


# (f) protected partition ------------------------------------------------------------------------------

def test_protected_and_unlabeled_stories_are_never_scored_by_shadow_ideas(tmp_path):
    wheel, model = make(tmp_path)
    propose(wheel, ["better", "same"])
    ShadowEvaluator(wheel).fill_slots(TRAIN)
    protected = frozenset(sid(i) for i in range(0, 120, 3))
    unlabeled = frozenset(sid(i) for i in range(1, 120, 5)) - protected
    run(drive(wheel, 0, 120, protected=protected, unlabeled=unlabeled))
    off_limits = protected | unlabeled
    assert not [(r, s) for r, s in model.calls if r not in ("base", wheel.active.config.rubric) and s in off_limits]
    for (idea,) in wheel.db.execute("SELECT DISTINCT idea_id FROM idea_evaluations"):
        assert not {s for s, _, _ in shadow_rows(wheel, idea)} & off_limits
    assert not {s for (s,) in wheel.db.execute("SELECT item_id FROM shadow_scored")} & off_limits
    wheel.close()


def replay_rows(count):
    return tuple(LabeledItem(item(i), truth(i), context={"human_feedback": f"c{i}"})
                 for i in range(count))


def test_a_replay_never_scores_scoreboard_stories_with_shadow_ideas(tmp_path):
    wheel, model = make(tmp_path, IdeaScreeningConfig(enabled=True, shadow_min_items=10, shadow_evict_after=8))
    plan = plan_replay(TASK, replay_rows(150), seed="board", batch_size=30)
    scoreboard = {row.item.id for row in plan.scoreboard}
    assert scoreboard
    report = run(run_cycle_replay(wheel, plan, optimize_every=10 ** 6, retrain_every=10 ** 6, stages=("rubric",),
                                  rubric_changes_every=10, rubric_trigger_basis="revealed_feedback_count",
                                  min_evaluation_per_class=2))
    assert len(report["cycles"]) == 150 and report["shadow"]["calls"] > 0
    # (normal decisions use the incumbent's rubric, which may be the promoted one; shadow calls use the others)
    assert not [(r, s) for r, s in model.calls if r not in ("base", wheel.active.config.rubric) and s in scoreboard]
    assert not {s for (s,) in wheel.db.execute("SELECT item_id FROM shadow_scored")} & scoreboard
    for (idea,) in wheel.db.execute("SELECT DISTINCT idea_id FROM idea_evaluations"):
        assert not {s for s, _, _ in shadow_rows(wheel, idea)} & scoreboard
    # one portfolio event per scored story, with no label in it
    scored = [e for e in wheel.history(1000000) if e["kind"] == "portfolio-scored"]
    assert scored and all(set(e) >= {"item_id", "incumbent", "ideas"} and "label" not in e for e in scored)
    assert all({"choice", "confidence"} <= set(e["incumbent"]) for e in scored)
    wheel.close()


# (g) budgets -------------------------------------------------------------------------------------------

def test_the_request_ceiling_stops_shadow_scoring_cleanly_and_normal_decisions_continue(tmp_path):
    wheel, model = make(tmp_path, IdeaScreeningConfig(enabled=True, shadow_ideas=3), max_requests=100)
    propose(wheel, ["better", "worse", "same"])
    ShadowEvaluator(wheel).fill_slots(TRAIN)
    run(drive(wheel, 0, 80))
    assert wheel.requests <= 100
    assert sum(1 for rubric, _ in model.calls if rubric == "base") == 80   # every normal decision was made
    stops = [e for e in wheel.history(1000000) if e["kind"] == "shadow-scoring-stopped"]
    assert len(stops) == 1 and "ceiling" in stops[0]["reason"]
    assert ShadowEvaluator(wheel).calls() > 0
    wheel.close()


def test_max_shadow_calls_stops_shadow_scoring_cleanly(tmp_path):
    wheel, model = make(tmp_path, IdeaScreeningConfig(enabled=True, max_shadow_calls=25))
    propose(wheel, ["better", "worse", "same"])
    ShadowEvaluator(wheel).fill_slots(TRAIN)
    run(drive(wheel, 0, 60))
    assert ShadowEvaluator(wheel).calls() == 25
    assert sum(1 for rubric, _ in model.calls if rubric != "base") == 25
    stops = [e for e in wheel.history(1000000) if e["kind"] == "shadow-scoring-stopped"]
    assert len(stops) == 1 and "max_shadow_calls" in stops[0]["reason"]
    wheel.close()


def test_a_failing_shadow_call_never_blocks_the_normal_decision(tmp_path):
    wheel, model = make(tmp_path)
    propose(wheel, ["better"])
    ShadowEvaluator(wheel).fill_slots(TRAIN)
    real = model.classify

    async def flaky(config, target, training, **kwargs):
        if config.rubric != "base":
            raise RuntimeError("provider down")
        return await real(config, target, training, **kwargs)
    model.classify = flaky
    run(drive(wheel, 0, 10))
    assert "shadow-scoring-failed" in kinds(wheel)
    assert sum(e["kind"] == "prediction" for e in wheel.history(1000000)) == 10
    wheel.close()


# (h) resume and determinism ---------------------------------------------------------------------------

def ledger_snapshot(wheel):
    rows = wheel.db.execute("SELECT idea_id,item_id,correct,choice,confidence,context_version FROM idea_evaluations "
                            "ORDER BY idea_id,item_id,context_version").fetchall()
    verdicts = wheel.db.execute("SELECT idea_id,verdict,reason,evidence FROM idea_verdicts ORDER BY rowid").fetchall()
    return rows, verdicts, wheel.active.config.rubric


def test_resume_keeps_the_config_the_shadow_set_the_sequence_and_the_ledger(tmp_path):
    config = IdeaScreeningConfig(enabled=True, shadow_ideas=2, shadow_min_items=25, shadow_evict_after=20,
                                 shadow_looks=6, max_shadow_calls=500)
    wheel, _ = make(tmp_path, config)
    propose(wheel, ["better", "same"])
    ShadowEvaluator(wheel).fill_slots(TRAIN)
    run(drive(wheel, 0, 30))
    before = (ledger_snapshot(wheel), ShadowEvaluator(wheel).snapshot())
    wheel.close()
    reopened = DecisionFlywheel(tmp_path / "w.sqlite", ClassifierConfig(TASK, rubric="base"), RubricModel(), Fake(),
                                max_requests=100000)
    assert reopened.idea_screening == config and reopened.shadow_enabled()
    assert (ledger_snapshot(reopened), ShadowEvaluator(reopened).snapshot()) == before
    run(drive(reopened, 30, 40))
    assert ShadowEvaluator(reopened).seq() == 40
    assert len(shadow_rows(reopened, idea_id_for(reopened, "better"))) > before[0][0].__len__() // 4
    reopened.close()


def test_same_inputs_give_identical_ledgers(tmp_path):
    snapshots = []
    for name in ("one.sqlite", "two.sqlite"):
        wheel, _ = make(tmp_path, name=name)
        run(drive(wheel, 0, 5))
        propose(wheel, ["better", "worse", "same"])
        run(drive(wheel, 5, 160))
        snapshots.append(ledger_snapshot(wheel))
        wheel.close()
    assert snapshots[0] == snapshots[1] and snapshots[0][0]


# optimizer context --------------------------------------------------------------------------------------

def test_the_optimizer_digest_carries_forward_records_and_only_revealed_stories(tmp_path):
    wheel, _ = make(tmp_path, IdeaScreeningConfig(enabled=True, shadow_ideas=3))
    propose(wheel, ["better", "worse", "same"])
    ShadowEvaluator(wheel).fill_slots(TRAIN)
    run(drive(wheel, 0, 50, unlabeled=frozenset({sid(7), sid(8)})))
    digest = ShadowEvaluator(wheel).optimizer_digest()
    rows = digest["hypothesis_ledger"]
    assert rows and len(rows) <= 10
    for row in rows:
        assert {"items_tested", "accuracy", "fixed_count", "broke_count", "last_verdict", "shadow_status"} <= set(row)
        assert len(row["fixed"]) <= 5 and len(row["broke"]) <= 5
        assert not {sid(7), sid(8)} & set(row["fixed"] + row["broke"])
    assert all(s not in (sid(7), sid(8)) for s in digest["stubborn_items"]) and len(digest["stubborn_items"]) <= 20
    from .idea_screening import optimizer_context
    context = optimizer_context(wheel, HypothesisLedger(wheel.db), list(ControlScheduler(wheel).ideas()), set())
    assert context["hypothesis_ledger"] == rows and context["prior_control_ideas"]
    wheel.close()


# (i) regression: a fresh replay runtime with shadow screening configured ------------------------------------

def test_a_fresh_replay_runtime_with_shadow_screening_starts_and_completes_through_the_study_code_path(tmp_path):
    wheel, model = make(tmp_path)
    assert [e["kind"] for e in wheel.history(10)] == ["idea-screening-configured"]
    plan = plan_replay(TASK, replay_rows(200), seed="regression", batch_size=40)
    report = run(run_cycle_replay(wheel, plan, optimize_every=10 ** 6, retrain_every=10 ** 6, stages=("rubric",),
                                  rubric_changes_every=12, rubric_trigger_basis="revealed_feedback_count",
                                  min_evaluation_per_class=2))
    assert len(report["cycles"]) == 200 and "stopped_reason" not in report
    assert report["shadow"]["calls"] > 0 and "portfolio-scored" in kinds(wheel)
    assert wheel.requests == model.calls.__len__()   # wheel.requests stays truthful about every real call
    wheel.close()


def test_the_study_script_exposes_and_records_the_shadow_flags(capsys):
    import importlib.util
    import pathlib
    from argparse import Namespace
    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "run_editorial_prequential_study.py"
    spec = importlib.util.spec_from_file_location("study_script_under_test", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as error:  # optional provider packages absent
        pytest.skip(str(error))
    with pytest.raises(SystemExit):
        module.main(["--help"])
    help_text = capsys.readouterr().out
    for flag in ("--screening-mode", "--shadow-ideas", "--shadow-min-items", "--shadow-evict-after",
                 "--shadow-looks", "--max-shadow-calls"):
        assert flag in help_text
    args = Namespace(idea_screening=True, screening_max_candidates=8, screening_proposals=3, screening_finalists=2,
                     screening_reserve=.2, screening_alpha=.05, screening_max_calls=2000, screening_mode="shadow",
                     shadow_ideas=2, shadow_min_items=50, shadow_evict_after=25, shadow_looks=8, max_shadow_calls=900)
    config = module.idea_screening_config(args)
    assert (config.mode, config.shadow_ideas, config.shadow_min_items, config.shadow_evict_after,
            config.shadow_looks, config.max_shadow_calls) == ("shadow", 2, 50, 25, 8, 900)
    assert config.as_json()["shadow_looks"] == 8   # what protocol.json records
