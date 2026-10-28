"""Catalog rows outside the head: one create-only record per key, and the read-only legacy layer."""

from .catalog_cold_rows import ColdRows, Legacy, in_review, is_evictable
from .s3_journal_test import ConditionalS3, S3Error, journal


def cold():
    return ColdRows(journal(name="console").payloads)


def test_a_record_repeats_harmlessly_and_a_different_content_at_the_same_key_is_refused():
    rows = cold()
    assert rows.put(ColdRows.job("a"), 1, {"row": 1})
    assert rows.put(ColdRows.job("a"), 1, {"row": 1})
    assert not rows.put(ColdRows.job("a"), 1, {"row": 2})
    assert ColdRows(rows.payloads).get(ColdRows.job("a"), 1) == {"row": 1}


def test_a_missing_record_reads_as_none_and_is_never_remembered_as_missing():
    rows = cold()
    assert rows.get(ColdRows.item("run", "x"), 1) is None
    ColdRows(rows.payloads).put(ColdRows.item("run", "x"), 1, {"row": "later"})
    assert rows.get(ColdRows.item("run", "x"), 1) == {"row": "later"}


class Denied(ConditionalS3):
    """S3 refusing reads of missing keys with AccessDenied, as it does for a principal without ListBucket."""

    def get_object(self, **request):
        try:
            return super().get_object(**request)
        except S3Error as error:
            if error.response["Error"]["Code"] == "NoSuchKey":
                raise S3Error("AccessDenied") from error
            raise


def test_access_denied_on_a_lookup_is_an_error_and_never_taken_for_a_missing_record():
    import pytest

    from .record_lake import RecordUnavailable

    rows = ColdRows(journal(Denied(), name="console").payloads)
    with pytest.raises(RecordUnavailable):
        rows.get(ColdRows.job("a"), 1)
    rows.put(ColdRows.job("a"), 1, {"row": 1})
    assert ColdRows(rows.payloads).get(ColdRows.job("a"), 1) == {"row": 1}


def test_any_other_read_failure_is_raised_and_never_taken_for_a_missing_record():
    import pytest

    from .record_lake import RecordUnavailable

    class Broken(ConditionalS3):
        def get_object(self, **request):
            raise S3Error("InvalidObjectState")

    with pytest.raises(RecordUnavailable):
        ColdRows(journal(Broken(), name="console").payloads).get(ColdRows.job("a"), 1)


def test_keys_name_a_row_by_identity_and_never_mix_runs_lists_or_items():
    kinds = {
        ColdRows.run_jobs("r1"),
        ColdRows.run_jobs("r2"),
        ColdRows.job("r1"),
        ColdRows.request("r1", "q"),
        ColdRows.request("r1", "q2"),
        ColdRows.run_items("r1"),
        ColdRows.item("r1", "i"),
        ColdRows.item("r2", "i"),
        ColdRows.list_revisions("l"),
        ColdRows.revision("l", "i"),
    }
    assert len(kinds) == 10


def test_the_legacy_index_numbers_each_runs_jobs_and_items_and_each_lists_revisions_in_table_order():
    job = {"run_id": "r", "request_id": "q", "status": "completed"}
    value = {
        "snapshot": {
            "tables": {
                "web_jobs": [{**job, "id": "j1"}, {**job, "id": "j2", "request_id": "q2"}, {**job, "id": "k", "run_id": "s"}],
                "web_items": [{"run_id": "r", "id": "i1"}, {"run_id": "r", "id": "i2"}],
                "list_item_revisions": [
                    {"list_id": "l", "id": "i1", "revision": 1},
                    {"list_id": "l", "id": "i1", "revision": 2},
                ],
            }
        },
        "claims": {"j1": {"owner": "o", "fence": 1}},
        "actors": {},
    }
    legacy = Legacy(value)
    assert legacy.job_order["r", 2] == "j2" and legacy.job_counts == {"r": 2, "s": 1}
    assert legacy.jobs["j1"]["claim"] == {"owner": "o", "fence": 1} and legacy.requests["r", "q2"] == "j2"
    assert legacy.item_order["r", 2] == "i2" and legacy.latest["l", "i1"] == 2


def test_only_a_running_job_must_stay_in_the_head():
    assert not is_evictable("web_jobs", {"status": "running"})
    assert is_evictable("web_jobs", {"status": "pending"}) and is_evictable("web_jobs", {"status": "failed"})
    waiting = {"prediction": '{"label":"yes"}', "reviewed": 0}
    assert in_review(waiting) and is_evictable("web_items", waiting) and is_evictable("list_item_revisions", {})
