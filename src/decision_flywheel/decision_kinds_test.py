from decision_flywheel.decision_kinds import DECISION_KINDS, WRITTEN, is_decision, same_kind


def test_a_decision_job_is_read_under_either_its_neutral_or_its_publication_kind():
    assert is_decision("decision") and is_decision("publication")
    assert not is_decision("label")
    assert WRITTEN in DECISION_KINDS


def test_a_replayed_request_matches_a_stored_job_of_the_other_decision_kind_but_no_other_kind():
    assert same_kind("publication", "decision") and same_kind("decision", "publication")
    assert same_kind("label", "label")
    assert not same_kind("publication", "label")


def test_new_decision_jobs_are_stored_under_the_neutral_kind():
    assert WRITTEN == "decision"
