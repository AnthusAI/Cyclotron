import asyncio
import hashlib
import json
import warnings

import pytest

from .bundle import (EXAMPLES_FILE, MANIFEST_FILE, CYCLOTRON_FILE, BundleModelWarning,
                     BundleValidationError, ClassifierBundle, load_bundle)
from .context import FixedExampleList
from .models import DecisionResult, DecisionTask, Item, LabeledItem

TASK = DecisionTask("tone", ("pos", "neg"), "Choose the tone.")
ROWS = [LabeledItem(Item(f"{label}-{n}", {"text": f"a {label} sample number {n}"}), label)
        for label in TASK.labels for n in range(4)]
BY_ID = {row.item.id: row for row in ROWS}
FIXED = FixedExampleList.from_items(TASK, [BY_ID[i] for i in ("pos-0", "pos-1", "neg-0", "neg-1")],
                                    [BY_ID["pos-2"], BY_ID["neg-2"]])
CYCLOTRON = json.dumps({"questions": {"tone": {"type": "choice", "criteria": {"pos": None, "neg": None}},
                                      "tone.sport": {"type": "noul"}},
                        "weights": {"tone": 2.0, "tone.sport": 1.0}}, sort_keys=True)
ENGINE = {"adapter": "jev", "configured_model": "fake-1", "model_identity": "jev:fake-1",
          "reported_model": "fake-1"}
TARGETS = [Item("new-a", {"text": "a brand new text to classify"}), Item("new-b", {"text": "another unseen one"}),
           BY_ID["pos-0"].item]


class Rubric:
    """A stand-in for the caller's rubric-and-head payload (in the harness: a Jev-Flywheel Score)."""

    def __init__(self, text):
        raw = json.loads(text)
        self._questions, self.weights = raw["questions"], raw["weights"]

    def questions(self):
        return self._questions

    def predict(self, answers):
        logit = (self.weights["tone"] * (answers["tone"]["probabilities"]["pos"] - 0.5)
                 + self.weights["tone.sport"] * (answers["tone.sport"]["noul"] - 0.5))
        p = 1 / (1 + 2.718281828 ** (-logit))
        label = "pos" if p >= 0.5 else "neg"
        return DecisionResult(label, {"pos": p, "neg": 1 - p}, confidence=max(p, 1 - p))


def _unit(*parts):
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


class Response:
    def __init__(self, answers, model):
        self.answers, self.model = answers, model


class FakeEngine:
    """Answers are a pure function of (state, question), so a request can be replayed exactly."""

    def __init__(self, model="fake-1"):
        self.model, self.requests = model, []

    async def system_one(self, *, state, questions):
        self.requests.append({"state": state, "questions": dict(questions)})
        answers = {}
        for name, question in questions.items():
            u = _unit(state, name)
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": u}
            else:
                answers[name] = {"type": "choice", "choice": "pos" if u >= 0.5 else "neg",
                                 "probabilities": {"pos": u, "neg": 1 - u}}
        return Response(answers, self.model)


def _bundle(examples=FIXED):
    rows = [BY_ID[i] for i in examples.example_ids + examples.reserve_ids] if examples else []
    return ClassifierBundle(TASK, CYCLOTRON, Rubric(CYCLOTRON), examples=examples, example_rows=rows,
                            engine=ENGINE, head={"features": ["tone", "tone.sport"], "fit_id": "f1"},
                            lineage={"round": 3, "labels_fingerprint": "0" * 64})


def _classify(bundle, engine, targets=TARGETS):
    return [asyncio.run(bundle.classify(t, engine)) for t in targets]


def _load(path, **kwargs):
    return load_bundle(path, TASK, Rubric, **kwargs)


def test_a_reloaded_bundle_predicts_exactly_like_the_in_memory_one(tmp_path):
    bundle = _bundle()
    digest = bundle.save(tmp_path / "b")
    reloaded = _load(tmp_path / "b")
    assert reloaded.bundle_hash == digest == bundle.bundle_hash
    first, second = FakeEngine(), FakeEngine()
    assert _classify(bundle, first) == _classify(reloaded, second)
    assert first.requests == second.requests


def test_classify_sends_one_request_with_the_fixed_list_in_the_state_and_every_rubric_question():
    engine = FakeEngine()
    asyncio.run(_bundle().classify(TARGETS[0], engine))
    (request,) = engine.requests
    assert set(request["questions"]) == {"tone", "tone.sport"}
    assert request["state"]["target"] == {"text": TARGETS[0].values["text"]}
    shown = [(e["text"], e["label"]) for e in request["state"]["labeled_examples"]]
    assert shown == [(BY_ID[i].item.values["text"], BY_ID[i].label) for i in FIXED.example_ids]


def test_a_target_that_is_one_of_the_examples_sees_the_reserve_instead_of_itself():
    engine = FakeEngine()
    asyncio.run(_bundle().classify(BY_ID["pos-0"].item, engine))
    texts = [e["text"] for e in engine.requests[0]["state"]["labeled_examples"]]
    assert BY_ID["pos-0"].item.values["text"] not in texts
    assert BY_ID["pos-2"].item.values["text"] in texts and len(texts) == 4


def test_a_bundle_without_a_list_asks_zero_shot_with_the_text_alone(tmp_path):
    bundle = _bundle(examples=None)
    bundle.save(tmp_path / "z")
    assert not (tmp_path / "z" / EXAMPLES_FILE).exists()
    engine = FakeEngine()
    reloaded = _load(tmp_path / "z")
    assert _classify(reloaded, engine) == _classify(bundle, FakeEngine())
    assert engine.requests[0]["state"] == {"text": TARGETS[0].values["text"]}
    assert json.loads((tmp_path / "z" / MANIFEST_FILE).read_text())["fewshot"] is None


def test_the_manifest_is_text_free_and_records_hashes_lineage_and_engine(tmp_path):
    _bundle().save(tmp_path / "b")
    blob = (tmp_path / "b" / MANIFEST_FILE).read_text()
    for row in ROWS:
        assert row.item.values["text"] not in blob
    manifest = json.loads(blob)
    assert manifest["task"]["fingerprint"] == TASK.fingerprint
    assert manifest["engine"] == ENGINE
    assert manifest["fewshot"]["fingerprint"] == FIXED.fingerprint
    assert manifest["rubric"]["cyclotron_sha256"] == hashlib.sha256(CYCLOTRON.encode()).hexdigest()
    assert set(manifest["rubric"]["questions"]) == {"tone", "tone.sport"}
    assert manifest["retrieval"] is None and manifest["lineage"]["round"] == 3


@pytest.mark.parametrize("name", [MANIFEST_FILE, EXAMPLES_FILE, CYCLOTRON_FILE])
def test_an_edited_file_is_rejected_before_any_model_call(tmp_path, name):
    _bundle().save(tmp_path / "b")
    path = tmp_path / "b" / name
    path.write_text(path.read_text().replace("sample", "simple", 1).replace("2.0", "3.0", 1)
                    .replace('"round":3', '"round":4', 1))
    with pytest.raises(BundleValidationError):
        _load(tmp_path / "b")


def test_a_different_task_or_configured_model_is_refused(tmp_path):
    _bundle().save(tmp_path / "b")
    with pytest.raises(BundleValidationError):
        load_bundle(tmp_path / "b", DecisionTask("tone", ("pos", "neg"), "Other wording."), Rubric)
    with pytest.raises(BundleValidationError):
        _load(tmp_path / "b", configured_model="another-model")
    assert _load(tmp_path / "b", configured_model="fake-1").engine == ENGINE


def test_a_different_reported_model_produces_a_warning_and_still_answers(tmp_path):
    bundle = _bundle()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = asyncio.run(bundle.classify(TARGETS[0], FakeEngine(model="fake-2")))
    assert result.label in TASK.labels
    assert any(issubclass(w.category, BundleModelWarning) for w in caught)


def test_example_rows_must_match_the_list():
    rows = [BY_ID[i] for i in FIXED.example_ids + FIXED.reserve_ids]
    changed = [LabeledItem(Item(r.item.id, {"text": r.item.values["text"] + "!"}), r.label) for r in rows]
    with pytest.raises(BundleValidationError):
        ClassifierBundle(TASK, CYCLOTRON, Rubric(CYCLOTRON), examples=FIXED, example_rows=changed, engine=ENGINE)
    with pytest.raises(BundleValidationError):
        ClassifierBundle(TASK, CYCLOTRON, Rubric(CYCLOTRON), examples=FIXED, example_rows=rows[:-1], engine=ENGINE)


def test_saving_refuses_to_overwrite_a_different_bundle(tmp_path):
    _bundle().save(tmp_path / "b")
    _bundle().save(tmp_path / "b")   # the same bundle again is fine
    with pytest.raises(BundleValidationError):
        _bundle(examples=None).save(tmp_path / "b")
