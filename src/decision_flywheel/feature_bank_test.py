"""Individual feature hypotheses survive poor trials and wording revisions."""
import sqlite3

from .feature_bank import FeatureBank, probability_diagnostics


QUESTION = {"name": "practical", "instructions": "Is practical evaluation central?", "labels": ["yes", "no"]}


def test_identical_questions_have_stable_ids_and_revisions_keep_their_lineage(tmp_path):
    path = tmp_path / "bank.sqlite"
    db = sqlite3.connect(path)
    bank = FeatureBank(db)
    first = bank.register(QUESTION, rationale="Human feedback", evidence={"train": "hash"})
    assert bank.register(QUESTION, rationale="Rediscovered", evidence={"later": "hash"}) == first
    revised = bank.register({**QUESTION, "instructions": "Is evaluation the main contribution?"},
                            rationale="Narrower measurement", evidence={"train": "hash"})
    assert revised != first
    assert bank.entries()[1 if bank.entries()[0]["id"] == first else 0]["parent_ids"] == [first]
    db.close()
    db = sqlite3.connect(path)
    assert len(FeatureBank(db).entries()) == 2
    db.close()


def test_a_losing_trial_records_signal_without_deleting_or_deploying_the_question():
    db = sqlite3.connect(":memory:")
    bank = FeatureBank(db)
    key = bank.register(QUESTION, rationale="Feedback", evidence={"train": "hash"})
    bank.record(key, {"feedback_fingerprint": "round", "improved": False}, {"scope": "training only"})
    entry = bank.entries()[0]
    assert entry["id"] == key
    assert entry["state"] == "measured"
    assert entry["attempts"][0]["diagnostics"]["scope"] == "training only"
    assert bank.entries(active_tasks=[QUESTION])[0]["state"] == "deployed"
    db.close()


def test_probability_diagnostics_show_per_class_counts_and_missing_answers_without_guessing():
    report = probability_diagnostics(["include", "exclude"], ["yes", "no"],
        [("include", {"yes": .9, "no": .1}), ("include", None), ("exclude", {"yes": .2, "no": .8})])
    assert report["scope"] == "training only; descriptive, not generalization evidence"
    assert report["by_class"]["include"] == {"count": 2, "answered": 1, "missing": 1,
        "mean_probabilities": {"yes": .9, "no": .1}}
    assert report["by_class"]["exclude"]["mean_probabilities"]["yes"] == .2


def test_malformed_probabilities_are_rejected_instead_of_reported_as_signal():
    import pytest
    for probabilities in ({"yes": .9}, {"yes": .9, "no": .9}, {"yes": float("nan"), "no": 0}):
        with pytest.raises(ValueError):
            probability_diagnostics(["include"], ["yes", "no"], [("include", probabilities)])


def test_provider_rounding_is_preserved_using_the_same_tolerance_as_decision_answers():
    report = probability_diagnostics(["include"], ["yes", "no", "unclear"],
                                     [("include", {"yes": .33, "no": .33, "unclear": .33})])
    assert report["by_class"]["include"]["mean_probabilities"] == {"yes": .33, "no": .33, "unclear": .33}
