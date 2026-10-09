"""An application can render what a cyclotron is doing from one validated snapshot."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from .classifier_config import ClassifierConfig
from .cyclotron_status import (FULL_REVIEW, SCHEMA_PATH, ReviewRate, ReviewRateOverride, load_schema,
                               status_from_events, status_from_flywheel)
from .feedback import LABEL_SOURCE_VETTED, FeedbackItem
from .flywheel import DecisionFlywheel
from .flywheel_test import DEV, TASK, TRAIN, FakeModel, agent

EXAMPLE = SCHEMA_PATH.with_name("cyclotron-status.v1.example.json")
TS_TYPES = Path(__file__).resolve().parents[2] / "trace-ui" / "src" / "cyclotronStatus.ts"
LABELS = ("include", "exclude")


def validate(value, schema, path="$"):
    """Small JSON Schema subset validator: enough for this schema, no dependency."""
    if "oneOf" in schema:
        errors = []
        for option in schema["oneOf"]:
            try:
                validate(value, option, path)
                return
            except AssertionError as error:
                errors.append(str(error))
        raise AssertionError(f"{path}: no oneOf option matched: {errors}")
    if "const" in schema:
        assert value == schema["const"], f"{path}: expected {schema['const']!r}"
    if "enum" in schema:
        assert value in schema["enum"], f"{path}: {value!r} not in {schema['enum']}"
    kinds = schema.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        checks = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
                  "string": lambda v: isinstance(v, str), "null": lambda v: v is None,
                  "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
                  "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)}
        assert any(checks[k](value) for k in kinds), f"{path}: {value!r} is not {kinds}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        assert value >= schema.get("minimum", value), f"{path}: below minimum"
        assert value <= schema.get("maximum", value), f"{path}: above maximum"
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            assert key in value, f"{path}: missing {key}"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            assert not extra, f"{path}: unexpected {sorted(extra)}"
        for key, child in properties.items():
            if key in value:
                validate(value[key], child, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, entry in enumerate(value):
            validate(entry, schema["items"], f"{path}[{index}]")


def prediction(item, label, include_probability, version="v-one"):
    return {"kind": "prediction", "target_id": item, "label": label, "version": version,
            "probabilities": {"include": include_probability, "exclude": 1 - include_probability},
            "confidence": max(include_probability, 1 - include_probability),
            "created_at": "2026-10-09T10:00:00+00:00"}


def review(item, label, *, feedback_id=None, action="submitted", selected_by=None, at="2026-10-09T11:00:00+00:00"):
    event = {"kind": "human-feedback", "action": action, "assignment": None, "created_at": at,
             "feedback": {"id": feedback_id or f"f-{item}", "item_id": item, "score_name": "inclusion",
                          "final_answer_value": label}}
    if selected_by:
        event["selected_by"] = selected_by
    return event


def build(events, **kwargs):
    return status_from_events(events, cyclotron_id="papyrus-relevance", classifier="relevant", labels=LABELS,
                              fingerprint=kwargs.pop("fingerprint", "v-one"), **kwargs)


def test_the_published_example_matches_the_schema():
    validate(json.loads(EXAMPLE.read_text()), load_schema())


def test_an_empty_cyclotron_reports_no_alignment_yet_and_full_review(tmp_path):
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel())
    status = status_from_flywheel(wheel, cyclotron_id="papyrus-relevance")
    wheel.close()
    snapshot = status.to_json()
    validate(snapshot, load_schema())
    assert snapshot["cyclotron"] == {"id": "papyrus-relevance", "classifier": "inclusion", "version": 1,
                                     "fingerprint": status.fingerprint}
    assert snapshot["alignment"]["labels"] == 0 and snapshot["alignment"]["accuracy"] is None
    assert snapshot["alignment"]["positiveLabel"] == "include"
    assert snapshot["calibration"] == {"saysSure": None, "isRight": None, "gapPoints": None, "curve": []}
    assert snapshot["reviewRate"]["state"] == "full" and snapshot["reviewRate"]["rate"] == 1.0
    assert snapshot["lastChange"] is None
    assert snapshot["pending"] == {"decisionsAwaitingReview": 0, "staleSince": None}


def test_alignment_uses_the_decision_shown_and_the_latest_label_per_item():
    events = [prediction("a", "include", .9), prediction("b", "include", .8), prediction("c", "exclude", .3),
              prediction("d", "exclude", .4),
              review("a", "include"), review("b", "exclude"), review("c", "exclude"),
              review("c", "include", feedback_id="f-c2"),  # a correction replaces the earlier label
              review("d", "exclude")]
    snapshot = build(events).to_json()
    validate(snapshot, load_schema())
    alignment = snapshot["alignment"]
    assert alignment["labels"] == 4
    assert alignment["accuracy"] == pytest.approx(2 / 4)
    assert alignment["precision"] == pytest.approx(1 / 2)  # include decided for a, b; only a agreed
    assert alignment["recall"] == pytest.approx(1 / 2)     # include labels a, c; only a decided include
    calibration = snapshot["calibration"]
    assert calibration["saysSure"] == pytest.approx((.9 + .8 + .7 + .6) / 4)
    assert calibration["isRight"] == pytest.approx(.5)
    assert calibration["gapPoints"] == 25
    assert sum(entry["count"] for entry in calibration["curve"]) == 4
    assert snapshot["pending"]["decisionsAwaitingReview"] == 0


def test_reviews_a_reviewer_chose_and_retracted_reviews_do_not_count_toward_alignment():
    events = [prediction("a", "include", .9), prediction("b", "include", .9), prediction("c", "include", .9),
              review("a", "exclude", selected_by="reviewer"),
              review("b", "include"), review("b", "include", action="retracted"),
              review("c", "include")]
    status = build(events)
    assert status.alignment.labels == 1
    assert status.alignment.accuracy == 1.0
    assert status.pending.decisions_awaiting_review == 1  # b's review was undone


def test_a_promotion_becomes_the_last_change_and_raises_the_version():
    events = [{"kind": "cycle-started", "classifier_snapshot": {"config": {"rubric": "", "example_ids": []}}},
              prediction("a", "include", .9, version="v-one"), review("a", "exclude"),
              {"kind": "classifier-activated", "classifier_version": "v-two",
               "classifier_snapshot": {"config": {"rubric": "Prefer primary sources.", "example_ids": []}},
               "created_at": "2026-10-09T12:00:00+00:00"},
              {"kind": "promoted", "version": "v-two", "reason": "lower development brier"},
              prediction("b", "exclude", .2, version="v-two"),
              review("b", "exclude", at="2026-10-09T13:00:00+00:00")]
    status = build(events, fingerprint="v-two")
    assert status.version == 2
    assert asdict(status.last_change) == {"kind": "promoted", "from_version": 1, "to_version": 2,
                                          "at": "2026-10-09T12:00:00+00:00", "summary": "Changed the rubric."}
    assert status.pending.stale_since == "2026-10-09T13:00:00+00:00"


def test_an_ml_model_refit_is_a_new_version_described_as_a_refit():
    snapshot = {"config": {"rubric": "Prefer primary sources."}, "head": None}
    events = [{"kind": "cycle-started", "classifier_snapshot": snapshot},
              {"kind": "classifier-activated", "classifier_version": "v-two", "classifier_snapshot": {
                  **snapshot, "head": {"provenance": {"training_ids": ["a", "b", "c"]}}}}]
    status = build(events, fingerprint="v-two")
    assert status.version == 2
    assert status.last_change.from_version == 1 and status.last_change.summary == "Refit the ML model on 3 labels."


def test_a_dropped_candidate_is_reported_without_changing_the_version():
    events = [prediction("a", "include", .9), {"kind": "candidate-rejected", "version": "v-one",
                                               "reason": "development brier did not improve"}]
    status = build(events)
    assert status.version == 1
    assert status.last_change.kind == "dropped" and status.last_change.summary == "development brier did not improve"


def test_a_running_flywheel_with_a_promoted_version_reports_it_without_model_calls(tmp_path):
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), model, agent([]), max_requests=30)
    asyncio.run(wheel.predict(DEV[0].item, TRAIN))
    wheel.record_feedback_event(FeedbackItem("f1", DEV[0].item.id, TASK.name, "exclude", DEV[0].label,
                                             label_source=LABEL_SOURCE_VETTED, selection_propensity=1.))
    result = asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1.0 for r in TRAIN}))
    assert result["promoted"]
    calls = model.calls
    snapshot = status_from_flywheel(wheel, cyclotron_id="inclusion-cyclotron").to_json()
    wheel.close()
    assert model.calls == calls
    validate(snapshot, load_schema())
    assert snapshot["cyclotron"]["version"] == 2
    assert snapshot["lastChange"]["kind"] == "promoted" and snapshot["lastChange"]["toVersion"] == 2
    assert snapshot["alignment"]["labels"] == 1


def test_a_review_rate_supplied_by_a_program_is_reported_with_its_override():
    rate = ReviewRate("manual", .5, "Set by the managing editor for the election week.", audit_floor=.1,
                      override=ReviewRateOverride(.5, "2026-10-20T00:00:00+00:00", "editor", "2026-10-09T00:00:00+00:00"))
    snapshot = build([], review_rate=rate).to_json()
    validate(snapshot, load_schema())
    assert snapshot["reviewRate"]["override"]["setBy"] == "editor"
    assert FULL_REVIEW.reason


@pytest.mark.parametrize("kwargs", [{"state": "lazy", "rate": .5, "reason": "x"},
                                    {"state": "manual", "rate": 1.5, "reason": "x"},
                                    {"state": "manual", "rate": .5, "reason": " "}])
def test_an_invalid_review_rate_is_refused(kwargs):
    with pytest.raises(ValueError):
        ReviewRate(**kwargs)


def test_the_typescript_type_names_every_schema_field():
    source = TS_TYPES.read_text()
    def names(schema):
        for option in schema.get("oneOf", ()):
            yield from names(option)
        for key, child in schema.get("properties", {}).items():
            yield key
            yield from names(child)
            yield from names(child.get("items", {}))
    missing = sorted({name for name in names(load_schema()) if f"{name}:" not in source and f"{name}?:" not in source})
    assert not missing, f"trace-ui/src/cyclotronStatus.ts lacks {missing}"
