"""Completed job results live in the record lake; the catalog keeps small job control state.

Runs the same behaviour over memory, local-file and synthetic-S3 lakes.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from .batch_storage import StorageScope
from .file_journal import LocalFileJournal
from .journal import JournalUncertain, encode
from .record_lake import Coverage, FileRecordLake, MemoryRecordLake, S3RecordLake, coverage_in
from .record_lake_contract_test import ListingS3
from .s3_console_storage import RESULT_REF, RESULTS_DOMAIN, RESULTS_KIND, S3ConsoleStorage, result_ref
from .s3_journal_test import journal as s3_journal
from .storage_payloads import FilePayloads

SCOPE = StorageScope("account-a", "workspace-a")
LIMIT = 100000


def decision_result(n):
    """About the size of a real publication result: a response plus a status snapshot."""
    snapshot = {"decisions": [{"item": f"item-{i}", "label": "science", "why": "w" * 60} for i in range(40)], "n": n}
    return {"response": {"label": "science", "evidence": "e" * 900, "n": n}, "statusSnapshot": snapshot, "finished": False}


@pytest.fixture(params=["memory", "file", "s3"])
def world(request, tmp_path):
    fake = ListingS3()
    memory_store = {}

    class World:
        backend = request.param

        def journal(self):
            if request.param == "s3":
                return s3_journal(fake, SCOPE, "console")
            payloads = FilePayloads(tmp_path, SCOPE, max_bytes=1000000)
            return LocalFileJournal(payloads, "console", "catalog", max_bytes=1000000, max_commits=1000)

        def lake(self):
            if request.param == "memory":
                return MemoryRecordLake(SCOPE, RESULTS_DOMAIN, RESULTS_KIND, max_record_bytes=LIMIT, store=memory_store)
            if request.param == "file":
                return FileRecordLake(
                    FilePayloads(tmp_path, SCOPE, max_bytes=1000000), RESULTS_DOMAIN, RESULTS_KIND, max_record_bytes=LIMIT
                )
            return S3RecordLake(self.journal().payloads, RESULTS_DOMAIN, RESULTS_KIND, max_record_bytes=LIMIT)

        def store(self, *, lake=True, initialize=False):
            result = S3ConsoleStorage(self.journal(), max_records=10000, results=self.lake() if lake else None)
            if initialize:
                result.initialize()
            return result

    return World()


def catalog_bytes(s):
    return len(encode(s.journal.read().value))


def complete(s, run_id, request_id, result, status="completed"):
    s.command(run_id, request_id, "publication", {"publication": "p", "bindingRevision": 1, "request": request_id})
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner)
    s.finish_command(claimed["id"], status, result, owner=owner, fence=claimed["claim_fence"])
    return claimed["id"]


def test_a_completed_result_goes_to_the_lake_and_the_catalog_row_holds_only_a_reference(world):
    s = world.store(initialize=True)
    run = s.create_run("Live", "live", {})
    job_id = complete(s, run["id"], "r1", decision_result(1))
    row = next(r for r in s.journal.read().value["snapshot"]["tables"]["web_jobs"] if r["id"] == job_id)
    ref = result_ref(row["result"])
    assert ref is not None and len(row["result"]) < 300 and "evidence" not in row["result"]
    assert world.lake().read(ref["seq"])["result"] == decision_result(1)
    assert coverage_in(s.journal.read().value, world.lake()).covers(ref["seq"])


def test_reads_fetch_the_result_by_reference_and_match_what_was_stored(world):
    s = world.store(initialize=True)
    run = s.create_run("Live", "live", {})
    job_id = complete(s, run["id"], "r1", decision_result(1))
    [job] = s.jobs(run["id"])
    assert job["status"] == "completed" and job["result"] == decision_result(1)
    assert s.job_result(job_id) == decision_result(1)
    assert s.command(run["id"], "r1", "publication", job["payload"])["result"] == decision_result(1)
    # A fresh process with only the journal and lake sees the same thing.
    assert world.store().jobs(run["id"])[0]["result"] == decision_result(1)
    # The cheap listing leaves the lake alone and names the reference instead.
    [bare] = s.jobs(run["id"], results=False)
    assert bare["result"]["schema"] == RESULT_REF and s.job_result(bare["id"]) == decision_result(1)


def test_failed_and_interrupted_results_also_leave_the_catalog(world):
    s = world.store(initialize=True)
    run = s.create_run("Live", "live", {})
    complete(s, run["id"], "r1", {"refusal": {"code": "request_ceiling_reached", "message": "m" * 400}}, status="failed")
    [job] = s.jobs(run["id"])
    assert job["status"] == "failed" and job["result"]["refusal"]["code"] == "request_ceiling_reached"
    assert result_ref(s.journal.read().value["snapshot"]["tables"]["web_jobs"][0]["result"]) is not None


def test_catalog_bytes_per_completed_job_stay_small_and_bounded_unlike_embedded_results(world, capsys):
    def grow(s, count):
        run = s.create_run("Live", "live", {})
        sizes = [catalog_bytes(s)]
        for n in range(count):
            complete(s, run["id"], f"r{n}", decision_result(n))
            sizes.append(catalog_bytes(s))
        return sizes

    embedded = grow(world.store(lake=False, initialize=True), 12)
    # The lake run joins the same catalog; only per-job growth is compared, never absolute size.
    lake_sizes = grow(world.store(), 12)
    per_job_embedded = (embedded[-1] - embedded[0]) / 12
    per_job_lake = (lake_sizes[-1] - lake_sizes[0]) / 12
    print(f"\n[{world.backend}] catalog bytes per completed job: embedded {per_job_embedded:.0f}, lake {per_job_lake:.0f}")
    assert per_job_embedded > 2000
    assert per_job_lake < 700 and per_job_lake < per_job_embedded / 5
    deltas = [b - a for a, b in zip(lake_sizes[1:], lake_sizes[2:])]
    assert max(deltas) - min(deltas) < 60  # growth per job is flat, not accelerating
    assert len(world.lake().sequences()) == 12


def test_an_abandoned_result_write_is_invisible_and_a_retry_adopts_it(world):
    s = world.store(initialize=True)
    run = s.create_run("Live", "live", {})
    s.command(run["id"], "r1", "publication", {"publication": "p"})
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner)
    before = s.journal.read()
    real_commit, attempts = s.journal.commit, []

    def lose_the_head(*args, **kwargs):
        attempts.append(1)
        raise JournalUncertain("synthetic lost head response")

    s.journal.commit = lose_the_head
    with pytest.raises(JournalUncertain):
        s.finish_command(claimed["id"], "completed", decision_result(1), owner=owner, fence=claimed["claim_fence"])
    s.journal.commit = real_commit
    lake = world.lake()
    assert attempts and s.journal.read().value == before.value
    assert lake.orphans(coverage_in(s.journal.read().value, lake)) == [1]
    [job] = s.jobs(run["id"])
    assert job["status"] == "running" and job["result"] is None
    assert not any(coverage_in(s.journal.read().value, lake).covers(n) for n in lake.sequences())
    # Retrying the identical completion adopts the identical page: no skipped number, no second page.
    s.finish_command(claimed["id"], "completed", decision_result(1), owner=owner, fence=claimed["claim_fence"])
    assert lake.sequences() == [1] and coverage_in(s.journal.read().value, lake) == Coverage(1)
    assert s.jobs(run["id"])[0]["result"] == decision_result(1)


def test_a_stale_fence_or_owner_writes_no_record_and_changes_nothing(world):
    s = world.store(initialize=True)
    run = s.create_run("Live", "live", {})
    s.command(run["id"], "r1", "publication", {"publication": "p"})
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner)
    before = s.journal.read().value
    for attempt in (
        {"owner": str(uuid4()), "fence": claimed["claim_fence"]},
        {"owner": owner, "fence": claimed["claim_fence"] + 1},
    ):
        with pytest.raises(ValueError):
            s.finish_command(claimed["id"], "completed", decision_result(1), **attempt)
    with pytest.raises(ValueError):
        s.finish_command(claimed["id"], "completed", decision_result(1))
    assert world.lake().sequences() == [] and s.journal.read().value == before
    assert s.claim_command(owner=str(uuid4())) is None  # one worker at a time


def test_a_second_process_cannot_claim_or_finish_a_job_it_does_not_hold(world):
    s = world.store(initialize=True)
    run = s.create_run("Live", "live", {})
    s.command(run["id"], "r1", "publication", {"publication": "p"})
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner)
    other = world.store()
    assert other.claim_command(owner=str(uuid4())) is None
    with pytest.raises(ValueError):
        other.finish_command(claimed["id"], "completed", decision_result(1), owner=owner, fence=claimed["claim_fence"])
    assert world.lake().sequences() == []


def test_independent_workers_still_claim_a_queued_job_exactly_once(world):
    s = world.store(initialize=True)
    run = s.create_run("Live", "live", {})
    s.command(run["id"], "once", "publication", {"publication": "p"})
    gate = threading.Barrier(4)

    def claim(_):
        worker = world.store()
        gate.wait()
        return worker.claim_command(owner=str(uuid4()))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    assert sum(result is not None for result in results) == 1


def test_finishing_twice_with_the_same_result_is_idempotent_and_a_different_one_is_refused(world):
    s = world.store(initialize=True)
    run = s.create_run("Live", "live", {})
    s.command(run["id"], "r1", "publication", {"publication": "p"})
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner)
    done = dict(owner=owner, fence=claimed["claim_fence"])
    s.finish_command(claimed["id"], "completed", decision_result(1), **done)
    revision = s.journal.read().revision
    s.finish_command(claimed["id"], "completed", decision_result(1), **done)
    assert s.journal.read().revision == revision and world.lake().sequences() == [1]
    with pytest.raises(ValueError):
        s.finish_command(claimed["id"], "completed", decision_result(2), **done)
    assert world.lake().sequences() == [1]


def test_finishing_inside_a_transaction_commits_record_and_job_together(world):
    s = world.store(initialize=True)
    run = s.create_run("Live", "live", {})
    s.command(run["id"], "r1", "publication", {"publication": "p"})
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner)
    with s.transaction():
        s.finish_command(claimed["id"], "completed", decision_result(1), owner=owner, fence=claimed["claim_fence"])
    assert s.jobs(run["id"])[0]["result"] == decision_result(1)
    assert coverage_in(s.journal.read().value, world.lake()).last == 1


def test_results_embedded_in_an_existing_catalog_keep_reading_and_mix_with_lake_results(world):
    legacy = world.store(lake=False, initialize=True)
    run = legacy.create_run("Live", "live", {})
    complete(legacy, run["id"], "old", decision_result(1))
    assert result_ref(legacy.journal.read().value["snapshot"]["tables"]["web_jobs"][0]["result"]) is None
    s = world.store()
    assert s.jobs(run["id"])[0]["result"] == decision_result(1)
    complete(s, run["id"], "new", decision_result(2))
    jobs = {j["request_id"]: j for j in s.jobs(run["id"])}
    assert jobs["old"]["result"] == decision_result(1) and jobs["new"]["result"] == decision_result(2)
    assert s.job_result(jobs["old"]["id"]) == decision_result(1)
    # Without a lake the embedded result still reads; a lake-held one is refused rather than guessed.
    bare = world.store(lake=False)
    with pytest.raises(ValueError):
        bare.jobs(run["id"])
    assert bare.jobs(run["id"], results=False)[1]["status"] == "completed"


def test_portable_export_is_self_contained_with_results_embedded(world):
    s = world.store(initialize=True)
    run = s.create_run("Live", "live", {})
    complete(s, run["id"], "r1", decision_result(1))
    exported = s.export_storage()
    [row] = exported["tables"]["web_jobs"]
    assert result_ref(row["result"]) is None and '"evidence"' in row["result"]


def test_a_reference_the_catalog_does_not_cover_is_refused(world):
    s = world.store(initialize=True)
    run = s.create_run("Live", "live", {})
    complete(s, run["id"], "r1", decision_result(1))
    record = s.journal.read()
    forged = {**record.value, "records": {}}
    with pytest.raises(ValueError):
        s._validate(forged)


def test_a_copied_record_does_not_pass_as_another_jobs_result(world):
    s = world.store(initialize=True)
    run = s.create_run("Live", "live", {})
    complete(s, run["id"], "r1", decision_result(1))
    complete(s, run["id"], "r2", decision_result(2))
    lake = world.lake()
    value = s.journal.read().value
    # Point the second job at the first job's page, which is covered but belongs to another job.
    rows = value["snapshot"]["tables"]["web_jobs"]
    ref = result_ref(rows[0]["result"])
    rows[1]["result"] = rows[0]["result"]
    with pytest.raises(ValueError):
        s._result_text(value, rows[1])
    assert lake.read(ref["seq"])["jobId"] == rows[0]["id"] != rows[1]["id"]


def test_a_lake_that_is_not_this_catalogs_job_result_lake_is_refused(world):
    j = world.journal()
    with pytest.raises(ValueError):
        S3ConsoleStorage(j, max_records=10, results=MemoryRecordLake(SCOPE, "console", "events", max_record_bytes=100))
    with pytest.raises(ValueError):
        S3ConsoleStorage(
            j,
            max_records=10,
            results=MemoryRecordLake(StorageScope("account-b", "w"), RESULTS_DOMAIN, RESULTS_KIND, max_record_bytes=100),
        )
