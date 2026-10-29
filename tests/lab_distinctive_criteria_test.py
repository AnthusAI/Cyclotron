"""Distinctive Mark's own cyclotron: its criteria directory is separate from the shared Anthus Cyclotron's."""

from dataclasses import replace
from pathlib import Path

from decision_flywheel.classifier_criteria import apply_plan, load_definition, load_directory, plan_classifiers, plan_definition
from decision_flywheel.s3_console_storage_test import store

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "infra/lab/criteria-distinctive"
SHARED = ROOT / "infra/lab/criteria"
SETTINGS = {"decisions_model": "jev-1.13.0", "decisions_provider": "jev", "description": "Shared."}


def committed():
    return load_directory(DIRECTORY, relative_to=ROOT), load_definition(DIRECTORY)


def lab_with_the_shared_definition():
    s = store()
    s.save_classifier(
        "anth-us-relevance", "Anth.us relevance", {"classes": [{"label": "include"}, {"label": "exclude"}], "question": "Q?"}
    )
    s.save_cyclotron_definition(
        "shared-source-relevance-r4", "Shared source relevance", [{"id": "anth-us-relevance", "revision": 1}], SETTINGS
    )
    return s


def test_distinctive_mark_has_one_classifier_in_a_cyclotron_of_its_own():
    criteria, definition = committed()
    assert [c.classifier_id for c in criteria] == definition["order"] == ["distinctive-relevance"]
    assert criteria[0].is_new and definition["supersedes"] is None
    shared = load_definition(SHARED)
    assert definition["id"] != shared["id"] and "distinctive-relevance" not in shared["order"]


def test_celebrity_and_brand_trademark_news_is_in_scope_for_distinctive_mark_by_ryans_decision():
    (criteria,), _ = committed()
    assert "Trademark news involving celebrities or well-known brands is in scope" in criteria.question
    assert "clickbait about celebrity filings" not in criteria.question
    assert any("2026-10-29" in source.get("note", "") for source in criteria.doctrine["sources"])


def test_ryan_approved_the_question_on_2026_10_29():
    (criteria,), _ = committed()
    assert criteria.approval == {"status": "approved", "approved_by": "Ryan", "approved_on": "2026-10-29"}


def test_once_approved_a_deploy_creates_the_classifier_and_its_own_definition_with_the_shared_model_settings():
    criteria, definition = committed()
    approved = tuple(
        replace(c, approval={"status": "approved", "approved_by": "Ryan", "approved_on": "2026-10-29"}) for c in criteria
    )
    s = lab_with_the_shared_definition()
    steps = plan_classifiers(s, approved)
    assert [(step.classifier_id, step.action) for step in steps] == [("distinctive-relevance", "save")]
    plan = plan_definition(s, approved, steps, definition)
    assert (plan.action, plan.definition_id, plan.classifiers) == (
        "define",
        "distinctive-mark-relevance",
        (("distinctive-relevance", 1),),
    )
    apply_plan(s, approved, steps, plan)
    live = s.cyclotron_definition("distinctive-mark-relevance")
    assert live["classifiers"] == [{"id": "distinctive-relevance", "revision": 1}]
    assert live["settings"]["decisions_model"] == "jev-1.13.0"
    assert s.cyclotron_definition("shared-source-relevance-r4")["classifiers"] == [{"id": "anth-us-relevance", "revision": 1}]
