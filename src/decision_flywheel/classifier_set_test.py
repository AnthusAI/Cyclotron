"""A store's classifier set can grow and shrink without losing what the other classifiers learned."""

from dataclasses import replace

import pytest

from .embedded_cyclotron import ClassifierSpec, Cyclotron
from .embedded_cyclotron_test import DEFINITION, RELEVANT, Model, item, run

SPORTS = ClassifierSpec("sports", ("include", "exclude"), "Does this belong in Sports?", positive_label="include")
BUSINESS = ClassifierSpec("business", ("include", "exclude"), "Does this belong in Business?", positive_label="include")
SPORTS_AND_RELEVANT = replace(DEFINITION, classifiers=(RELEVANT, SPORTS))
ALL_THREE = replace(DEFINITION, classifiers=(RELEVANT, SPORTS, BUSINESS))
RELEVANT_AND_BUSINESS = replace(DEFINITION, classifiers=(RELEVANT, BUSINESS))


def label_relevant(cyclotron, identifier="reviewed-item"):
    decision = run(cyclotron.decide(item(identifier)))
    run(
        cyclotron.review(
            decision.decision_id, "include", classifier="relevant", selected_by="reviewer", review_id=f"r-{identifier}"
        )
    )
    return decision


def test_a_classifier_added_to_an_existing_store_decides_unfitted_with_its_seed_rubric(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        result = run(
            cyclotron.add_classifiers([SPORTS], added_by="editor-1", seed_rubrics={"sports": "Sports means games and athletes."})
        )
        assert result["added"] == ["sports"] and result["reactivated"] == [] and result["classifiers"] == ["relevant", "sports"]
        decision = run(cyclotron.decide(item("after-add")))
        assert set(decision.results) == {"relevant", "sports"}
        assert cyclotron.wheels["sports"].active.config.rubric == "Sports means games and athletes."
        assert cyclotron.status("sports").alignment.labels == 0
        assert cyclotron.definition.classifiers == (RELEVANT, SPORTS)
        assert [change["by"] for change in cyclotron.classifier_set_changes()] == ["editor-1"]


def test_existing_classifiers_keep_their_labels_and_rubrics_when_a_classifier_is_added(tmp_path):
    with Cyclotron.open(
        tmp_path / "c", DEFINITION, Model(), seed_rubrics={"relevant": "Relevant means on our beat."}
    ) as cyclotron:
        label_relevant(cyclotron)
        rubric_before = cyclotron.wheels["relevant"].active.config.rubric
        run(cyclotron.add_classifiers([SPORTS], added_by="editor-1"))
        assert cyclotron.wheels["relevant"].active.config.rubric == rubric_before == "Relevant means on our beat."
        assert [row["classifier"] for row in cyclotron.labels()] == ["relevant"]


def test_a_new_classifier_learns_only_from_reviews_made_after_it_was_added(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        earlier = label_relevant(cyclotron)
        run(cyclotron.add_classifiers([SPORTS], added_by="editor-1"))
        with pytest.raises(ValueError, match="before that classifier was added"):
            run(cyclotron.review(earlier.decision_id, "include", classifier="sports", selected_by="reviewer"))
        later = run(cyclotron.decide(item("later-item")))
        run(cyclotron.review(later.decision_id, "include", classifier="sports", selected_by="reviewer", review_id="sports-1"))
        assert [(row["classifier"], row["item"].id) for row in cyclotron.labels()] == [
            ("relevant", "reviewed-item"),
            ("sports", "later-item"),
        ]


def test_a_retired_classifier_stops_deciding_but_keeps_its_history_and_labels(tmp_path):
    with Cyclotron.open(tmp_path / "c", SPORTS_AND_RELEVANT, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("before-retire")))
        run(cyclotron.review(decision.decision_id, "include", classifier="sports", selected_by="reviewer", review_id="sports-1"))
        run(
            cyclotron.review(
                decision.decision_id, "include", classifier="relevant", selected_by="reviewer", review_id="relevant-1"
            )
        )
        result = run(cyclotron.retire_classifiers(["sports"], retired_by="editor-2"))
        assert result["retired"] == ["sports"] and result["classifiers"] == ["relevant"]
        assert cyclotron.retired_classifiers() == ["sports"]
        assert set(run(cyclotron.decide(item("after-retire"))).results) == {"relevant"}
        assert cyclotron.decision(decision.decision_id).results["sports"].label in ("include", "exclude")
        assert [row["classifier"] for row in cyclotron.labels()] == ["relevant"]
        assert [row["classifier"] for row in cyclotron.labels(include_retired=True)] == ["relevant", "sports"]
        assert cyclotron.decision_log()[0]["reviews"]["sports"]["label"] == "include"
        with pytest.raises(ValueError, match="retired"):
            run(cyclotron.review(decision.decision_id, "exclude", classifier="sports", selected_by="reviewer"))
        with pytest.raises(ValueError, match="retired"):
            run(cyclotron.review(decision.decision_id, labels={"sports": "exclude"}, selected_by="reviewer"))
        with pytest.raises(ValueError, match="retired"):
            run(cyclotron.undo_review(decision.decision_id, classifier="sports"))
        assert [change["kind"] for change in cyclotron.classifier_set_changes()] == ["retired"]
        assert cyclotron.classifier_set_changes()[0]["by"] == "editor-2"


def test_a_store_reopens_with_the_changed_definition_after_additions_and_retirements(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), seed_rubrics={"relevant": "Seeded."}) as cyclotron:
        label_relevant(cyclotron)
        run(cyclotron.add_classifiers([SPORTS, BUSINESS], added_by="editor-1", seed_rubrics={"business": "Business rubric."}))
        run(cyclotron.retire_classifiers(["sports"], retired_by="editor-2"))
    with Cyclotron.open(tmp_path / "c", RELEVANT_AND_BUSINESS, Model()) as reopened:
        assert list(reopened.wheels) == ["relevant", "business"]
        assert reopened.wheels["business"].active.config.rubric == "Business rubric."
        assert reopened.wheels["relevant"].active.config.rubric == "Seeded."
        assert reopened.retired_classifiers() == ["sports"]
        assert [row["item"].id for row in reopened.labels()] == ["reviewed-item"]
        assert [change["kind"] for change in reopened.classifier_set_changes()] == ["added", "retired"]
        assert set(run(reopened.decide(item("fresh"))).results) == {"relevant", "business"}
    reordered = replace(DEFINITION, classifiers=(BUSINESS, RELEVANT))
    with Cyclotron.open(tmp_path / "c", reordered, Model()) as reopened_in_other_order:
        assert set(reopened_in_other_order.wheels) == {"relevant", "business"}


def test_reopening_with_a_definition_missing_a_recorded_retirement_is_refused(tmp_path):
    with Cyclotron.open(tmp_path / "c", SPORTS_AND_RELEVANT, Model()) as cyclotron:
        run(cyclotron.retire_classifiers(["sports"], retired_by="editor-2"))
    with pytest.raises(ValueError, match="different cyclotron definition: it retired classifier 'sports'.*editor-2"):
        Cyclotron.open(tmp_path / "c", SPORTS_AND_RELEVANT, Model())


def test_reopening_with_a_definition_missing_a_recorded_addition_is_refused(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        run(cyclotron.add_classifiers([SPORTS], added_by="editor-1"))
    with pytest.raises(ValueError, match="different cyclotron definition: it added classifier 'sports'.*editor-1"):
        Cyclotron.open(tmp_path / "c", DEFINITION, Model())
    with pytest.raises(ValueError, match="different cyclotron definition"):
        Cyclotron.open(tmp_path / "c", ALL_THREE, Model())


def test_a_retired_classifier_added_again_returns_with_the_history_it_had(tmp_path):
    with Cyclotron.open(tmp_path / "c", SPORTS_AND_RELEVANT, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("sports-item")))
        run(cyclotron.review(decision.decision_id, "include", classifier="sports", selected_by="reviewer", review_id="sports-1"))
        version_before = cyclotron.versions["sports"].version
        run(cyclotron.retire_classifiers(["sports"], retired_by="editor-2"))
        result = run(cyclotron.add_classifiers([SPORTS], added_by="editor-3"))
        assert result["reactivated"] == ["sports"] and result["added"] == []
        assert cyclotron.retired_classifiers() == []
        assert cyclotron.versions["sports"].version == version_before
        assert [(row["classifier"], row["review_id"]) for row in cyclotron.labels()] == [("sports", "sports-1")]
        assert set(run(cyclotron.decide(item("another"))).results) == {"relevant", "sports"}
    with Cyclotron.open(tmp_path / "c", SPORTS_AND_RELEVANT, Model()) as reopened:
        assert [row["review_id"] for row in reopened.labels()] == ["sports-1"]


def test_a_retired_classifier_cannot_return_with_a_different_specification(tmp_path):
    with Cyclotron.open(tmp_path / "c", SPORTS_AND_RELEVANT, Model()) as cyclotron:
        run(cyclotron.retire_classifiers(["sports"], retired_by="editor-2"))
        changed = ClassifierSpec("sports", ("yes", "no"), "Sports?")
        with pytest.raises(ValueError, match="retired with a different specification"):
            run(cyclotron.add_classifiers([changed], added_by="editor-3"))
        assert cyclotron.retired_classifiers() == ["sports"]


def test_adding_and_retiring_are_idempotent(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        run(cyclotron.add_classifiers([SPORTS], added_by="editor-1"))
        again = run(cyclotron.add_classifiers([SPORTS], added_by="editor-1"))
        assert again["added"] == [] and again["already_active"] == ["sports"]
        run(cyclotron.retire_classifiers(["sports"], retired_by="editor-2"))
        repeated = run(cyclotron.retire_classifiers(["sports"], retired_by="editor-2"))
        assert repeated["retired"] == [] and repeated["already_retired"] == ["sports"]
        assert [change["kind"] for change in cyclotron.classifier_set_changes()] == ["added", "retired"]


def test_unknown_duplicate_and_unattributed_requests_are_refused_without_changing_the_store(tmp_path):
    with Cyclotron.open(tmp_path / "c", SPORTS_AND_RELEVANT, Model()) as cyclotron:
        with pytest.raises(ValueError, match="not in this cyclotron: missing"):
            run(cyclotron.retire_classifiers(["sports", "missing"], retired_by="editor"))
        with pytest.raises(ValueError, match="distinct"):
            run(cyclotron.retire_classifiers(["sports", "sports"], retired_by="editor"))
        with pytest.raises(ValueError, match="distinct"):
            run(cyclotron.add_classifiers([BUSINESS, BUSINESS], added_by="editor"))
        with pytest.raises(ValueError, match="retired_by"):
            run(cyclotron.retire_classifiers(["sports"], retired_by="  "))
        with pytest.raises(ValueError, match="added_by"):
            run(cyclotron.add_classifiers([BUSINESS], added_by=""))
        with pytest.raises(ValueError, match="already active with a different specification"):
            run(cyclotron.add_classifiers([ClassifierSpec("sports", ("a", "b"), "Other?")], added_by="editor"))
        with pytest.raises(ValueError, match="seed_rubrics"):
            run(cyclotron.add_classifiers([BUSINESS], added_by="editor", seed_rubrics={"sports": "x"}))
        with pytest.raises(ValueError, match="at least one active classifier"):
            run(cyclotron.retire_classifiers(["sports", "relevant"], retired_by="editor"))
        assert list(cyclotron.wheels) == ["relevant", "sports"] and cyclotron.classifier_set_changes() == []


def test_retiring_a_classifier_drops_its_queued_learning_pass(tmp_path):
    from .flywheel_test import agent

    with Cyclotron.open(tmp_path / "c", SPORTS_AND_RELEVANT, Model(), agent([]), learning="deferred") as cyclotron:
        decision = run(cyclotron.decide(item("queued")))
        run(cyclotron.review(decision.decision_id, "include", classifier="sports", selected_by="reviewer"))
        assert cyclotron.pending_learning() == ["sports"]
        run(cyclotron.retire_classifiers(["sports"], retired_by="editor"))
        assert cyclotron.pending_learning() == []
        assert run(cyclotron.learn()) == []


def test_a_snapshot_keeps_a_retired_classifiers_store_and_restores_it(tmp_path):
    with Cyclotron.open(tmp_path / "c", SPORTS_AND_RELEVANT, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("kept")))
        run(cyclotron.review(decision.decision_id, "include", classifier="sports", selected_by="reviewer", review_id="sports-1"))
        run(cyclotron.retire_classifiers(["sports"], retired_by="editor"))
        manifest = cyclotron.snapshot(tmp_path / "archive.tar.gz")
        assert len(manifest["files"]) == 4
    Cyclotron.restore(tmp_path / "archive.tar.gz", tmp_path / "restored")
    with Cyclotron.open(tmp_path / "restored", replace(DEFINITION, classifiers=(RELEVANT,)), Model()) as restored:
        assert restored.retired_classifiers() == ["sports"]
        run(restored.add_classifiers([SPORTS], added_by="editor-2"))
        assert [row["review_id"] for row in restored.labels()] == ["sports-1"]


def test_cloud_storage_extends_and_retires_classifiers_and_replays_them_on_reopen(monkeypatch):
    from .s3_embedded_storage import S3CyclotronStores
    from .s3_journal_test import ConditionalS3, journal

    client = ConditionalS3()

    def stores():
        return S3CyclotronStores(
            journal(client, name="set-root"),
            max_records=10000,
            journal_for=lambda domain, name: journal(client, name=domain + "-" + name),
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("cloud embedded opened SQLite or created a local store")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    model = Model()
    with Cyclotron.open(None, DEFINITION, model, storage=stores()) as cyclotron:
        label_relevant(cyclotron)
        run(cyclotron.add_classifiers([SPORTS, BUSINESS], added_by="editor-1", seed_rubrics={"sports": "Cloud sports."}))
        run(cyclotron.retire_classifiers(["business"], retired_by="editor-2"))
        decision = run(cyclotron.decide(item("cloud-item")))
        run(cyclotron.review(decision.decision_id, "include", classifier="sports", selected_by="reviewer", review_id="cloud-1"))
        run(cyclotron.add_classifiers([BUSINESS], added_by="editor-3"))
        run(cyclotron.retire_classifiers(["business"], retired_by="editor-4"))
    with Cyclotron.open(None, SPORTS_AND_RELEVANT, model, storage=stores()) as reopened:
        assert list(reopened.wheels) == ["relevant", "sports"]
        assert reopened.wheels["sports"].active.config.rubric == "Cloud sports."
        assert reopened.retired_classifiers() == ["business"]
        assert [row["review_id"] for row in reopened.labels()] == ["r-reviewed-item", "cloud-1"]
        run(reopened.add_classifiers([BUSINESS], added_by="editor-5"))
        assert list(reopened.wheels) == ["relevant", "sports", "business"]
    with pytest.raises(ValueError, match="different cyclotron definition"):
        Cyclotron.open(None, SPORTS_AND_RELEVANT, model, storage=stores())
