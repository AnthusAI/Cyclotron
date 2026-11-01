"""The committed lab criteria files are exactly what a deploy would apply to the live catalog."""

import hashlib
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
PREVIOUS_FAMILY = "shared-source-relevance-r4"
R3_FAMILY = "shared-source-relevance-r3"
R2_FAMILY = "shared-source-relevance-r2"
ORIGINAL_FAMILY = "shared-source-relevance"
# Classifiers created at revision 1 by a reviewed criteria file (no ``previous``), and the date Ryan approved each.
NEW_CLASSIFIERS = {"data-parade-relevance": "2026-10-23", "pilobol-us-relevance": "2026-10-23"}
# The classifier this deploy creates, and the day Ryan approved it: Papyrus's newspaper renamed Machines on Machines,
# on its own site. It replaces p-apyr-us-relevance, which stays in the catalog at revision 2 with no criteria file.
NEW_IN_THIS_DEPLOY = {"machines-on-machines-relevance": "2026-11-01"}
RETIRED = "p-apyr-us-relevance"
# The original family's classifiers that still have criteria files; each was rewritten at revision 2 by the r2 migration.
ORIGINAL_CLASSIFIERS = ("anth-us-relevance", "threat-intelligence-relevance")
# Shipped's mission rewrite (revision 1 -> 2, the r4 deploy), and the day Ryan approved it.
REWRITTEN_BY_R4 = {"shipped-relevance": "2026-10-27"}
# The retired classifier's live configs, as the catalog holds them (its file now lives on as the new classifier).
RETIRED_CONFIG = {
    "classes": [{"label": "include", "role": "positive"}, {"label": "exclude", "role": "negative"}],
    "question": "Should this source become a reference for Papyrus's newspaper (revision 2)?",
    "rubric": "",
}
# The revision of each classifier in the definition this deploy produces.
REVISIONS = {
    "anth-us-relevance": 2,
    "machines-on-machines-relevance": 1,
    "threat-intelligence-relevance": 2,
    "data-parade-relevance": 1,
    "pilobol-us-relevance": 1,
    "shipped-relevance": 2,
}


def committed():
    return load_directory(DIRECTORY, relative_to=ROOT), load_definition(DIRECTORY)


def live_previous_config(item, approved_on):
    """A classifier's previous revision as its deploy saved it: the old question with its criteria provenance block."""
    provenance = {
        "file": item.path,
        "source": item.doctrine,
        "approval": {"status": "approved", "approved_by": "Ryan", "approved_on": approved_on},
        "question_sha256": hashlib.sha256(item.previous["question"].encode()).hexdigest(),
    }
    return {**item.previous_config(), "criteria": provenance}


def catalog_after_r4(criteria, definition):
    """The lab catalog as it stands after the r4 deploy (2026-10-27), before this deploy.

    The original family (Anth.us, Papyrus's newspaper, Threat Intelligence) has revisions 1 and 2; Data Parade and
    Pilobolus exist at revision 1; Shipped has revisions 1 and 2; the original family and the r2, r3 and r4 successor
    definitions all exist. Machines on Machines does not exist yet.
    """
    s = store()
    settings = {"decisions_model": "jev-1.13.0", "decisions_provider": "jev", "description": "Draft."}
    by_id = {c.classifier_id: c for c in criteria}
    for cid in ORIGINAL_CLASSIFIERS:
        s.save_classifier(cid, by_id[cid].name, by_id[cid].previous_config())
    s.save_classifier(RETIRED, "Papyrus relevance", {**RETIRED_CONFIG, "question": "Revision 1?"})
    family = [*ORIGINAL_CLASSIFIERS, RETIRED]
    s.save_cyclotron_definition(
        ORIGINAL_FAMILY, "Shared source relevance (DRAFT)", [{"id": cid, "revision": 1} for cid in family], settings
    )
    for cid in ORIGINAL_CLASSIFIERS:
        s.save_classifier(cid, by_id[cid].name, by_id[cid].classifier_config())
    s.save_classifier(RETIRED, "Papyrus relevance", RETIRED_CONFIG)
    for cid in NEW_CLASSIFIERS:
        s.save_classifier(cid, by_id[cid].name, by_id[cid].classifier_config())
    shipped = by_id["shipped-relevance"]
    s.save_classifier(shipped.classifier_id, shipped.name, live_previous_config(shipped, "2026-10-25"))
    r4_order = [RETIRED if cid in NEW_IN_THIS_DEPLOY else cid for cid in definition["order"]]
    r2 = [cid for cid in r4_order if cid != "shipped-relevance"]
    s.save_cyclotron_definition(
        R2_FAMILY,
        definition["name"],
        [{"id": cid, "revision": 1 if cid in NEW_CLASSIFIERS else 2} for cid in r2],
        {**settings, "description": "Five publications."},
    )
    s.save_cyclotron_definition(
        R3_FAMILY,
        definition["name"],
        [{"id": cid, "revision": 1 if cid in NEW_CLASSIFIERS or cid in REWRITTEN_BY_R4 else 2} for cid in r4_order],
        {**settings, "description": "Six publications."},
    )
    s.save_classifier(shipped.classifier_id, shipped.name, shipped.classifier_config())
    s.save_cyclotron_definition(
        PREVIOUS_FAMILY,
        definition["name"],
        [{"id": cid, "revision": 1 if cid in NEW_CLASSIFIERS else 2} for cid in r4_order],
        {**settings, "description": "Six publications."},
    )
    return s


def test_every_lab_classifier_has_one_criteria_file_with_a_source_and_an_approval():
    criteria, definition = committed()
    assert {c.classifier_id for c in criteria} == set(definition["order"])
    for item in criteria:
        assert item.doctrine["sources"] and item.doctrine["summary"]
        assert item.approval["status"] in ("approved", "pending")
        if item.is_new:
            assert item.classifier_id in {**NEW_CLASSIFIERS, **NEW_IN_THIS_DEPLOY}
        else:
            assert item.previous["revision"] == REVISIONS[item.classifier_id] - 1
            assert item.previous["question"] != item.question


def test_approved_files_record_who_approved_them_and_when():
    criteria, _ = committed()
    approved = [c for c in criteria if c.approved]
    assert {c.classifier_id for c in approved} == {
        *ORIGINAL_CLASSIFIERS,
        *NEW_CLASSIFIERS,
        *NEW_IN_THIS_DEPLOY,
        *REWRITTEN_BY_R4,
    }
    on = {c.classifier_id: c.approval["approved_on"] for c in approved}
    assert all(c.approval["status"] == "approved" and c.approval["approved_by"] == "Ryan" for c in approved)
    dated = {**NEW_CLASSIFIERS, **REWRITTEN_BY_R4, **NEW_IN_THIS_DEPLOY}
    assert all(on.pop(cid) == day for cid, day in dated.items()) and set(on.values()) == {"2026-10-22"}


def test_a_deploy_adds_only_machines_on_machines_and_defines_a_successor_with_the_live_revisions():
    criteria, definition = committed()
    s = catalog_after_r4(criteria, definition)
    steps = plan_classifiers(s, criteria)
    assert {step.classifier_id: step.action for step in steps} == {
        c.classifier_id: "save" if c.classifier_id in NEW_IN_THIS_DEPLOY else "current" for c in criteria
    }
    plan = plan_definition(s, criteria, steps, definition)
    assert plan.action == "define" and not plan.name.endswith("awaiting editor approval)")
    apply_plan(s, criteria, steps, plan)
    for item in criteria:
        live = s.classifier(item.classifier_id)
        assert live["revision"] == REVISIONS[item.classifier_id]
        assert live["config"] == item.classifier_config()
    successor = s.cyclotron_definition(definition["id"])
    assert successor["classifiers"] == [{"id": cid, "revision": REVISIONS[cid]} for cid in definition["order"]]
    assert all(step.action == "current" for step in plan_classifiers(s, criteria))


def test_a_deploy_never_moves_the_definition_the_standing_run_is_pinned_to():
    criteria, definition = committed()
    s = catalog_after_r4(criteria, definition)
    before = s.cyclotron_definition(PREVIOUS_FAMILY, 1)
    steps = plan_classifiers(s, criteria)
    apply_plan(s, criteria, steps, plan_definition(s, criteria, steps, definition))
    assert s.cyclotron_definition(PREVIOUS_FAMILY, 1)["classifiers"] == before["classifiers"]
    # The engine store is named for the cyclotron id; a changed classifier set needs a new id (ticket ade3088c).
    assert definition["id"] != PREVIOUS_FAMILY and definition["supersedes"] == PREVIOUS_FAMILY


def test_a_hand_edited_live_question_blocks_the_deploy():
    criteria, definition = committed()
    s = catalog_after_r4(criteria, definition)
    edited = next(c for c in criteria if c.classifier_id in ORIGINAL_CLASSIFIERS)
    s.save_classifier(edited.classifier_id, edited.name, {**edited.classifier_config(), "question": "Hand edit?"})
    steps = plan_classifiers(s, criteria)
    assert next(step for step in steps if step.classifier_id == edited.classifier_id).action == "drift"
    assert plan_definition(s, criteria, steps, definition).action == "blocked"


@pytest.mark.parametrize("cid", [*ORIGINAL_CLASSIFIERS, *NEW_CLASSIFIERS, *NEW_IN_THIS_DEPLOY, *REWRITTEN_BY_R4])
def test_the_question_is_stored_as_one_line_of_text_exactly_as_approved(cid):
    document = json.loads((DIRECTORY / f"{cid}.json").read_text())
    assert "\n" not in document["question"] and document["question"].startswith("Should this source become a reference for")


def test_machines_on_machines_replaces_the_papyrus_classifier_and_leaves_it_in_the_catalog():
    criteria, definition = committed()
    assert RETIRED not in definition["order"] and "machines-on-machines-relevance" in definition["order"]
    item = next(c for c in criteria if c.classifier_id == "machines-on-machines-relevance")
    assert item.name == "Machines on Machines relevance"
    assert "Papyrus" not in item.question and "Machines on Machines" in item.question
    s = catalog_after_r4(criteria, definition)
    steps = plan_classifiers(s, criteria)
    apply_plan(s, criteria, steps, plan_definition(s, criteria, steps, definition))
    assert s.classifier(RETIRED)["revision"] == 2 and s.classifier(RETIRED)["config"] == RETIRED_CONFIG
