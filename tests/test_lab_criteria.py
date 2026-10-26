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
LIVE_FAMILY = "shared-source-relevance-r2"
ORIGINAL_FAMILY = "shared-source-relevance"
# Classifiers created at revision 1 by a reviewed criteria file (no ``previous``), and the date Ryan approved each.
NEW_CLASSIFIERS = {"data-parade-relevance": "2026-10-23", "pilobol-us-relevance": "2026-10-23", "shipped-relevance": "2026-10-25"}
# Classifiers that existed before the r3 deploy: everything but the one this deploy adds.
ADDED_BY_THIS_DEPLOY = ("shipped-relevance",)


def committed():
    return load_directory(DIRECTORY, relative_to=ROOT), load_definition(DIRECTORY)


def catalog_after_r2(criteria, definition):
    """The lab catalog as it stands after the r2 migration (2026-10-24/25), before this deploy.

    The three original classifiers have their revision 1 (previous) and revision 2 (approved) text; Data Parade and
    Pilobolus exist at revision 1; the original family and the r2 successor definition both exist.
    """
    s = store()
    settings = {"decisions_model": "jev-1.13.0", "decisions_provider": "jev", "description": "Draft."}
    rewritten = [c for c in criteria if not c.is_new]
    for item in rewritten:
        s.save_classifier(item.classifier_id, item.name, item.previous_config())
    s.save_cyclotron_definition(
        ORIGINAL_FAMILY, "Shared source relevance (DRAFT)", [{"id": c.classifier_id, "revision": 1} for c in rewritten], settings
    )
    for item in criteria:
        if item.classifier_id not in ADDED_BY_THIS_DEPLOY:
            s.save_classifier(item.classifier_id, item.name, item.classifier_config())
    r2 = [cid for cid in definition["order"] if cid not in ADDED_BY_THIS_DEPLOY]
    s.save_cyclotron_definition(
        LIVE_FAMILY,
        definition["name"],
        [{"id": cid, "revision": 1 if cid in NEW_CLASSIFIERS else 2} for cid in r2],
        {**settings, "description": "Five publications."},
    )
    return s


def test_every_lab_classifier_has_one_criteria_file_with_a_source_and_an_approval():
    criteria, definition = committed()
    assert {c.classifier_id for c in criteria} == set(definition["order"])
    for item in criteria:
        assert item.doctrine["sources"] and item.doctrine["summary"]
        assert item.approval["status"] in ("approved", "pending")
        if item.is_new:
            assert item.classifier_id in NEW_CLASSIFIERS
        else:
            assert item.previous["revision"] == 1 and item.previous["question"] != item.question


def test_approved_files_record_who_approved_them_and_when():
    criteria, _ = committed()
    approved = [c for c in criteria if c.approved]
    assert {c.classifier_id for c in approved} == {
        "anth-us-relevance",
        "p-apyr-us-relevance",
        "threat-intelligence-relevance",
        *NEW_CLASSIFIERS,
    }
    on = {c.classifier_id: c.approval["approved_on"] for c in approved}
    assert all(c.approval["status"] == "approved" and c.approval["approved_by"] == "Ryan" for c in approved)
    assert all(on.pop(cid) == day for cid, day in NEW_CLASSIFIERS.items()) and set(on.values()) == {"2026-10-22"}


def test_a_deploy_adds_only_the_new_classifier_and_defines_a_successor_with_the_live_revisions():
    criteria, definition = committed()
    s = catalog_after_r2(criteria, definition)
    steps = plan_classifiers(s, criteria)
    assert {step.classifier_id: step.action for step in steps} == {
        c.classifier_id: "save" if c.classifier_id in ADDED_BY_THIS_DEPLOY else "current" for c in criteria
    }
    plan = plan_definition(s, criteria, steps, definition)
    assert plan.action == "define" and not plan.name.endswith("awaiting editor approval)")
    apply_plan(s, criteria, steps, plan)
    for item in criteria:
        live = s.classifier(item.classifier_id)
        assert live["revision"] == (1 if item.is_new else 2)
        assert live["config"] == item.classifier_config()
    successor = s.cyclotron_definition(definition["id"])
    assert successor["classifiers"] == [
        {"id": cid, "revision": 1 if cid in NEW_CLASSIFIERS else 2} for cid in definition["order"]
    ]
    assert all(step.action == "current" for step in plan_classifiers(s, criteria))


def test_a_deploy_never_moves_the_definition_the_standing_run_is_pinned_to():
    criteria, definition = committed()
    s = catalog_after_r2(criteria, definition)
    before = s.cyclotron_definition(LIVE_FAMILY, 1)
    steps = plan_classifiers(s, criteria)
    apply_plan(s, criteria, steps, plan_definition(s, criteria, steps, definition))
    assert s.cyclotron_definition(LIVE_FAMILY, 1)["classifiers"] == before["classifiers"]
    # The engine store is named for the cyclotron id; a changed classifier set needs a new id (ticket ade3088c).
    assert definition["id"] != LIVE_FAMILY and definition["supersedes"] == LIVE_FAMILY


def test_a_hand_edited_live_question_blocks_the_deploy():
    criteria, definition = committed()
    s = catalog_after_r2(criteria, definition)
    edited = next(c for c in criteria if not c.is_new)
    s.save_classifier(edited.classifier_id, edited.name, {**edited.classifier_config(), "question": "Hand edit?"})
    steps = plan_classifiers(s, criteria)
    assert next(step for step in steps if step.classifier_id == edited.classifier_id).action == "drift"
    assert plan_definition(s, criteria, steps, definition).action == "blocked"


@pytest.mark.parametrize("cid", ["anth-us-relevance", "p-apyr-us-relevance", "threat-intelligence-relevance", *NEW_CLASSIFIERS])
def test_the_question_is_stored_as_one_line_of_text_exactly_as_approved(cid):
    document = json.loads((DIRECTORY / f"{cid}.json").read_text())
    assert "\n" not in document["question"] and document["question"].startswith("Should this source become a reference for")
