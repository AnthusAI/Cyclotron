"""Trusted standing-run policy: allowance arithmetic, item admission bounds and the proposed target shape."""

import json
from datetime import date
from uuid import uuid4

import pytest

from .hosted_publications import targets_from_environment
from .machine_access import principals_from_environment
from .standing_publications import MAX_VALUES_BYTES, RequestCeiling, validate_item


def test_a_cumulative_ceiling_without_a_daily_allowance_is_just_the_total():
    assert RequestCeiling(300).allowed(date(2030, 1, 1)) == 300


def test_a_daily_allowance_accrues_from_its_start_date_up_to_the_total():
    ceiling = RequestCeiling(250, per_day=100, since="2026-10-21")
    assert ceiling.allowed(date(2026, 10, 20)) == 0
    assert [ceiling.allowed(date(2026, 10, d)) for d in (21, 22, 23, 24)] == [100, 200, 250, 250]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"total": 0},
        {"total": True},
        {"total": 100001},
        {"total": 5, "per_day": 1},
        {"total": 5, "since": "2026-10-21"},
        {"total": 5, "per_day": 1, "since": "2026-13-40"},
        {"total": 5, "per_day": 1, "since": "tomorrow"},
        {"total": 5, "per_day": 0, "since": "2026-10-21"},
    ],
)
def test_malformed_ceilings_are_refused(kwargs):
    with pytest.raises(ValueError):
        RequestCeiling(**kwargs)


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
        {**GOOD, "id": "x" * 201},
        {**GOOD, "id": "new\nline"},
        {**GOOD, "id": 7},
        {**GOOD, "values": {}},
        {**GOOD, "values": {"text": ""}},
        {**GOOD, "values": {"text": 5}},
        {**GOOD, "values": {"text": "ok", "bad": float("nan")}},
        {**GOOD, "values": {"text": "ok", "bad": object()}},
        {**GOOD, "values": {"text": "x" * 20001}},
        {**GOOD, "values": {"text": "ok", "pad": "y" * MAX_VALUES_BYTES}},
        {**GOOD, "values": {"text": "ok", **{f"k{n}": 1 for n in range(40)}}},
    ],
)
def test_unbounded_or_unfit_sources_are_refused(item):
    with pytest.raises(ValueError):
        validate_item(item, ["text"])


def test_machine_principals_must_be_exact_distinct_role_arns():
    role = "arn:aws:iam::222222222222:role/papyrus-monitor"
    assert principals_from_environment({}) == () and principals_from_environment({"CYCLOTRON_MACHINE_PRINCIPALS": ""}) == ()
    assert principals_from_environment({"CYCLOTRON_MACHINE_PRINCIPALS": json.dumps([role])}) == (role,)
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
            "requestCeiling": {"total": 300, "perDay": 100, "since": "2026-10-21"},
            "maxItems": 300,
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
    assert (target.request_ceiling.total, target.request_ceiling.per_day, target.max_items) == (300, 100, 300)


def test_target_configuration_rejects_unknown_or_unsafe_standing_fields():
    for change in (
        {"standing": "true"},
        {"standing": False},
        {"requestCeiling": {"perDay": 5, "since": "2026-10-21"}},
        {"requestCeiling": {"total": 5, "burst": 9}},
        {"requestCeiling": 5},
        {"maxItems": 0},
        {"machine": True},
    ):
        raw = [{**anthus_targets(RUN_PLACEHOLDER)[0], **change}]
        with pytest.raises(ValueError, match="configuration"):
            targets_from_environment({"CYCLOTRON_PUBLICATION_TARGETS": json.dumps(raw)})
