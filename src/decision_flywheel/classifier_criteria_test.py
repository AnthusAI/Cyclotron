"""Criteria files carry the question, its doctrine source and its editor approval into the catalog."""

import copy

import pytest

from .classifier_criteria import (
    DRAFT_SUFFIX,
    SCHEMA,
    apply_plan,
    parse,
    plan_classifiers,
    plan_definition,
    validate_provenance,
)
from .s3_console_storage_test import store

LABELS = [{"label": "include", "role": "positive"}, {"label": "exclude", "role": "negative"}]
DEFINITION = {
    "id": "screen-2",
    "name": "Screen",
    "description": "Final.",
    "draft_description": "Draft.",
    "order": ["a-rel", "b-rel"],
    "settings_from": {"id": "screen", "revision": 1},
    "supersedes": "screen",
}


def document(identifier, *, approved=True, question="New question?"):
    approval = (
        {"status": "approved", "approved_by": "Ryan", "approved_on": "2026-10-22"}
        if approved
        else {"status": "pending", "note": "waiting"}
    )
    return {
        "schema": SCHEMA,
        "classifier": {"id": identifier, "name": identifier.upper()},
        "labels": LABELS,
        "rubric": "",
        "question": question,
        "previous": {"revision": 1, "question": "Old question?"},
        "doctrine": {"summary": "Doctrine", "sources": [{"kind": "repo", "repo": "x/y", "path": "d.yml", "commit": "abc"}]},
        "approval": approval,
    }


def seeded(*, approved=(True, True)):
    s = store()
    criteria = []
    for identifier, ok in zip(("a-rel", "b-rel"), approved):
        criteria.append(parse(document(identifier, approved=ok), f"infra/lab/criteria/{identifier}.json"))
        s.save_classifier(identifier, identifier.upper(), {"classes": LABELS, "question": "Old question?", "rubric": ""})
    s.save_cyclotron_definition(
        "screen", "Screen", [{"id": "a-rel", "revision": 1}, {"id": "b-rel", "revision": 1}], {"seed": "s"}
    )
    return s, criteria


def test_an_approved_file_becomes_a_new_revision_that_carries_its_source_and_approval():
    s, criteria = seeded()
    steps = plan_classifiers(s, criteria)
    assert [(x.action, x.live_revision) for x in steps] == [("save", 1), ("save", 1)]
    result = apply_plan(s, criteria, steps, plan_definition(s, criteria, steps, DEFINITION))
    assert result["classifiers"] == {"a-rel": 2, "b-rel": 2}
    saved = s.classifier("a-rel")
    assert saved["revision"] == 2 and saved["config"]["question"] == "New question?"
    block = saved["config"]["criteria"]
    assert block["approval"]["approved_by"] == "Ryan" and block["source"]["sources"][0]["commit"] == "abc"
    assert s.classifier("a-rel", 1)["config"]["question"] == "Old question?"


def test_applying_twice_changes_nothing_further():
    s, criteria = seeded()
    steps = plan_classifiers(s, criteria)
    apply_plan(s, criteria, steps, plan_definition(s, criteria, steps, DEFINITION))
    again = plan_classifiers(s, criteria)
    assert [x.action for x in again] == ["current", "current"]
    definition = plan_definition(s, criteria, again, DEFINITION)
    assert definition.action == "current"
    assert apply_plan(s, criteria, again, definition) == {"classifiers": {}, "definition": None}
    assert s.classifier("a-rel")["revision"] == 2


def test_the_successor_definition_pins_the_new_revisions_and_leaves_the_pinned_one_alone():
    s, criteria = seeded()
    steps = plan_classifiers(s, criteria)
    apply_plan(s, criteria, steps, plan_definition(s, criteria, steps, DEFINITION))
    successor = s.cyclotron_definition("screen-2")
    assert successor["classifiers"] == [{"id": "a-rel", "revision": 2}, {"id": "b-rel", "revision": 2}]
    assert successor["name"] == "Screen" and successor["settings"] == {"seed": "s", "description": "Final."}
    assert s.cyclotron_definition("screen", 1)["classifiers"] == [{"id": "a-rel", "revision": 1}, {"id": "b-rel", "revision": 1}]


def test_pending_criteria_are_held_never_saved_and_mark_the_definition_as_a_draft():
    s, criteria = seeded(approved=(True, False))
    steps = plan_classifiers(s, criteria)
    assert [x.action for x in steps] == ["save", "hold"]
    definition = plan_definition(s, criteria, steps, DEFINITION)
    assert definition.name == "Screen" + DRAFT_SUFFIX and definition.settings["description"] == "Draft."
    assert dict(definition.classifiers) == {"a-rel": 2, "b-rel": 1}
    apply_plan(s, criteria, steps, definition)
    assert s.classifier("b-rel")["revision"] == 1


def test_a_live_classifier_that_matches_neither_text_blocks_the_whole_change():
    s, criteria = seeded()
    s.save_classifier("a-rel", "A-REL", {"classes": LABELS, "question": "Edited by hand?", "rubric": ""})
    steps = plan_classifiers(s, criteria)
    assert steps[0].action == "drift"
    definition = plan_definition(s, criteria, steps, DEFINITION)
    assert definition.action == "blocked"
    with pytest.raises(ValueError, match="drifted"):
        apply_plan(s, criteria, steps, definition)
    assert s.classifier("b-rel")["revision"] == 1


def test_an_unapproved_file_cannot_be_forced_through_apply():
    s, criteria = seeded(approved=(False, True))
    steps = plan_classifiers(s, criteria)
    forced = [type(steps[0])("save", steps[0].classifier_id, 1), steps[1]]
    with pytest.raises(ValueError, match="not approved"):
        apply_plan(s, criteria, forced, plan_definition(s, criteria, steps, DEFINITION))


def test_provenance_requires_a_named_approver_and_date_for_an_approval():
    good = parse(document("a-rel"), "a-rel.json").provenance()
    assert validate_provenance(good) == good
    for mutate in (
        lambda b: b["approval"].pop("approved_by"),
        lambda b: b["approval"].update(approved_on="yesterday"),
        lambda b: b["approval"].update(status="maybe"),
        lambda b: b["source"].update(sources=[]),
        lambda b: b.pop("file"),
    ):
        bad = copy.deepcopy(good)
        mutate(bad)
        with pytest.raises(ValueError):
            validate_provenance(bad)


def test_the_catalog_refuses_a_classifier_whose_criteria_block_is_malformed():
    s = store()
    config = {"classes": LABELS, "question": "Q?", "rubric": "", "criteria": {"approval": {"status": "approved"}}}
    with pytest.raises(ValueError):
        s.save_classifier("a-rel", "A", config)


def test_a_file_must_be_named_for_its_classifier_and_carry_a_previous_revision():
    with pytest.raises(ValueError, match="file name"):
        parse(document("a-rel"), "infra/lab/criteria/other.json")
    broken = document("a-rel")
    broken["previous"] = {"question": "Old?"}
    with pytest.raises(ValueError, match="previous revision"):
        parse(broken, "a-rel.json")
