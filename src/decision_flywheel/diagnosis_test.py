"""Root-cause diagnosis and element screening (Stage 2d): fake optimizers and a fake decision model only, no network."""
import asyncio
import json
import math
import random

import pytest

from .classifier_config import ClassifiedAnswers, ClassifierConfig
from .control_scheduler import ControlScheduler
from .cycle_replay import run_cycle_replay
from .diagnosis import (cause_trial, choose_elements, deal, draw_samples, element_metrics, error_summary,
                        passes_trial, recorded_predictions, run_diagnosis, support, validate_addresses,
                        validate_diagnosis, validate_reconcile, visible_rows, diagnosis_digest, Asker, JevMeter)
from .element_stats import auc, cv_gain, pearson
from .flywheel import DecisionFlywheel
from .hypothesis_ledger import HypothesisLedger
from .idea_screening import DiagnosisConfig, IdeaScreeningConfig, optimizer_context
from .models import DecisionResult, Item, LabeledItem
from .optimizer_agent import FeedbackBriefing, OptimizerAgent, OptimizerReply
from .proposal_operators import ask_round
from .proposal_operators_test import Operators, builder
from .replay import plan_replay
from .shadow_evaluation_test import TASK, TRAIN, draw, item, replay_rows, run, sid, truth

FAST = dict(samples=8, contrast=4, min_cell=5, element_sample=60)


def screening(**diagnosis):
    return IdeaScreeningConfig(enabled=True, diagnosis=DiagnosisConfig(enabled=True, **{**FAST, **diagnosis}))


def events(wheel, kind):
    return [e for e in wheel.history(1000000) if e["kind"] == kind]


# -- the fake decision model ---------------------------------------------------------------------------------------------

def sigmoid(z):
    return 1 / (1 + math.exp(-z))


class ElementModel:
    """include/exclude by parity. About 40 percent of includes are 'hard': the base rubric calls them exclude. A rubric
    containing PATCH_OK fixes them (and breaks a few excludes); PATCH_BAD fixes nothing and breaks many excludes.
    Element 'el_hard_flag' tracks the truth with its own noise (information the main decision lacks); every other
    element is independent noise."""
    model_identity = "element-model"

    def __init__(self):
        self.calls = []

    async def classify(self, config, target, training, *, now=None, event_sink=None):
        self.calls.append((config.rubric, target.id, len(config.tasks)))
        include = int(target.id[1:]) % 2 == 1
        hard = include and draw(target.id, "hard") < .4
        logit = (.8 if include else -.8) + (draw(target.id, "n") - .5) * 2.4
        if hard and "PATCH_OK" not in config.rubric:
            logit -= 2.5
        if "PATCH_OK" in config.rubric and not include and draw(target.id, "b") < .1:
            logit += 2.5
        if "PATCH_BAD" in config.rubric and not include and draw(target.id, "bb") < .6:
            logit += 3
        p = sigmoid(logit)
        label = "include" if p >= .5 else "exclude"
        answers = {"decision": DecisionResult(label, {"include": p, "exclude": 1 - p})}
        for task in config.tasks:
            q = ((.8 if include else .2) + (draw(target.id, "e") - .5) * .3) if task.name == "el_hard_flag" \
                else draw(target.id, task.name)
            answers[task.name] = DecisionResult("yes" if q >= .5 else "no", {"yes": q, "no": 1 - q})
        return ClassifiedAnswers(answers, "element-model", {"input_tokens": 1}, 1)


# -- a scripted diagnosing optimizer -------------------------------------------------------------------------------------

def default_independent(cur):
    ids = [e["id"] for e in cur["errors"]]
    return {"hypotheses": [{"name": "hard includes", "description": "stories phrased as hard-won achievements",
                            "proposed_rule": "include hard-won achievements", "evidence_story_ids": ids[:2]}]}


def default_reconcile(cur):
    cell = "include->exclude" if "include->exclude" in cur["cell_keys"] else cur["cell_keys"][0]
    block = next(c for c in cur["cells"] if c["cell"]["key"] == cell)
    both = [d[0]["evidence_story_ids"][0] for d in block["diagnoses"]]
    keep = [s["id"] for s in cur["contrast_seen"]][:1]
    cause = {"id": "c1", "name": "hard includes", "cell": cell, "description": "hard-won achievements are missed",
             "evidence_story_ids": both, "proposed_rule": "include hard-won achievements",
             "patch": "PATCH_OK include hard-won achievements.", "must_keep_story_ids": keep, "confirmed": True,
             "candidate_element": {"name": "hard_flag", "question": "Is this a hard-won achievement?"}}
    bad = {**cause, "id": "c2", "name": "bad idea", "patch": "PATCH_BAD include everything.", "confirmed": False,
           "candidate_element": None, "evidence_story_ids": both[:1]}
    return {"causes": [cause, bad], "generic_elements": [{"name": "noise_a", "question": "Is the sky blue?"}]}


class Scripted:
    def __init__(self, *, ceiling=None, independent=default_independent, reconcile=default_reconcile, burn=0):
        self.ceiling, self.independent, self.reconcile, self.burn = ceiling, independent, reconcile, burn
        self.seen, self.observer = [], lambda event: None

    def request_messages(self, briefing):
        return []

    def calls_remaining(self):
        return None if self.ceiling is None else self.ceiling - len(self.seen) * (1 + self.burn)

    def diagnose(self, briefing):
        current = briefing.payload["current"]
        self.seen.append(current)
        return (self.independent if current["diagnosis_phase"] == "independent" else self.reconcile)(current)


# -- fixtures ----------------------------------------------------------------------------------------------------------

def rows_for(start, stop):
    return tuple(LabeledItem(item(i), truth(i), context={"human_feedback": f"editor note {i}"}) for i in range(start, stop))


async def prepared(tmp_path, optimizer=None, config=None, *, train=120, dev=20, protected=20, name="w.sqlite", max_requests=100000):
    model = ElementModel()
    wheel = DecisionFlywheel(tmp_path / name, ClassifierConfig(TASK, rubric="base"), model, optimizer or Scripted(),
                             max_requests=max_requests, idea_screening=config or screening())
    training, development = rows_for(0, train), rows_for(train, train + dev)
    held = tuple(item(i) for i in range(train + dev, train + dev + protected))
    # a training row whose text equals a development story's text must stay hidden from every prompt
    twin = LabeledItem(Item(TWIN if development[0].label == "include" else TWIN_EVEN, dict(development[0].item.values)),
                       development[0].label,
                       context={"human_feedback": "twin note"})
    training = (*training, twin)
    for row in (*training, *development):
        await wheel.predict(row.item, TRAIN)
    for story in held:
        await wheel.predict(story, TRAIN)
    return wheel, model, training, development, held


TWIN, TWIN_EVEN = "s5001", "s5000"   # same parity (hence label) as the story whose text they copy


# -- 1. error summary --------------------------------------------------------------------------------------------------

def test_the_confusion_summary_counts_cells_directions_and_ignores_predictions_made_after_the_reveal(tmp_path):
    async def go():
        wheel, model, training, development, held = await prepared(tmp_path, train=60, dev=10, protected=4)
        revealed = [*training, *development]
        # a prediction recorded after the story's label was revealed must not count
        wheel._emit({"kind": "human-feedback", "feedback": {"item_id": training[0].item.id}})
        wrong = "include" if training[0].label == "exclude" else "exclude"
        wheel._emit({"kind": "prediction", "target_id": training[0].item.id, "decision_model_label": wrong,
                     "decision_model_probabilities": {"include": .5, "exclude": .5}, "version": wheel.active.fingerprint})
        predictions = recorded_predictions(wheel, {r.item.id for r in revealed})
        assert predictions[training[0].item.id]["label"] == next(
            e for e in events(wheel, "prediction") if e["target_id"] == training[0].item.id)["decision_model_label"]
        summary = error_summary(wheel, revealed, predictions)
        n = len(revealed)
        assert summary["n"] == n and summary["scope"] == "current-version"
        # recompute by hand from the model's rule
        missed = sum(1 for r in revealed if r.label == "include" and predictions[r.item.id]["label"] == "exclude")
        wrongful = sum(1 for r in revealed if r.label == "exclude" and predictions[r.item.id]["label"] == "include")
        cells = {c["key"]: c for c in summary["cells"]}
        assert cells["include->exclude"]["errors"] == missed > 0
        assert cells["exclude->include"]["errors"] == wrongful
        assert summary["confusion"]["include"]["exclude"] == missed
        assert cells["include->exclude"]["direction"] == "missed include"
        assert cells["exclude->include"]["direction"] == "wrongful include"
        assert summary["cells"][0]["errors"] >= summary["cells"][-1]["errors"]
        assert "missed include" in summary["text"] and "vs" in summary["text"]
        assert summary["shares"]["include"]["answered"] < summary["shares"]["include"]["actual"]
        # an old classifier version: too few current-version predictions fall back to every recorded prediction
        other = error_summary(wheel, revealed[:10], {k: {**v, "version": "old"} for k, v in predictions.items()})
        assert other["scope"] == "all-versions" and other["n"] == 10
        assert error_summary(wheel, [], {})["cells"] == []
        wheel.close()
    run(go())


# -- 2. samples ---------------------------------------------------------------------------------------------------------

def test_samples_are_disjoint_deterministic_confident_first_and_training_only(tmp_path):
    async def go():
        wheel, model, training, development, held = await prepared(tmp_path)
        revealed = [*training, *development]
        predictions = recorded_predictions(wheel, {r.item.id for r in revealed})
        summary = error_summary(wheel, revealed, predictions)
        rows = visible_rows(training, development, held, TASK)
        dev_ids = {r.item.id for r in development}
        assert TWIN not in rows and TWIN_EVEN not in rows and not dev_ids & set(rows) and not {h.id for h in held} & set(rows)
        cells = [c for c in summary["cells"] if c["errors"] >= 5]
        a = draw_samples(TASK, rows, predictions, cells, ways=3, size=8, contrast=4, seed=7)
        b = draw_samples(TASK, rows, predictions, cells, ways=3, size=8, contrast=4, seed=7)
        other = draw_samples(TASK, rows, predictions, cells, ways=3, size=8, contrast=4, seed=8)
        ids = lambda s: {cell: [[r["id"] for r in part] for part in parts] for cell, parts in s["errors"].items()}  # noqa: E731
        assert ids(a) == ids(b) and ids(a) != ids(other)
        seen = []
        for cell in cells:
            parts = a["errors"][cell["key"]]
            available = sum(1 for r in rows.values() if (r.label, predictions[r.item.id]["label"]) == (cell["true"], cell["answered"]))
            assert len(parts) == 3 and all(len(p) <= 8 for p in parts) and sum(map(len, parts)) == min(24, available)
            assert max(map(len, parts)) - min(map(len, parts)) <= 1
            for part in parts:
                assert all((r["true"], r["answered"]) == (cell["true"], cell["answered"]) for r in part)
                confidences = [r["confidence"] for r in part]
                assert confidences == sorted(confidences, reverse=True)       # confident-first
                seen += [r["id"] for r in part]
        for sample in a["contrast"]:
            for label, part in sample.items():
                assert len(part) == 4 and all(r["true"] == label == r["answered"] for r in part)
                seen += [r["id"] for r in part]
        assert len(seen) == len(set(seen))                                    # disjoint across samples, cells and labels
        assert set(seen) <= set(rows)                                         # training-role, visible stories only
        # the three samples span the confidence range: each holds one of every consecutive confident triple
        pool = sorted([r["id"] for r in a["errors"]["include->exclude"][0]])
        assert pool and set(pool) <= set(rows)
        wheel.close()
    run(go())


def test_deal_is_deterministic_and_balanced():
    ordered = list(range(30))
    parts = deal(ordered, 3, 8, "seed")
    assert parts == deal(ordered, 3, 8, "seed") and sum(len(p) for p in parts) == 24
    assert sorted(x for p in parts for x in p) == list(range(24))
    for part in parts:
        assert part == sorted(part) and all(any(x // 3 == k for x in part) for k in range(8))
    assert deal(ordered[:5], 3, 8, "seed") != [[], [], []]


# -- 3. strict validation -----------------------------------------------------------------------------------------------

def good_diagnosis(ids=("a1", "a2")):
    return {"hypotheses": [{"name": "h", "description": "d", "proposed_rule": "r", "evidence_story_ids": list(ids)}]}


def good_cause(**changes):
    cause = {"id": "c1", "name": "n", "cell": "include->exclude", "description": "d", "evidence_story_ids": ["a1"],
             "proposed_rule": "r", "patch": "p", "must_keep_story_ids": ["k1"], "confirmed": True,
             "candidate_element": {"name": "flag", "question": "Is it?"}}
    return {**cause, **changes}


KW = dict(errors_by_cell={"include->exclude": {"a1", "a2"}, "exclude->include": {"b1"}}, contrast_ids={"k1", "k2"},
          max_causes=2, max_elements=2)


def test_independent_diagnoses_are_validated_strictly():
    assert validate_diagnosis(good_diagnosis(), {"a1", "a2", "a3"})[0]["evidence_story_ids"] == ["a1", "a2"]
    bad = [good_diagnosis(("zz",)), good_diagnosis(()), good_diagnosis(("a1", "a1")), {"hypotheses": []},
           {"hypotheses": good_diagnosis()["hypotheses"] * 5}, {"hypotheses": good_diagnosis()["hypotheses"] * 2},
           {"hypotheses": [{"name": "h"}]}, {"other": 1}, [], "text",
           {"hypotheses": [{**good_diagnosis()["hypotheses"][0], "name": "x" * 99}]},
           {"hypotheses": [{**good_diagnosis()["hypotheses"][0], "description": ""}]},
           {"hypotheses": [{**good_diagnosis()["hypotheses"][0], "extra": 1}]}]
    for reply in bad:
        with pytest.raises(ValueError):
            validate_diagnosis(reply, {"a1", "a2", "a3"})


def test_reconcile_replies_reject_unknown_ids_wrong_cells_duplicates_and_overlength():
    ok = validate_reconcile({"causes": [good_cause()], "generic_elements": [{"name": "other", "question": "Q?"}]}, **KW)
    assert ok["causes"][0]["candidate_element"]["name"] == "flag" and ok["generic_elements"][0]["name"] == "other"
    assert validate_reconcile({"causes": []}, **KW) == {"causes": [], "generic_elements": []}
    bad_causes = [
        good_cause(evidence_story_ids=["zz"]),                      # id never shown
        good_cause(evidence_story_ids=["b1"]),                      # an error of a DIFFERENT cell
        good_cause(evidence_story_ids=[]),
        good_cause(must_keep_story_ids=["a1"]),                     # an error, not a contrast story
        good_cause(cell="nowhere->else"),
        good_cause(name=""), good_cause(name="x" * 200), good_cause(patch=""), good_cause(patch="p" * 900),
        good_cause(description="d" * 900), good_cause(proposed_rule="r" * 900),
        good_cause(confirmed="yes"), good_cause(id="has space"),
        good_cause(candidate_element={"name": "Bad Name", "question": "Q?"}),
        good_cause(candidate_element={"name": "flag"}),
        {k: v for k, v in good_cause().items() if k != "patch"},    # missing field
        {**good_cause(), "bonus": 1},                               # unknown field
    ]
    for cause in bad_causes:
        with pytest.raises(ValueError):
            validate_reconcile({"causes": [cause]}, **KW)
    with pytest.raises(ValueError):                                 # duplicate ids
        validate_reconcile({"causes": [good_cause(), good_cause(name="m")]}, **KW)
    with pytest.raises(ValueError):                                 # duplicate names, case-insensitive
        validate_reconcile({"causes": [good_cause(), good_cause(id="c2", name="N")]}, **KW)
    with pytest.raises(ValueError):                                 # too many causes
        validate_reconcile({"causes": [good_cause(id=f"c{i}", name=f"n{i}") for i in range(3)]}, **KW)
    with pytest.raises(ValueError):                                 # too many generic elements
        validate_reconcile({"causes": [], "generic_elements": [{"name": f"e{i}", "question": "Q?"} for i in range(3)]}, **KW)
    with pytest.raises(ValueError):                                 # one name, two questions
        validate_reconcile({"causes": [good_cause()], "generic_elements": [{"name": "flag", "question": "Different?"}]}, **KW)
    with pytest.raises(ValueError):
        validate_reconcile({"causes": [], "other": 1}, **KW)
    with pytest.raises(ValueError):
        validate_reconcile({"generic_elements": []}, **KW)


def test_a_bad_reply_is_retried_once_with_the_error_and_the_calls_are_counted(tmp_path):
    async def go():
        replies = [{"hypotheses": []}, good_diagnosis()]

        class Flaky(Scripted):
            def diagnose(self, briefing):
                self.seen.append(briefing.payload["current"])
                return replies.pop(0)
        wheel, *_ = await prepared(tmp_path, Flaky(), train=20, dev=4, protected=2)
        asker = Asker(wheel, (), ())
        current = {"diagnosis_phase": "independent"}
        assert (await asker.ask(current, lambda r: validate_diagnosis(r, {"a1", "a2"})))[0]["name"] == "h"
        assert asker.calls == 2 and "previous_reply_error" in wheel.optimizer.seen[1]
        assert "previous_reply_error" not in wheel.optimizer.seen[0]
        replies[:] = [{"hypotheses": []}, {"hypotheses": []}, good_diagnosis()]
        assert await asker.ask(current, lambda r: validate_diagnosis(r, {"a1", "a2"})) is None   # bounded: one retry only
        assert asker.calls == 4 and len(replies) == 1
        assert len(events(wheel, "diagnosis-reply-rejected")) == 3
        wheel.close()
    run(go())


# -- 4. confirmation and the cause trial -------------------------------------------------------------------------------

def test_a_cause_is_confirmed_only_when_every_independent_diagnosis_supports_it():
    a = [{"evidence_story_ids": ["a1", "a2"]}]
    b = [{"evidence_story_ids": ["b1"]}]
    assert support({"evidence_story_ids": ["a1", "b1"]}, [a, b])
    assert not support({"evidence_story_ids": ["a1"]}, [a, b])          # only one diagnosis cites it
    assert not support({"evidence_story_ids": ["b1"]}, [a, b])
    assert not support({"evidence_story_ids": ["a1", "b1"]}, [a, None])  # an independent diagnosis is missing
    assert not support({"evidence_story_ids": ["a1"]}, [])


def test_the_trial_pass_rule():
    # fixes at least max(2, 25 percent of mismatches) and breaks no more than a third as many as it fixes
    assert passes_trial(2, 0, 8) and passes_trial(3, 1, 8) and passes_trial(2, 0, 4)
    assert not passes_trial(1, 0, 8)            # fewer than 2
    assert not passes_trial(2, 1, 8)            # breaks more than a third of what it fixes
    assert not passes_trial(3, 2, 8)
    assert passes_trial(6, 2, 24) and not passes_trial(5, 0, 24)   # 25 percent of 24 is 6
    assert not passes_trial(0, 0, 0)


def test_cause_trial_counts_fixed_and_broke_on_the_held_aside_sample_with_a_fake_scorer():
    async def go():
        class Wheel:
            class active:
                config = ClassifierConfig(TASK, rubric="base")
            _emit = staticmethod(lambda e: None)
        errors = [{"row": LabeledItem(item(i), "include"), "true": "include"} for i in range(0, 8)]
        contrast = [{"row": LabeledItem(item(i), "exclude" if i % 2 == 0 else "include"),
                     "true": "exclude" if i % 2 == 0 else "include"} for i in range(10, 18)]
        meter = type("M", (), {"used": 0, "left": 99})()
        fixed_ids = {sid(i) for i in (0, 1, 2)}
        broken_ids = {sid(10)}

        async def scorer(row):
            if row.item.id in fixed_ids:
                return "include"
            if row.item.id in broken_ids:
                return "include"
            return row.label if row.item.id not in {sid(i) for i in range(8)} else "exclude"
        result = await cause_trial(Wheel, {"patch": "do it"}, errors, contrast, TRAIN, None, meter, scorer)
        assert (result["fixed"], result["broke"], result["n_errors"], result["n_contrast"]) == (3, 1, 8, 8)
        assert result["fix_rate"] == .375 and result["break_rate"] == .125
        assert result["passed"] is True and result["status"] == "complete"   # 3 >= 2, 1*3 <= 3
        broken_ids |= {sid(12)}
        assert (await cause_trial(Wheel, {"patch": "do it"}, errors, contrast, TRAIN, None, meter, scorer))["passed"] is False
        few = await cause_trial(Wheel, {"patch": "x"}, errors[:1], contrast, TRAIN, None, meter, scorer)
        assert few["passed"] is False and few["status"].startswith("untestable")
    run(go())


# -- 5. element statistics -----------------------------------------------------------------------------------------------

def test_auc_matches_hand_computation():
    assert auc([.1, .4, .35, .8], [False, False, True, True]) == .75      # the textbook case: 3 of 4 pairs ordered
    assert auc([1, 1], [True, False]) == .5                                # a tie counts half
    assert auc([.2, .9], [True, False]) == 0.0 and auc([.9, .2], [True, False]) == 1.0
    assert auc([.5, .6], [True, True]) is None
    assert pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0) and pearson([1, 1, 1], [1, 2, 3]) is None


def test_cv_gain_on_a_tiny_case_matches_hand_computation():
    # 20 stories, 10 positive, the main score carries nothing (P = 0.5) and the element is the label itself.
    y = [1] * 10 + [0] * 10
    out = cv_gain([.5] * 20, [float(t) for t in y], y, k=5, repeats=1, seed=1, bootstrap=50)
    # Stratified folds leave every training set balanced, so the base model's intercept is exactly 0: p = 0.5 for
    # every story, log-loss ln 2 and (ties predict positive) accuracy 10/20.
    assert out["base_log_loss"] == pytest.approx(math.log(2), abs=1e-9) and out["base_accuracy"] == pytest.approx(.5)
    assert out["augmented_accuracy"] == pytest.approx(1.0) and out["mean_accuracy_gain"] == pytest.approx(.5)
    assert out["mean_log_loss_gain"] == pytest.approx(math.log(2) - out["augmented_log_loss"], abs=1e-9)
    assert out["augmented_log_loss"] < .2 and out["lower_log_loss_gain"] > .5
    assert cv_gain([.5] * 20, [0.] * 20, [1] * 3 + [0] * 17, k=5) is None      # too few positives to cross-validate
    assert out == cv_gain([.5] * 20, [float(t) for t in y], y, k=5, repeats=1, seed=1, bootstrap=50)  # seeded


def synthetic(n=240, seed=3):
    rng = random.Random(seed)
    y, main, informative, noise, duplicate = [], [], [], [], []
    for _ in range(n):
        label = int(rng.random() < .5)
        y.append(label)
        main.append(sigmoid((1 if label else -1) * .5 + rng.gauss(0, 1.2)))
        e = min(1, max(0, (.75 if label else .25) + rng.gauss(0, .2)))
        informative.append(e)
        duplicate.append(min(1, max(0, e + rng.gauss(0, .005))))
        noise.append(rng.random())
    return y, main, {"informative": informative, "noise": noise, "duplicate": duplicate}


def test_the_informative_element_is_kept_the_noise_and_the_duplicate_are_not():
    y, main, scores = synthetic()
    labels = ["include" if t else "exclude" for t in y]
    errors = [(m >= .5) != bool(t) for m, t in zip(main, y)]
    metrics = element_metrics(y, main, labels, errors, scores, seed=5, repeats=3, bootstrap=200)
    assert metrics["informative"]["cv"]["mean_log_loss_gain"] > .02 and metrics["informative"]["cv"]["lower_log_loss_gain"] > 0
    assert metrics["informative"]["auc_label"] > .85 and metrics["noise"]["auc_label"] < .6
    assert metrics["noise"]["cv"]["lower_log_loss_gain"] <= 0                    # no information beyond the main score
    assert metrics["informative"]["auc_error_by_label"]["include"] is not None
    kept, verdicts = choose_elements(metrics, scores)
    assert len(kept) == 1 and kept[0] in ("informative", "duplicate") and "noise" not in kept
    other = "duplicate" if kept[0] == "informative" else "informative"
    assert verdicts[other]["reason"] == "redundant with an already kept element"
    assert abs(verdicts[other]["redundancy"][kept[0]]) > .95 and "not above zero" in verdicts["noise"]["reason"]
    again = element_metrics(y, main, labels, errors, scores, seed=5, repeats=3, bootstrap=200)
    assert again == metrics                                                       # seeded


# -- 6. the round ---------------------------------------------------------------------------------------------------------

def test_a_round_runs_summary_rca_trial_and_element_screening_in_order(tmp_path):
    async def go():
        wheel, model, training, development, held = await prepared(tmp_path)
        optimizer = wheel.optimizer
        before_requests, before_calls = wheel.requests, len(model.calls)
        result = await run_diagnosis(wheel, training, development, held)
        assert result["skipped"] is None
        # optimizer calls: two cells x two independent diagnoses + one reconcile
        phases = [c["diagnosis_phase"] for c in optimizer.seen]
        assert phases == ["independent"] * 4 + ["reconcile"] and result["optimizer_calls"] == 5
        causes = {c["local_id"]: c for c in result["causes"]}
        assert causes["c1"]["id"].endswith(".c1") and causes["c1"]["confirmed"] is True
        assert causes["c2"]["confirmed"] is False
        assert causes["c1"]["trial"]["passed"] is True and causes["c1"]["trial"]["fixed"] >= 2
        assert causes["c2"]["trial"]["passed"] is False and causes["c2"]["trial"]["fixed"] == 0
        # the trial scored the held-aside sample through the wheel: it counts toward the ceilings and the ledger
        spent = wheel.requests - before_requests
        assert result["jev_calls"] == spent == len(model.calls) - before_calls
        assert spent <= wheel.idea_screening.diagnosis.max_jev_calls
        # elements: the informative one is kept, the noise one is not; the active config is untouched
        assert [e["name"] for e in result["elements"]["kept"]] == ["hard_flag"]
        assert [e["name"] for e in result["elements"]["failed"]] == ["noise_a"]
        assert wheel.active.config.tasks == ()
        # every question rode in ONE request per story: elements=2 tasks added to the same decision request
        assert {n for _, _, n in model.calls if n} == {2}
        rows = HypothesisLedger(wheel.db).element_evaluations()
        assert {r["element"]: r["kept"] for r in rows} == {"hard_flag": True, "noise_a": False}
        assert all(r["n"] > 0 and r["auc_label"] is not None for r in rows)
        assert rows[0]["metrics"]["cv"]["lower_log_loss_gain"] is not None
        # kept elements are registered as control ideas (operator element, control tasks)
        ideas = [i for i in ControlScheduler(wheel).ideas() if i.get("operator") == "element"]
        assert len(ideas) == 1 and ideas[0]["control"] == "tasks"
        assert ideas[0]["proposal"]["tasks"][0]["name"] == "el_hard_flag"
        # the summary event carries the accounting
        summary = events(wheel, "diagnosis-summary")[-1]
        assert summary["optimizer_calls"] == 5 and summary["jev_calls"] == spent
        assert "5 optimizer calls" in summary["accounting"] and summary["summary"]["cells"]
        # prompts contain only visible training stories: never development, protected or the development twin
        shown = {s["id"] for cur in optimizer.seen for key in ("errors", "contrast", "errors_seen", "contrast_seen")
                 for s in cur.get(key, ())}
        for block in (c for cur in optimizer.seen for c in cur.get("cells", ())):
            shown |= {s["id"] for s in block["errors_seen"]}
        hidden = {r.item.id for r in development} | {h.id for h in held} | {TWIN, TWIN_EVEN}
        assert shown and not shown & hidden
        text = json.dumps(optimizer.seen)
        assert not any(sid_ in text for sid_ in hidden)
        # a second round does not re-screen an element that was already evaluated
        again = await run_diagnosis(wheel, training, development, held)
        assert again["elements"]["skipped"] == "no candidate elements" or not again["elements"]["kept"]
        wheel.close()
    run(go())


def test_a_confirmed_flag_the_evidence_does_not_back_is_downgraded(tmp_path):
    async def go():
        def reconcile(cur):
            reply = default_reconcile(cur)
            # claims confirmation but cites evidence only from the first diagnosis
            block = next(c for c in cur["cells"] if c["cell"]["key"] == reply["causes"][0]["cell"])
            reply["causes"][0]["evidence_story_ids"] = [block["diagnoses"][0][0]["evidence_story_ids"][0]]
            return reply
        wheel, model, training, development, held = await prepared(tmp_path, Scripted(reconcile=reconcile))
        result = await run_diagnosis(wheel, training, development, held)
        first = next(c for c in result["causes"] if c["local_id"] == "c1")
        assert first["confirmed_claimed"] is True and first["confirmed"] is False
        wheel.close()
    run(go())


def test_a_ceiling_that_cannot_cover_the_plan_skips_diagnosis_without_calling_the_optimizer(tmp_path):
    async def go():
        wheel, model, training, development, held = await prepared(tmp_path, Scripted(ceiling=5))   # needs 5 + 1 proposal
        result = await run_diagnosis(wheel, training, development, held)
        assert result["skipped"].startswith("the optimizer-call ceiling leaves 5 calls")
        assert wheel.optimizer.seen == [] and result["optimizer_calls"] == 0 and result["causes"] == []
        assert events(wheel, "diagnosis-skipped")[-1]["reason"] == result["skipped"]
        assert events(wheel, "diagnosis-summary")[-1]["skipped"] == result["skipped"]
        assert result["summary"]["cells"]                       # the error summary is still available to the proposals
        wheel.close()
        wheel, model, training, development, held = await prepared(tmp_path, Scripted(ceiling=6), name="exact.sqlite")
        result = await run_diagnosis(wheel, training, development, held)
        assert result["skipped"] is None and len(wheel.optimizer.seen) == 5   # one call is still left for the proposals
        wheel.close()
        # the ceiling shrinks during the round (each call burns three): stop before taking the proposal call's share
        wheel, model, training, development, held = await prepared(tmp_path, Scripted(ceiling=9, burn=1), name="burn.sqlite")
        result = await run_diagnosis(wheel, training, development, held)
        assert result["skipped"] == "the optimizer-call ceiling leaves no room for another diagnosis call"
        assert len(wheel.optimizer.seen) * 2 <= 9 - 1
        wheel.close()
    run(go())


def test_the_jev_cap_limits_trial_and_element_scoring(tmp_path):
    async def go():
        wheel, model, training, development, held = await prepared(tmp_path, config=screening(max_jev_calls=10))
        before = wheel.requests
        result = await run_diagnosis(wheel, training, development, held)
        assert wheel.requests - before <= 10 and result["jev_calls"] <= 10
        assert any(c["trial"]["status"].startswith("incomplete") for c in result["causes"])
        assert all(not c["trial"]["passed"] for c in result["causes"] if c["trial"]["status"].startswith("incomplete"))
        assert result["elements"]["skipped"] and "stories were scored" in result["elements"]["skipped"]
        assert events(wheel, "element-screening-skipped")
        # a request ceiling on the wheel also bounds the round (the headroom for normal decisions is kept)
        tight, model2, training2, development2, held2 = await prepared(tmp_path, name="tight.sqlite", max_requests=500)
        tight.max_requests = tight.requests + len(training2) + 2 * len(development2) + 5
        start = tight.requests
        await run_diagnosis(tight, training2, development2, held2)
        assert tight.requests - start <= 5
        wheel.close()
        tight.close()
    run(go())


def test_no_cell_with_enough_errors_skips_with_the_summary_and_no_optimizer_call(tmp_path):
    async def go():
        wheel, model, training, development, held = await prepared(
            tmp_path, config=screening(min_cell=2), train=12, dev=4, protected=2)
        wheel2, *_ = await prepared(tmp_path, config=screening(min_cell=100), name="b.sqlite", train=30, dev=4, protected=2)
        result = await run_diagnosis(wheel2, rows_for(0, 30), rows_for(30, 34), ())
        assert "no error cell" in result["skipped"] and wheel2.optimizer.seen == []
        wheel.close()
        wheel2.close()
    run(go())


def test_the_optimizer_without_diagnose_and_the_default_off_path_do_nothing(tmp_path):
    async def go():
        class Plain:
            observer = staticmethod(lambda e: None)

            def request_messages(self, b):
                return []
        wheel, model, training, development, held = await prepared(tmp_path, Plain())
        result = await run_diagnosis(wheel, training, development, held)
        assert result["skipped"] == "the optimizer has no diagnose method"
        wheel.close()
        off = DecisionFlywheel(tmp_path / "off.sqlite", ClassifierConfig(TASK, rubric="base"), ElementModel(), Scripted(),
                               max_requests=100, idea_screening=IdeaScreeningConfig(enabled=True))
        assert await run_diagnosis(off, TRAIN, (), ()) is None
        assert off.history(100) and not events(off, "diagnosis-summary")
        off.close()
    run(go())


# -- 7. proposals, addresses and credit assignment -----------------------------------------------------------------------

def fake_diagnosis(passing=("r1.c1", "r1.c2")):
    causes = [{"id": cid, "name": cid, "cell": "include->exclude", "description": "d", "proposed_rule": "r",
               "patch": f"PATCH {cid}", "confirmed": cid == "r1.c1",
               "trial": {"fixed": 3, "broke": 0, "n_errors": 8, "n_contrast": 8, "passed": cid in passing,
                         "status": "complete"}} for cid in ("r1.c1", "r1.c2", "r1.c3")]
    return {"summary": {"text": "SUMMARY TEXT"}, "causes": causes,
            "elements": {"kept": [{"name": "hard_flag", "question": "Q?", "mean_log_loss_gain": .1,
                                   "lower_log_loss_gain": .05}], "failed": [
                {"name": "noise_a", "question": "N?", "mean_log_loss_gain": -.01, "lower_log_loss_gain": -.03}]}}


def test_proposals_see_the_diagnosis_and_their_addresses_are_validated(tmp_path):
    from .shadow_evaluation_test import make
    wheel, _ = make(tmp_path, screening(), optimizer=Operators(
        [{"operator": "bold", "rationale": "a", "rubric": " ".join(f"alpha{k}" for k in range(30)), "addresses": ["r1.c1"]},
         {"operator": "bold", "rationale": "b", "rubric": " ".join(f"beta{k}" for k in range(30)), "addresses": ["r9.zz"]},
         {"operator": "combine", "rationale": "c", "rubric": " ".join(f"gamma{k}" for k in range(30)),
          "addresses": ["r1.c1", "r1.c2"]},
         {"operator": "combine", "rationale": "d", "rubric": " ".join(f"delta{k}" for k in range(30))}]))
    proposals, lineage = asyncio.run(ask_round(wheel, builder(wheel), {}, "rubric", [], TRAIN, (), (), fake_diagnosis()))
    current = wheel.optimizer.briefings[0]
    block = current["diagnosis"]
    assert block["confusion_summary"] == "SUMMARY TEXT" and block["passing_cause_ids"] == ["r1.c1", "r1.c2"]
    assert [c["confirmed"] for c in block["causes"]] == [True, False, False]       # unconfirmed ones are flagged
    assert block["kept_elements"][0]["name"] == "hard_flag" and block["failed_elements"][0]["name"] == "noise_a"
    # no complementary pair of ideas yet, so combine merges the patches of the passing causes
    assert [p["id"] for p in current["operator_requests"]["combine"]["cause_patches"]] == ["r1.c1", "r1.c2"]
    assert len(proposals) == 3 and [m["addresses"] for m in lineage] == [["r1.c1"], ["r1.c1", "r1.c2"], []]
    skipped = [e for e in events(wheel, "proposal-operator-skipped") if "unknown cause" in e["reason"]]
    assert len(skipped) == 1                                                         # the unknown id was dropped, not a crash
    with pytest.raises(ValueError):
        validate_addresses(["x"], {"y"})
    with pytest.raises(ValueError):
        validate_addresses("r1.c1", {"r1.c1"})
    with pytest.raises(ValueError):
        validate_addresses(["r1.c1", "r1.c1"], {"r1.c1"})
    assert validate_addresses(None, {"a"}) == [] and validate_addresses([], {"a"}) == []
    # addresses land in the idea payload, its lineage and the proposal-operator event
    ControlScheduler(wheel).admit_proposals("rubric", proposals, TRAIN, 1, lineage)
    stored = {i["id"]: i for i in ControlScheduler(wheel).ideas()}
    assert sorted(i.get("addresses", []) for i in stored.values()) == [[], ["r1.c1"], ["r1.c1", "r1.c2"]]
    with_addresses = next(i for i in stored.values() if i.get("addresses") == ["r1.c1", "r1.c2"])
    assert HypothesisLedger(wheel.db).lineage(with_addresses["id"])["addresses"] == ["r1.c1", "r1.c2"]
    assert any(e.get("addresses") == ["r1.c1"] for e in events(wheel, "proposal-operator"))
    wheel.close()


def test_cause_outcomes_credit_the_ideas_that_addressed_each_cause(tmp_path):
    from .shadow_evaluation_test import make
    wheel, _ = make(tmp_path, screening())
    ledger = HypothesisLedger(wheel.db)
    ledger.record_cause(1, {"id": "r1.c1", "name": "hard includes", "cell": "include->exclude", "confirmed": True,
                            "trial": {"passed": True}})
    ledger.record_cause(1, {"id": "r1.c2", "name": "other", "cell": "include->exclude", "confirmed": False,
                            "trial": {"passed": False}})
    scheduler = ControlScheduler(wheel)
    for idea_id, addresses, op in (("i1", ["r1.c1"], "bold"), ("i2", ["r1.c1", "r1.c2"], "combine"), ("i3", [], "bold")):
        scheduler.save({"id": idea_id, "control": "rubric", "proposal": {"rubric": idea_id}, "rationale": "",
                        "attempts": [], "last_cycle": 0, "created_cycle": 1, "operator": op, "parents": [],
                        "addresses": addresses})
    ledger.record_verdict("i1", "advanced", "ok", {"gained": 7, "lost": 2, "net": 5})
    ledger.record_verdict("i1", "promoted", "cleared", {"gained": 9, "lost": 2, "net": 7})
    ledger.record_verdict("i2", "screened_out", "no", {"gained": 1, "lost": 4})
    outcomes = ledger.cause_outcomes()
    c1, c2 = outcomes["r1.c1"], outcomes["r1.c2"]
    assert [i["idea_id"] for i in c1["ideas"]] == ["i1", "i2"] and c1["trial_passed"] is True
    assert (c1["ideas"][0]["verdict"], c1["ideas"][0]["gained"], c1["ideas"][0]["lost"]) == ("promoted", 9, 2)
    assert (c1["ideas_addressed"], c1["ideas_advanced"], c1["net_forward"]) == (2, 1, 4)    # (9-2) + (1-4)
    assert (c2["ideas_addressed"], c2["net_forward"]) == (1, -3)
    ledger.record_cause(2, {"id": "r2.c1", "name": "n", "cell": "x", "confirmed": False, "trial": {"passed": False}})
    assert outcomes.keys() < ledger.cause_outcomes().keys()
    # the optimizer digest carries cause outcomes and the screened elements across rounds
    ledger.record_element_evaluation("hard_flag", 1, question="Q?", source="r1.c1", n=100,
                                     metrics={"auc_label": .8, "cv": {"mean_log_loss_gain": .1, "lower_log_loss_gain": .05,
                                                                       "mean_accuracy_gain": .02}}, kept=True, reason="ok")
    ledger.record_element_evaluation("noise_a", 1, question="N?", source="generic", n=100,
                                     metrics={"auc_label": .5, "cv": None}, kept=False, reason="no")
    digest = diagnosis_digest(ledger)
    assert set(digest) == {"cause_outcomes", "elements"} and digest["elements"]["kept"][0]["name"] == "hard_flag"
    assert digest["elements"]["failed"][0]["name"] == "noise_a"
    context = optimizer_context(wheel, ledger, [], frozenset())
    assert "cause_outcomes" in context and "elements" in context
    wheel.close()


# -- 8. configuration -----------------------------------------------------------------------------------------------------

def test_the_default_is_off_and_leaves_the_saved_form_and_behaviour_unchanged(tmp_path):
    config = IdeaScreeningConfig(enabled=True)
    assert config.diagnosis == DiagnosisConfig() and not config.diagnosis.enabled
    assert "diagnosis" not in config.as_json()                                  # the saved form (and stage keys) are unchanged
    assert IdeaScreeningConfig.from_json(config.as_json()) == config
    from .shadow_evaluation_test import make
    wheel, _ = make(tmp_path, config)
    asyncio.run(ask_round(wheel, builder(wheel), {}, "rubric", [], TRAIN, (), ()))
    assert "diagnosis" not in wheel.optimizer.briefings[0]
    assert "cause_outcomes" not in optimizer_context(wheel, HypothesisLedger(wheel.db), [], frozenset())
    wheel.close()


def test_diagnosis_options_validate_persist_and_resume(tmp_path):
    config = screening(samples=6, independent=3, element_candidates=4, max_jev_calls=77)
    assert IdeaScreeningConfig.from_json(config.as_json()) == config and config.as_json()["diagnosis"]["independent"] == 3
    for bad in ({"samples": 1}, {"samples": True}, {"independent": 1}, {"independent": 9}, {"min_cell": 1},
                {"element_candidates": -1}, {"element_sample": 5}, {"max_jev_calls": 0}, {"max_causes": 0}, {"contrast": 0}):
        with pytest.raises(ValueError):
            DiagnosisConfig(enabled=True, **bad)
    with pytest.raises(ValueError):
        DiagnosisConfig(enabled=1)
    with pytest.raises(ValueError):
        IdeaScreeningConfig(diagnosis=DiagnosisConfig(enabled=True))                       # screening itself is off
    with pytest.raises(ValueError):
        IdeaScreeningConfig(enabled=True, mode="dev_screen", diagnosis=DiagnosisConfig(enabled=True))
    with pytest.raises(ValueError):
        IdeaScreeningConfig(enabled=True, diagnosis={"enabled": True})
    first = DecisionFlywheel(tmp_path / "r.sqlite", ClassifierConfig(TASK, rubric="base"), ElementModel(), Scripted(),
                             idea_screening=config)
    assert events(first, "idea-screening-configured")[0]["idea_screening"]["diagnosis"]["max_jev_calls"] == 77
    first.close()
    reopened = DecisionFlywheel(tmp_path / "r.sqlite", ClassifierConfig(TASK, rubric="base"), ElementModel(), Scripted())
    assert reopened.idea_screening == config and reopened.idea_screening.diagnosis.independent == 3
    reopened.close()


def test_the_real_agent_carries_the_diagnosis_instructions_and_counts_its_ceiling():
    seen = []

    def complete(messages):
        seen.append(messages)
        return OptimizerReply(json.dumps(good_diagnosis()), "fake")
    complete.max_calls, complete.calls = 7, 3
    agent = OptimizerAgent(complete)
    assert agent.calls_remaining() == 4 and OptimizerAgent(lambda m: None).calls_remaining() is None
    briefing = FeedbackBriefing.build(TASK, (), protected=(), current={"diagnosis_phase": "independent"})
    assert agent.diagnose(briefing)["hypotheses"][0]["name"] == "h"
    assert "ROOT-CAUSE DIAGNOSIS" in seen[0][0]["content"] and "RECONCILE" not in seen[0][0]["content"]
    reconcile = FeedbackBriefing.build(TASK, (), protected=(), current={"diagnosis_phase": "reconcile"})
    assert "RECONCILE-AND-CRITIQUE" in agent.request_messages(reconcile)[0]["content"]
    with pytest.raises(ValueError):
        agent.request_messages(FeedbackBriefing.build(TASK, (), protected=(), current={"diagnosis_phase": "wild"}))
    operators = FeedbackBriefing.build(TASK, (), protected=(), current={
        "control_under_test": "rubric", "operator_requests": {"bold": {"count": 1}}, "diagnosis": {"x": 1}})
    assert "addresses" in agent.request_messages(operators)[0]["content"]
    plain = FeedbackBriefing.build(TASK, (), protected=(), current={
        "control_under_test": "rubric", "operator_requests": {"bold": {"count": 1}}})
    assert "addresses" not in agent.request_messages(plain)[0]["content"]
    bad = OptimizerAgent(lambda m: OptimizerReply("[1]", "fake"))
    with pytest.raises(ValueError):
        bad.diagnose(briefing)


# -- 9. end to end: the study script path -----------------------------------------------------------------------------------

@pytest.mark.slow
def test_a_fresh_shadow_replay_with_diagnosis_starts_completes_and_diagnoses(tmp_path):
    prompts = []

    def complete(messages):
        current = json.loads(messages[1]["content"])["current"]
        prompts.append(current)
        phase = current.get("diagnosis_phase")
        if phase == "independent":
            return OptimizerReply(json.dumps(default_independent(current)), "fake")
        if phase == "reconcile":
            return OptimizerReply(json.dumps(default_reconcile(current)), "fake")
        if "operator_requests" not in current:
            return OptimizerReply(json.dumps({"rationale": "plain", "rubric": "base"}), "fake")
        causes = [c["id"] for c in current.get("diagnosis", {}).get("causes", ())][:1]
        proposals = []
        for operator, spec in sorted(current["operator_requests"].items()):
            for k in range(spec["count"]):
                n = len(prompts) * 10 + k
                proposals.append({"operator": operator, "rationale": f"{operator}{n}", "addresses": causes,
                                  "rubric": "PATCH_OK " + " ".join(f"w{n}x{j}" for j in range(30))})
        return OptimizerReply(json.dumps({"proposals": proposals}), "fake")
    def counting(messages):
        counting.calls += 1
        return complete(messages)
    counting.max_calls, counting.calls = 400, 0
    model = ElementModel()
    wheel = DecisionFlywheel(tmp_path / "e2e.sqlite", ClassifierConfig(TASK, rubric="base"), model,
                             OptimizerAgent(counting), max_requests=100000,
                             idea_screening=IdeaScreeningConfig(
                                 enabled=True, shadow_min_items=10, shadow_evict_after=8,
                                 proposal_mix={"bold": 2, "target": 2, "combine": 2},
                                 diagnosis=DiagnosisConfig(enabled=True, samples=4, min_cell=3, element_sample=40,
                                                           max_jev_calls=120)))
    plan = plan_replay(TASK, replay_rows(240), seed="mix", batch_size=40)
    report = run(run_cycle_replay(wheel, plan, optimize_every=10 ** 6, retrain_every=10 ** 6, stages=("rubric",),
                                  rubric_changes_every=24, rubric_trigger_basis="revealed_feedback_count",
                                  min_evaluation_per_class=2))
    assert len(report["cycles"]) == 240 and "stopped_reason" not in report
    summaries = events(wheel, "diagnosis-summary")
    assert summaries and any(s["skipped"] is None for s in summaries)
    ran = [s for s in summaries if s["skipped"] is None]
    assert all(s["optimizer_calls"] >= 3 and 0 < s["jev_calls"] <= 120 for s in ran)
    diagnosis_calls = [p for p in prompts if p.get("diagnosis_phase")]
    assert len(diagnosis_calls) == sum(s["optimizer_calls"] for s in summaries)
    proposal_calls = [p for p in prompts if "operator_requests" in p]
    assert any("diagnosis" in p and p["diagnosis"]["causes"] for p in proposal_calls)
    # every optimizer call (diagnosis and proposal) is within the transport's ceiling accounting
    assert counting.calls == len(prompts) <= 400
    # no scoreboard (protected) story or development text reached a diagnosis prompt
    scoreboard = {r.item.id for r in plan.scoreboard}
    development = {r.item.id for r in plan.development}
    text = json.dumps(diagnosis_calls)
    assert not any(f'"{i}"' in text for i in scoreboard | development)
    # addresses reached the ideas
    assert any(i.get("addresses") for i in ControlScheduler(wheel).ideas())
    assert HypothesisLedger(wheel.db).cause_outcomes()
    wheel.close()


def test_the_study_script_parses_records_and_validates_the_diagnosis_flags(capsys):
    import importlib.util
    import pathlib
    from argparse import Namespace
    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "run_editorial_prequential_study.py"
    spec = importlib.util.spec_from_file_location("study_script_diagnosis", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as error:
        pytest.skip(str(error))
    with pytest.raises(SystemExit):
        module.main(["--help"])
    help_text = capsys.readouterr().out
    assert all(flag in help_text for flag in ("--diagnosis", "--diagnosis-samples", "--diagnosis-independent",
                                              "--element-candidates", "--element-sample", "--diagnosis-max-jev-calls"))
    base = dict(idea_screening=True, screening_max_candidates=8, screening_proposals=3, screening_finalists=2,
                screening_reserve=.2, screening_alpha=.05, screening_max_calls=2000, screening_mode="shadow",
                shadow_ideas=4, shadow_min_items=40, shadow_evict_after=30, shadow_looks=10, max_shadow_calls=3000,
                proposal_mix=None, min_novelty=None, target_sample=None)
    flags = dict(diagnosis=True, diagnosis_samples=6, diagnosis_independent=3, element_candidates=5, element_sample=100,
                 diagnosis_max_jev_calls=250)
    recorded = module.idea_screening_config(Namespace(**base, **flags)).as_json()["diagnosis"]
    assert recorded == {"enabled": True, "samples": 6, "contrast": 4, "min_cell": 5, "max_causes": 6, "independent": 3,
                        "element_candidates": 5, "element_sample": 100, "max_jev_calls": 250}
    unset = module.idea_screening_config(Namespace(**base, diagnosis=True, diagnosis_samples=None, diagnosis_independent=None,
                                                   element_candidates=None, element_sample=None, diagnosis_max_jev_calls=None))
    assert unset.diagnosis == DiagnosisConfig(enabled=True)
    off = module.idea_screening_config(Namespace(**base, **{**flags, "diagnosis": False}))
    assert off == IdeaScreeningConfig(enabled=True) and "diagnosis" not in off.as_json()
    for argv in (["--diagnosis"], ["--diagnosis", "--idea-screening", "--screening-mode", "dev_screen"]):
        with pytest.raises(SystemExit):
            module.main(["--operational-corpus", "x", "--operational-sha256", "x", "--bootstrap-corpus", "x",
                         "--bootstrap-sha256", "x", *argv])
        capsys.readouterr()
