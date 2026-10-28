"""Standing-run source validation and quota-free target configuration."""

import json
from uuid import uuid4

import pytest

from .hosted_publications import targets_from_environment
from .machine_access import principals_from_environment
from .standing_publications import validate_item

GOOD = {"id": "hn-42", "values": {"text": "A story.", "url": "https://example.test/a", "points": 12}}


def test_a_valid_source_is_returned_in_canonical_form():
    assert validate_item(GOOD, ["text"]) == ("hn-42", GOOD["values"])


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"id": "x"},
        {**GOOD, "extra": 1},
        {**GOOD, "id": ""},
        {**GOOD, "id": " padded"},
        {**GOOD, "id": "new\nline"},
        {**GOOD, "id": 7},
        {**GOOD, "values": {}},
        {**GOOD, "values": {"text": "ok", "": 1}},
        {**GOOD, "values": {"text": ""}},
        {**GOOD, "values": {"text": 5}},
        {**GOOD, "values": {"text": "ok", "bad": float("nan")}},
        {**GOOD, "values": {"text": "ok", "bad": object()}},
    ],
)
def test_unfit_sources_are_refused(item):
    with pytest.raises(ValueError):
        validate_item(item, ["text"])


def test_a_source_with_a_long_id_many_long_keys_and_a_200_kb_text_is_accepted_whole():
    values = {"text": "x" * 200_000, **{f"field-{n:03d}-" + "k" * 100: n for n in range(100)}}
    item = {"id": "i" * 1000, "values": values}
    assert validate_item(item, ["text"]) == ("i" * 1000, values)


def test_a_standing_target_does_not_need_request_or_item_quota_fields():
    target = anthus_targets(RUN_PLACEHOLDER)[0]
    (parsed,) = targets_from_environment({"CYCLOTRON_PUBLICATION_TARGETS": json.dumps([target])})
    assert parsed.standing is True
    assert not hasattr(parsed, "request_ceiling") and not hasattr(parsed, "max_items")


@pytest.mark.parametrize(
    "field,value", [("requestCeiling", {"total": 300, "perDay": 100, "since": "2026-10-21"}), ("maxItems", 300)]
)
def test_a_target_with_a_request_or_item_quota_field_is_rejected_as_an_unknown_key(field, value):
    target = anthus_targets(RUN_PLACEHOLDER)[0]
    target[field] = value
    with pytest.raises(ValueError, match="invalid"):
        targets_from_environment({"CYCLOTRON_PUBLICATION_TARGETS": json.dumps([target])})


def test_machine_principals_must_be_exact_distinct_role_arns():
    role = "arn:aws:iam::222222222222:role/papyrus-monitor"
    assert principals_from_environment({}) == () and principals_from_environment({"CYCLOTRON_MACHINE_PRINCIPALS": ""}) == ()
    assert principals_from_environment({"CYCLOTRON_MACHINE_PRINCIPALS": json.dumps([role])}) == (role,)
    many = [f"arn:aws:iam::222222222222:role/papyrus-monitor-{n}-" + "r" * 80 for n in range(50)]
    assert principals_from_environment({"CYCLOTRON_MACHINE_PRINCIPALS": json.dumps(many)}) == tuple(many)
    for bad in (
        [role, role],
        ["arn:aws:iam::222222222222:role/*"],
        ["arn:aws:iam::*:role/papyrus-monitor"],
        ["arn:aws:iam::222222222222:role/path/papyrus-monitor"],
        ["arn:aws:iam::222222222222:user/papyrus-monitor"],
        ["arn:aws:sts::222222222222:assumed-role/papyrus-monitor/session"],
        ["222222222222"],
        role,
        {"role": role},
    ):
        with pytest.raises(ValueError, match="exact IAM role ARNs"):
            principals_from_environment({"CYCLOTRON_MACHINE_PRINCIPALS": json.dumps(bad)})
    with pytest.raises(ValueError):
        principals_from_environment({"CYCLOTRON_MACHINE_PRINCIPALS": "not json"})


ACCOUNT = str(uuid4())  # synthetic; the reviewed shape is what is tested
RUN_PLACEHOLDER = str(uuid4())


def anthus_targets(run_id):
    """The reviewed shape for a standing run; the run id is filled once it exists."""
    return [
        {
            "accountId": ACCOUNT,
            "runId": run_id,
            "modelIdentity": "jev:jev-1.13.0",
            "standing": True,
            "bindings": [
                {"publicationId": "anth-us", "outputs": ["anth-us-relevance"], "revision": "1"},
                {"publicationId": "p-apyr-us", "outputs": ["p-apyr-us-relevance"], "revision": "1"},
                {"publicationId": "threat-intelligence", "outputs": ["threat-intelligence-relevance"], "revision": "1"},
                {"publicationId": "data-parade", "outputs": ["data-parade-relevance"], "revision": "1"},
            ],
            "pendingOutputs": ["data-parade-relevance"],
        }
    ]


def test_the_reviewed_anthus_target_configuration_parses():
    (target,) = targets_from_environment({"CYCLOTRON_PUBLICATION_TARGETS": json.dumps(anthus_targets(RUN_PLACEHOLDER))})
    assert target.account_id == ACCOUNT and target.standing and target.model_identity == "jev:jev-1.13.0"
    assert [b.publication_id for b in target.bindings] == ["anth-us", "p-apyr-us", "threat-intelligence", "data-parade"]
    assert target.pending_outputs == ("data-parade-relevance",)
    assert target.standing is True
    assert not hasattr(target, "request_ceiling") and not hasattr(target, "max_items")


def test_target_configuration_rejects_unknown_or_unsafe_standing_fields():
    for change in (
        {"standing": "true"},
        {"machine": True},
    ):
        raw = [{**anthus_targets(RUN_PLACEHOLDER)[0], **change}]
        with pytest.raises(ValueError, match="configuration"):
            targets_from_environment({"CYCLOTRON_PUBLICATION_TARGETS": json.dumps(raw)})


def test_a_target_configuration_of_three_hundred_targets_with_a_long_model_identity_parses():
    identity = "jev:" + "m" * 2_000
    raw = [{**anthus_targets(str(uuid4()))[0], "modelIdentity": identity} for _ in range(300)]
    targets = targets_from_environment({"CYCLOTRON_PUBLICATION_TARGETS": json.dumps(raw)})
    assert len(targets) == 300 and {target.model_identity for target in targets} == {identity}
