"""The committed lab criteria files are exactly what a deploy would apply to the live catalog."""

import json
from pathlib import Path

import pytest

from decision_flywheel.classifier_criteria import (
    apply_plan,
    load_definition,
    load_directory,
    plan_classifiers,
    plan_definition,
)
from decision_flywheel.s3_console_storage_test import store

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "infra/lab/criteria"
LIVE_FAMILY = "shared-source-relevance"
NEW_CLASSIFIER = "data-parade-relevance"


def committed():
    return load_directory(DIRECTORY, relative_to=ROOT), load_definition(DIRECTORY)


def catalog_at_revision_one(criteria):
    """The lab catalog as read from production on 2026-10-21: three revision 1 classifiers and one definition.

    Classifiers whose criteria file has no ``previous`` are new and are not in the catalog yet.
    """
    s = store()
    existing = [c for c in criteria if not c.is_new]
    for item in existing:
        s.save_classifier(item.classifier_id, item.name, item.previous_config())
    refs = [{"id": c.classifier_id, "revision": 1} for c in existing]
    settings = {"decisions_model": "jev-1.13.0", "decisions_provider": "jev", "description": "Draft."}
    s.save_cyclotron_definition(LIVE_FAMILY, "Shared source relevance (DRAFT)", refs, settings)
    return s


def test_every_lab_classifier_has_one_criteria_file_with_a_source_and_an_approval():
    criteria, definition = committed()
    assert {c.classifier_id for c in criteria} == set(definition["order"])
    for item in criteria:
        assert item.doctrine["sources"] and item.doctrine["summary"]
        assert item.approval["status"] in ("approved", "pending")
        if item.is_new:
            assert item.classifier_id == NEW_CLASSIFIER
        else:
            assert item.previous["revision"] == 1 and item.previous["question"] != item.question


def test_approved_files_record_who_approved_them_and_when():
    criteria, _ = committed()
    approved = [c for c in criteria if c.approved]
    assert {c.classifier_id for c in approved} == {
        "anth-us-relevance",
        "p-apyr-us-relevance",
        "threat-intelligence-relevance",
        NEW_CLASSIFIER,
    }
    on = {c.classifier_id: c.approval["approved_on"] for c in approved}
    assert all(c.approval["status"] == "approved" and c.approval["approved_by"] == "Ryan" for c in approved)
    assert on.pop(NEW_CLASSIFIER) == "2026-10-23" and set(on.values()) == {"2026-10-22"}


def test_a_deploy_applies_existing_classifiers_as_revision_two_and_the_new_one_as_revision_one_in_a_successor_definition():
    criteria, definition = committed()
    s = catalog_at_revision_one(criteria)
    steps = plan_classifiers(s, criteria)
    assert all(step.action == "save" for step in steps)
    plan = plan_definition(s, criteria, steps, definition)
    assert plan.action == "define" and not plan.name.endswith("awaiting editor approval)")
    apply_plan(s, criteria, steps, plan)
    for item in criteria:
        live = s.classifier(item.classifier_id)
        assert live["revision"] == (1 if item.is_new else 2)
        assert live["config"] == item.classifier_config()
        assert live["config"]["question"] == item.question
        if not item.is_new:
            assert s.classifier(item.classifier_id, 1)["config"]["question"] == item.previous["question"]
    successor = s.cyclotron_definition(definition["id"])
    assert successor["classifiers"] == [
        {"id": cid, "revision": 1 if cid == NEW_CLASSIFIER else 2} for cid in definition["order"]
    ]
    assert all(step.action == "current" for step in plan_classifiers(s, criteria))


def test_a_deploy_never_moves_the_definition_the_standing_run_is_pinned_to():
    criteria, definition = committed()
    s = catalog_at_revision_one(criteria)
    steps = plan_classifiers(s, criteria)
    apply_plan(s, criteria, steps, plan_definition(s, criteria, steps, definition))
    pinned = s.cyclotron_definition(LIVE_FAMILY, 1)
    assert pinned["classifiers"] == [{"id": cid, "revision": 1} for cid in definition["order"] if cid != NEW_CLASSIFIER]
    # The engine store is named for the cyclotron id; a changed question needs a new id (ticket ade3088c).
    assert definition["id"] != LIVE_FAMILY and definition["supersedes"] == LIVE_FAMILY


def test_the_previous_text_in_each_file_is_what_the_live_catalog_must_still_hold():
    criteria, definition = committed()
    s = catalog_at_revision_one(criteria)
    s.save_classifier(criteria[0].classifier_id, criteria[0].name, {**criteria[0].previous_config(), "question": "Hand edit?"})
    steps = plan_classifiers(s, criteria)
    assert steps[0].action == "drift"
    assert plan_definition(s, criteria, steps, definition).action == "blocked"


@pytest.mark.parametrize("cid", ["anth-us-relevance", "p-apyr-us-relevance", "threat-intelligence-relevance", NEW_CLASSIFIER])
def test_the_question_is_stored_as_one_line_of_text_exactly_as_approved(cid):
    document = json.loads((DIRECTORY / f"{cid}.json").read_text())
    assert "\n" not in document["question"] and document["question"].startswith("Should this source become a reference for")
