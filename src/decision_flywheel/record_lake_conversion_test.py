"""The one-time conversion of embedded history into the record lake.

Every behaviour runs over three worlds: the synthetic S3 client with S3 lakes, local files with file lakes,
and the S3 journal with in-memory lakes. History is first written the OLD way (no lake), then converted.
"""

import json
from hashlib import sha256
from uuid import uuid4

import pytest

from .batch_storage import StorageScope
from .journal import encode
from .record_lake import MemoryRecordLake, coverage_in
from .record_lake_contract_test import ListingS3
from .record_lake_conversion import (
    ConversionMismatch,
    ReadOnlyS3,
    attribute,
    conversion_owner,
    convert_account,
    file_account,
    main,
    s3_account,
)
from .s3_batch_storage import S3BatchStorage
from .s3_console_storage import RESULTS_DOMAIN, RESULTS_KIND, S3ConsoleStorage, result_ref
from .s3_embedded_storage import S3EmbeddedStorage
from .s3_runtime_storage import RuntimeOwnershipError, S3RuntimeStores, events_kind
from .storage_errors import StorageUncertain

SCOPE = StorageScope("account-a", "console")
CLASSIFIER = "classifier-1"
ROOT = "run-1"


class TreeS3(ListingS3):
    """The listing fake plus delimiter listing (the journal names under a coordination prefix)."""

    def list_objects_v2(self, **request):
        if not request.get("Delimiter"):
            return super().list_objects_v2(**request)
        prefix = request["Prefix"]
        names = sorted({k[len(prefix) :].split("/")[0] for k in self.objects if k.startswith(prefix) and "/" in k[len(prefix) :]})
        return {"CommonPrefixes": [{"Prefix": f"{prefix}{n}/"} for n in names], "IsTruncated": False}


class World:
    def __init__(self, backend, tmp_path):
        self.backend, self.tmp_path = backend, tmp_path
        self.client, self.memory = TreeS3(), {}
        if backend == "file":
            self.account = file_account(tmp_path, SCOPE)
        else:
            self.account = s3_account(
                self.client,
                "synthetic-bucket",
                "111111111111",
                "account-a",
            )
        if backend == "memory":
            self.account.events_lake = lambda j: MemoryRecordLake(j.boundary[0], "runtime", events_kind(j), store=self.memory)
            self.account.reply_lake = lambda j, label: MemoryRecordLake(
                j.boundary[0], "shared-batches", f"replies-{label}", store=self.memory
            )
            self.account.results_lake = lambda j: MemoryRecordLake(j.boundary[0], RESULTS_DOMAIN, RESULTS_KIND, store=self.memory)

    # -- naming, as the live code derives it
    @property
    def root_namespace(self):
        return self.account.journal("workspace", ROOT).namespace

    @property
    def hosted(self):
        return sha256(self.root_namespace.encode()).hexdigest()

    @property
    def ledger_name(self):
        return self.hosted

    @property
    def runtime_name(self):
        return sha256((self.hosted + "\0" + CLASSIFIER).encode()).hexdigest()

    # -- the old way: nothing goes to a lake
    def catalog(self, *, lake=False):
        j = self.account.journal("console", "catalog")
        return S3ConsoleStorage(j, results=self.account.results_lake(j) if lake else None)

    def ledger(self, *, lake=False):
        j = self.account.journal("shared-batches", self.ledger_name)
        return S3BatchStorage(j, replies=self.account.reply_lake(j, self.hosted) if lake else None)

    def runtime(self, name=None, *, lake=False):
        j = self.account.journal("runtime", name or self.runtime_name)
        return S3RuntimeStores(j, events_lake=self.account.events_lake(j) if lake else None)

    def seed(self, events=7, replies=3, jobs=3):
        root = S3EmbeddedStorage(self.account.journal("workspace", ROOT))
        root.initialize()
        catalog = self.catalog()
        catalog.initialize()
        run = catalog.create_run("Live", "live", {"classifiers": [{"id": CLASSIFIER, "revision": 1}]})
        self.run_id = run["id"]
        # One catalog commit, so every finished job is still in the catalog head (a later commit moves finished jobs
        # out of the head into their own records, where an embedded result no longer costs anything per commit).
        with catalog.transaction():
            for n in range(jobs):
                self.finish_job(catalog, f"r{n}", {"response": {"label": "yes", "n": n, "pad": "x" * 300}, "finished": False})
        ledger = self.ledger()
        ledger.initialize()
        self.fingerprints = []
        for n in range(replies):
            fp, owner = f"{n:064x}", str(uuid4())
            ledger.reserve(fp, 100, owner)
            ledger.claim(fp, owner)
            ledger.settle(fp, owner, "complete", json.dumps({"reply": n, "text": "雪" + "x" * 500}))
            self.fingerprints.append(fp)
        ledger.reserve(self.fingerprints[0], 100, str(uuid4()), policy="refresh")  # a history row too
        runtime = self.runtime()
        runtime.initialize()
        runtime.acquire(str(uuid4()))
        for n in range(events):
            runtime.runtime_storage.append_event(
                json.dumps({"kind": "step", "n": n, "cycle_number": n // 3 + 1, "pad": "x" * 200})
            )
        runtime.runtime_cache.begin_answer("answer-1", None)
        runtime.runtime_cache.complete_answer("answer-1", '{"label":"yes"}', previous=None)
        runtime.close()
        return self

    def finish_job(self, catalog, request_id, result, status="completed"):
        catalog.command(self.run_id, request_id, "publication", {"publication": "p", "request": request_id})
        owner = str(uuid4())
        claimed = catalog.claim_command(owner=owner)
        catalog.finish_command(claimed["id"], status, result, owner=owner, fence=claimed["claim_fence"])
        return claimed["id"]

    # -- what a reader sees now, through the live code
    def events(self):
        j = self.account.journal("runtime", self.runtime_name)
        store = S3RuntimeStores(j, events_lake=self.account.events_lake(j))
        return store.audit_events()

    def replies(self):
        return self.ledger(lake=True).export()

    def results(self):
        catalog = self.catalog(lake=True)
        return {job["id"]: job["result"] for job in catalog.jobs(self.run_id)}

    def sizes(self):
        return {
            "runtime": len(encode(self.runtime().journal.read().value)),
            "ledger": len(encode(self.ledger().journal.read().value)),
            "catalog": len(encode(self.catalog().journal.read().value)),
        }

    def flip_byte_in_record(self, domain, kind, seq):
        """Alter a stored lake record behind the application's back (they are write-once, so only a fault does this)."""
        if self.backend == "memory":
            key = (f"{SCOPE.key}/records/{domain}/{kind}", seq)
            self.memory[key] = self.memory[key].replace(b"xxxx", b"xxxy", 1)
        elif self.backend == "file":
            path = self.tmp_path / SCOPE.account / SCOPE.workspace / "records" / domain / kind / f"{seq:012d}.json"
            path.write_bytes(path.read_bytes().replace(b"xxxx", b"xxxy", 1))
        else:
            key = f"{SCOPE.key}/records/{domain}/{kind}/{seq:012d}.json"
            body, etag = self.client.objects[key]
            self.client.objects[key] = (body.replace(b"xxxx", b"xxxy", 1), etag)

    def lake_keys(self):
        if self.backend == "memory":
            return sorted(self.memory)
        if self.backend == "file":
            return sorted(
                str(p.relative_to(self.tmp_path))
                for p in (self.tmp_path / SCOPE.account).rglob("*.json")
                if "/records/" in str(p)
            )
        return sorted(k for k in self.client.objects if "/records/" in k)


@pytest.fixture(params=["s3", "file", "memory"])
def world(request, tmp_path):
    return World(request.param, tmp_path).seed()


def statuses(reports):
    return {r.journal.split("/")[0] + ":" + r.kind: r.status for r in reports}


def convert(world, **options):
    return convert_account(world.account, **options)


# --- dry run ---------------------------------------------------------------------------------------------


def test_the_dry_run_reports_each_journal_and_changes_nothing(world):
    before_sizes, before_objects = world.sizes(), dict(world.client.objects)
    reports = convert(world)
    assert statuses(reports) == {
        "runtime:runtime-events": "would-convert",
        "shared-batches:reply-ledger": "would-convert",
        "console:job-results": "would-convert",
    }
    by_kind = {r.kind: r for r in reports}
    assert by_kind["runtime-events"].records == 7
    assert by_kind["reply-ledger"].records == 3  # two still current plus the superseded one in history
    assert by_kind["job-results"].records == 3
    for report in reports:
        assert 0 < report.bytes_after < report.bytes_before and report.lake_bytes > 0 and not report.problems
    assert world.sizes() == before_sizes and world.lake_keys() == []
    if world.backend != "file":
        assert world.client.objects == before_objects


def test_the_dry_run_cannot_write_even_if_it_tried(world):
    if world.backend == "file":
        pytest.skip("S3 client wrapper")
    account = s3_account(
        ReadOnlyS3(world.client),
        "synthetic-bucket",
        "111111111111",
        "account-a",
    )
    calls = len(world.client.calls)
    reports = convert_account(account)
    assert all(r.status == "would-convert" for r in reports)
    assert not [c for kind, c in world.client.calls[calls:] if kind == "put"]
    with pytest.raises(PermissionError):
        ReadOnlyS3(world.client).put_object(Bucket="b", Key="k", Body=b"")


# --- conversion ------------------------------------------------------------------------------------------


def test_apply_moves_every_kind_and_the_live_code_reads_back_the_identical_history(world):
    original = {
        "events": world.events().embedded,
        "replies": world.ledger().export(),
        "results": world.catalog().jobs(world.run_id),
    }
    sizes = world.sizes()
    reports = convert(world, apply=True)
    assert set(statuses(reports).values()) == {"converted"}
    assert all(not r.problems for r in reports)
    # Events: same ids, same order, same bytes; nothing embedded any more.
    audit = world.events()
    assert audit.embedded == [] and audit.stored == original["events"]
    # Replies: every reply (and history reply) is byte-identical when read back through the lake.
    assert world.replies() == original["replies"]
    assert S3BatchStorage.audit_replies(world.ledger(lake=True))[1] == []
    assert world.ledger(lake=True).get(world.fingerprints[1]).payload == original["replies"]["batches"][1][2]
    # Results: the same decoded results, now by reference.
    assert world.results() == {j["id"]: j["result"] for j in original["results"]}
    rows = world.catalog(lake=True).journal.read().value["snapshot"]["tables"]["web_jobs"]
    assert all(result_ref(r["result"]) is not None for r in rows)
    after = world.sizes()
    assert all(after[k] < sizes[k] for k in sizes) and after["runtime"] < sizes["runtime"] / 2
    for report in reports:
        assert report.revision_after > report.revision_before and report.digest_before


def test_a_second_run_converts_nothing_and_writes_nothing(world):
    convert(world, apply=True)
    keys, sizes = world.lake_keys(), world.sizes()
    calls = len(world.client.calls)
    reports = convert(world, apply=True)
    assert set(statuses(reports).values()) == {"nothing-to-convert"}
    assert world.lake_keys() == keys and world.sizes() == sizes
    if world.backend != "file":  # taking and releasing ownership writes heads; no record and no payload of history is written
        assert not [c for kind, c in world.client.calls[calls:] if kind == "put" and "/records/" in c["Key"]]


def test_the_worker_can_take_every_journal_afterwards_and_the_lake_is_never_rewritten(world):
    convert(world, apply=True)
    runtime = world.runtime(lake=True)
    runtime.acquire(str(uuid4()))
    runtime.runtime_storage.append_event(json.dumps({"kind": "after", "cycle_number": 9}))
    assert runtime.runtime_storage.next_cycle_number() == 10
    assert [e["event_id"] for e in runtime.runtime_storage.history(-1)] == list(range(1, 9))
    runtime.close()
    root = S3EmbeddedStorage(world.account.journal("workspace", ROOT))
    root.acquire(str(uuid4()))
    root.release()
    if world.backend != "file":
        for key, versions in world.client.versions.items():
            if "/records/" in key or "/payloads/" in key:
                assert len(versions) == 1  # write-once: nothing in the lake or payload store was ever replaced


def test_only_the_named_journal_is_converted(world):
    reports = convert(world, apply=True, only=("runtime/",))
    assert statuses(reports) == {"runtime:runtime-events": "converted"}
    assert world.ledger().audit_replies()[1] and world.catalog().audit_results()[1]


def test_a_cycle_number_mark_is_stored_so_numbering_never_scans_history(world):
    convert(world, apply=True)
    assert json.loads(world.runtime(lake=True).journal.read().value["data"]["state"]["cycle-number-high-water"]) == 3


# --- ownership -------------------------------------------------------------------------------------------


def test_a_runtime_held_by_a_worker_is_skipped_and_reported_busy(world):
    worker = world.runtime(lake=True)
    worker.acquire(str(uuid4()))
    reports = convert(world, apply=True)
    assert statuses(reports)["runtime:runtime-events"] == "busy"
    assert statuses(reports)["shared-batches:reply-ledger"] == "converted"  # independent journals still convert
    assert len(world.events().embedded) == 7 and world.events().record.value["owner"] == worker.authority[0]
    worker.close()
    assert statuses(convert(world, apply=True))["runtime:runtime-events"] == "converted"


def test_a_workspace_held_by_a_worker_skips_its_ledger_and_runtimes(world):
    root = S3EmbeddedStorage(world.account.journal("workspace", ROOT))
    root.acquire(str(uuid4()))
    reports = convert(world, apply=True)
    assert statuses(reports)["runtime:runtime-events"] == statuses(reports)["shared-batches:reply-ledger"] == "busy"
    assert statuses(reports)["console:job-results"] == "converted"
    assert len(world.events().embedded) == 7 and world.ledger().audit_replies()[1]
    assert S3EmbeddedStorage(world.account.journal("workspace", ROOT)).journal.read().value["owner"] == root.authority[0]
    root.release()
    assert set(statuses(convert(world, apply=True)).values()) <= {"converted", "nothing-to-convert"}


def test_the_dry_run_also_reports_busy(world):
    worker = world.runtime(lake=True)
    worker.acquire(str(uuid4()))
    assert statuses(convert(world))["runtime:runtime-events"] == "busy"


def test_a_catalog_with_a_running_job_is_busy(world):
    catalog = world.catalog()
    catalog.command(world.run_id, "running", "publication", {"publication": "p"})
    claimed = catalog.claim_command(owner=str(uuid4()))
    before = catalog.journal.read().value
    reports = convert(world, apply=True)
    assert statuses(reports)["console:job-results"] == "busy"
    assert catalog.journal.read().value == before and claimed["status"] == "running"


def test_the_conversion_leaves_no_ownership_behind(world):
    convert(world, apply=True)
    assert world.events().record.value["owner"] is None
    assert S3EmbeddedStorage(world.account.journal("workspace", ROOT)).journal.read().value["owner"] is None


# --- interruption and resume -----------------------------------------------------------------------------


class Dead(BaseException):
    """The process stopped. Nothing after this moment runs (no verification, no release)."""


class Faults:
    """Stops the process at an exact step of one journal's conversion, then lets the test resume it."""

    LAKES = {
        "events": ("events_lake", "events-"),
        "replies": ("reply_lake", "replies-"),
        "results": ("results_lake", "job-results"),
    }

    def __init__(self, world):
        self.world, self.dead, self.commits, self.records = world, False, {}, {}
        self.commit_fault = self.record_fault = None
        account, journal_of = world.account, world.account.journal

        def journal(domain, name):
            j = journal_of(domain, name)
            real = j.commit

            def commit(expected, value):
                if self.dead:
                    raise Dead("process is gone")
                count = self.commits[domain] = self.commits.get(domain, 0) + 1
                fault = self.commit_fault
                matches = fault is not None and fault[0] == domain and fault[1] == count
                if matches and fault[2] == "before":
                    self.dead = True
                    raise Dead("stopped before the commit")
                result = real(expected, value)
                if matches and fault[2] == "after":
                    self.dead = True
                    raise Dead("stopped right after the commit landed")
                if matches and fault[2] == "corrupt":
                    fault[3]()
                if matches and fault[2] == "lost":
                    raise StorageUncertain("response lost after the commit landed")
                return result

            j.commit = commit
            return j

        account.journal = journal
        for attr, (_, prefix) in self.LAKES.items():
            self._wrap(attr, prefix)

    def _wrap(self, attr, prefix):
        account = self.world.account
        factory = getattr(account, self.LAKES[attr][0])

        def lake_for(*args):
            lake = factory(*args)
            real = lake._create

            def create(seq, content):
                if self.dead:
                    raise Dead("process is gone")
                count = self.records[prefix] = self.records.get(prefix, 0) + 1
                if self.record_fault == (prefix, count):
                    self.dead = True
                    raise Dead("stopped between two records")
                return real(seq, content)

            lake._create = create
            return lake

        setattr(account, self.LAKES[attr][0], lake_for)

    def disarm(self):
        self.dead, self.commit_fault, self.record_fault = False, None, None


ORIGINAL = {}


def snapshot_original(world):
    return {
        "events": world.events().embedded,
        "replies": world.ledger().export(),
        "results": {j["id"]: j["result"] for j in world.catalog().jobs(world.run_id)},
    }


def assert_fully_converted(world, original):
    audit = world.events()
    assert audit.embedded == [] and audit.stored == original["events"] and audit.record.value["owner"] is None
    assert audit.coverage.skipped == () and audit.coverage.last == 7
    assert world.replies() == original["replies"]
    ledger_coverage = coverage_in(
        world.ledger(lake=True).journal.read().value, world.account.reply_lake(world.ledger().journal, world.hosted)
    )
    assert ledger_coverage.skipped == () and ledger_coverage.last == 3
    assert world.results() == original["results"]
    lake = world.account.results_lake(world.catalog().journal)
    assert coverage_in(world.catalog().journal.read().value, lake).skipped == () and len(world.lake_keys()) == 7 + 3 + 3
    root = S3EmbeddedStorage(world.account.journal("workspace", ROOT))
    assert root.journal.read().value["owner"] is None


STEPS = {
    "events": {
        "mid-records": ("record", ("events-", 3)),
        "before-commit": ("commit", ("runtime", 2, "before")),
        "after-commit": ("commit", ("runtime", 2, "after")),
        "after-taking-ownership": ("commit", ("runtime", 1, "after")),
    },
    "replies": {
        "mid-records": ("record", ("replies-", 3)),
        "before-commit": ("commit", ("shared-batches", 1, "before")),
        "after-commit": ("commit", ("shared-batches", 1, "after")),
    },
    "results": {
        "mid-records": ("record", ("job-results", 2)),
        "before-commit": ("commit", ("console", 1, "before")),
        "after-commit": ("commit", ("console", 1, "after")),
    },
}


@pytest.mark.parametrize("kind,step", [(k, s) for k, steps in STEPS.items() for s in steps])
def test_a_conversion_stopped_at_any_step_resumes_without_converting_anything_twice(world, kind, step):
    original = snapshot_original(world)
    faults = Faults(world)
    where, spec = STEPS[kind][step]
    if where == "record":
        faults.record_fault = spec
    else:
        faults.commit_fault = spec
    with pytest.raises(Dead):
        convert(world, apply=True)
    faults.disarm()
    if step in ("mid-records", "before-commit"):
        # Nothing counted yet: the journal is exactly as it was, and any pages written are orphans nobody can read.
        audit_before = {
            "events": lambda: len(world.events().embedded) == 7,
            "replies": lambda: bool(world.ledger().audit_replies()[1]),
            "results": lambda: len(world.catalog().audit_results()[1]) == 3,
        }[kind]
        assert audit_before()
    reports = convert(world, apply=True)
    assert "mismatch" not in statuses(reports).values() and not [r for r in reports if r.problems]
    assert_fully_converted(world, original)
    # And a further run has nothing left to do.
    assert set(statuses(convert(world, apply=True)).values()) == {"nothing-to-convert"}


@pytest.mark.parametrize("kind,domain", [("events", "runtime"), ("replies", "shared-batches"), ("results", "console")])
def test_a_commit_whose_outcome_is_unknown_is_reported_and_the_rerun_finds_it_landed(world, kind, domain):
    original = snapshot_original(world)
    faults = Faults(world)
    faults.commit_fault = (domain, 2 if kind == "events" else 1, "lost")
    reports = convert(world, apply=True)
    [lost] = [r for r in reports if r.status == "problem"]
    assert any("outcome is unknown" in n for n in lost.notes)
    faults.disarm()
    assert "mismatch" not in statuses(convert(world, apply=True)).values()
    assert_fully_converted(world, original)


def test_a_stale_hold_left_by_a_stopped_conversion_is_resumed_but_a_workers_hold_never_is(world):
    owner = conversion_owner(world.account.journal("console", "catalog").namespace)
    runtime = world.runtime(lake=True)
    runtime.acquire(owner)  # an earlier conversion run stopped holding this
    assert statuses(convert(world))["runtime:runtime-events"] == "would-convert"
    assert statuses(convert(world, apply=True))["runtime:runtime-events"] == "converted"
    assert world.events().record.value["owner"] is None
    other = world.runtime(lake=True)
    other.acquire(str(uuid4()))
    with pytest.raises(RuntimeOwnershipError):
        world.runtime(lake=True).acquire(owner, resume=True)  # resume only ever re-takes the caller's own id


# --- verification ----------------------------------------------------------------------------------------

CORRUPT = {
    "events": ("runtime", lambda w: "events-" + w.runtime_name, "runtime"),
    "replies": ("shared-batches", lambda w: "replies-" + w.hosted, "shared-batches"),
    "results": (RESULTS_DOMAIN, lambda w: RESULTS_KIND, "console"),
}


@pytest.mark.parametrize("kind", ["events", "replies", "results"])
def test_verification_catches_a_record_corrupted_after_the_commit_and_stops_the_run(world, kind):
    faults = Faults(world)
    domain, kind_of, journal_domain = CORRUPT[kind]
    faults.commit_fault = (
        journal_domain,
        2 if kind == "events" else 1,
        "corrupt",
        lambda: world.flip_byte_in_record(domain, kind_of(world), 2),
    )
    seen = []
    with pytest.raises(ConversionMismatch) as error:
        convert(world, apply=True, emit=seen.append)
    assert any(w in str(error.value) for w in ("differs", "bytes", "does not match"))
    assert seen[-1].status == "mismatch"
    # The run stopped: whatever had not been reached is still embedded and untouched.
    later = {"replies": ("events", "results"), "events": ("results",), "results": ()}[kind]
    if "results" in later:
        assert len(world.catalog().audit_results()[1]) == 3
    # Ownership is released even on a mismatch, so a worker is not locked out by a half-finished tool.
    assert S3EmbeddedStorage(world.account.journal("workspace", ROOT)).journal.read().value["owner"] is None


@pytest.mark.parametrize("kind", ["events", "replies", "results"])
def test_a_page_that_does_not_read_back_intact_never_gets_committed(world, kind):
    attr = Faults.LAKES[kind][0]
    factory = getattr(world.account, attr)

    def corrupt_reads(*args):
        lake = factory(*args)
        real = lake._fetch
        lake._fetch = lambda seq: (lambda c: c.replace(b"xxxx", b"xxxy", 1) if seq == 2 and c else c)(real(seq))
        return lake

    setattr(world.account, attr, corrupt_reads)
    before = world.sizes()
    seen = []
    with pytest.raises(ConversionMismatch):
        convert(world, apply=True, emit=seen.append)
    assert seen[-1].status == "mismatch" and "commit abandoned" in seen[-1].problems[0]
    journal_of = {"events": "runtime", "replies": "ledger", "results": "catalog"}[kind]
    assert world.sizes()[journal_of] == before[journal_of]  # that journal's control state never moved
    setattr(world.account, attr, factory)
    audit = {
        "events": lambda: len(world.events().embedded),
        "replies": lambda: len(world.ledger().audit_replies()[1]),
        "results": lambda: len(world.catalog().audit_results()[1]),
    }[kind]
    assert audit() > 0


# --- odd shapes ------------------------------------------------------------------------------------------


def test_lake_events_written_after_deploy_keep_their_ids_and_order_when_the_embedded_ones_move(world):
    original = world.events().embedded
    runtime = world.runtime(lake=True)
    runtime.acquire(str(uuid4()))
    for n in range(3):
        runtime.runtime_storage.append_event(json.dumps({"kind": "new", "n": n, "pad": "x" * 100}))
    runtime.close()
    mixed = world.events()
    assert len(mixed.embedded) == 7 and [i for i, _ in mixed.stored] == [8, 9, 10]
    report = [r for r in convert(world) if r.kind == "runtime-events"][0]
    assert any("copied after the embedded events" in n for n in report.notes)
    assert statuses(convert(world, apply=True))["runtime:runtime-events"] == "converted"
    audit = world.events()
    assert audit.embedded == [] and [i for i, _ in audit.stored] == list(range(1, 11))
    assert audit.stored[:7] == original and json.loads(audit.stored[7][1])["n"] == 0
    assert audit.coverage.skipped == (1, 2, 3)
    worker = world.runtime(lake=True)
    worker.acquire(str(uuid4()))
    assert worker.runtime_storage.append_event(json.dumps({"kind": "later"})) == 11
    worker.close()


def test_more_lake_events_than_the_old_skip_budget_still_convert_with_ids_and_order_kept(world):
    # The old 64-entry skip budget refused this runtime; now every earlier lake record is copied after the embedded ones.
    original = world.events().embedded
    runtime = world.runtime(lake=True)
    runtime.acquire(str(uuid4()))
    for n in range(70):
        runtime.runtime_storage.append_event(json.dumps({"kind": "new", "n": n}))
    runtime.close()
    report = convert(world, apply=True, only=("runtime/",))[0]
    assert report.status == "converted" and report.problems == []
    audit = world.events()
    assert audit.embedded == [] and [i for i, _ in audit.stored] == list(range(1, 78))
    assert audit.stored[:7] == original and [json.loads(p)["n"] for _, p in audit.stored[7:]] == list(range(70))
    assert len(audit.coverage.skipped) == 70


def test_embedded_event_ids_that_are_not_one_to_n_are_refused_rather_than_renumbered(world):
    journal = world.account.journal("runtime", world.runtime_name)
    record = journal.read()
    value = json.loads(json.dumps(record.value))
    value["data"]["events"] = [r for r in value["data"]["events"] if r[0] != 2]
    journal.commit(record, value)
    report = [r for r in convert(world, apply=True) if r.kind == "runtime-events"][0]
    assert report.status == "problem" and "1..n" in report.problems[0]
    assert len(world.events().embedded) == 6


def test_a_result_that_is_not_canonical_json_stays_embedded_and_is_reported(world):
    catalog = world.catalog()
    record = catalog.journal.read()
    value = json.loads(json.dumps(record.value))
    value["snapshot"]["tables"]["web_jobs"][0]["result"] = '{"b": 1,  "a": 2}'
    catalog.journal.commit(record, value)
    report = [r for r in convert(world, apply=True) if r.kind == "job-results"][0]
    assert report.status == "converted" and report.records == 2 and any("not canonical" in n for n in report.notes)
    rows = world.catalog(lake=True).journal.read().value["snapshot"]["tables"]["web_jobs"]
    assert rows[0]["result"] == '{"b": 1,  "a": 2}' and all(result_ref(r["result"]) for r in rows[1:])


def test_a_runtime_with_no_workspace_root_is_a_problem_unless_explicitly_allowed(world):
    stray = "stray-runtime"
    stores = world.runtime(stray)
    stores.initialize()
    stores.acquire(str(uuid4()))
    stores.runtime_storage.append_event(json.dumps({"kind": "step", "pad": "x" * 100}))
    stores.close()
    by_root, loose = attribute(world.account)
    assert [u.name for u in loose] == [stray] and len(by_root[ROOT]) == 2
    report = [r for r in convert(world, apply=True) if r.journal == f"runtime/{stray}"][0]
    assert report.status == "problem" and "--allow-unattributed" in report.problems[0]
    report = [r for r in convert(world, apply=True, allow_unattributed=True) if r.journal == f"runtime/{stray}"][0]
    assert report.status == "converted" and report.records == 1


def test_the_conversion_owner_is_a_fixed_valid_uuid4_per_account():
    from .journal import uuid

    owner = conversion_owner("account-a/console/console/catalog")
    assert uuid(owner) == owner == conversion_owner("account-a/console/console/catalog")
    assert owner != conversion_owner("account-b/console/console/catalog")


# --- the command line ------------------------------------------------------------------------------------


class Session:
    def __init__(self, client, account="111111111111"):
        self.client_object, self.account = client, account

    def client(self, name):
        if name == "sts":
            return type(
                "Sts", (), {"get_caller_identity": lambda _self: {"Account": self.account, "Arn": "arn:aws:iam::1:user/x"}}
            )()
        return self.client_object


ARGS = ["--expect-aws-account", "111111111111", "--books-bucket", "synthetic-bucket", "--cyclotron-account", "account-a"]


@pytest.fixture
def cli():
    return World("s3", None).seed()


def run_cli(world, *extra, account="111111111111"):
    lines = []
    code = main([*ARGS, *extra], session=Session(world.client, account), out=lines.append)
    return code, "\n".join(lines)


def test_the_command_line_is_a_dry_run_unless_apply_is_given(cli):
    puts = len([1 for kind, _ in cli.client.calls if kind == "put"])
    code, out = run_cli(cli)
    assert code == 0 and "DRY RUN" in out and "would-convert" in out and "converted" not in out.replace("would convert", "")
    assert "records=7" in out and "state" in out and "lake +" in out
    assert len([1 for kind, _ in cli.client.calls if kind == "put"]) == puts


def test_the_command_line_refuses_a_different_aws_account(cli):
    code, out = run_cli(cli, account="999999999999")
    assert code == 2 and cli.lake_keys() == []


def test_apply_converts_and_prints_the_state_each_journal_can_be_restored_from(cli):
    code, out = run_cli(cli, "--apply")
    assert code == 0 and out.count(" converted") >= 3 and "before: revision" in out and "state digest" in out
    code, out = run_cli(cli, "--apply")
    assert code == 0 and "nothing-to-convert" in out and "converted  " not in out


def test_the_exit_code_says_when_a_journal_was_skipped_busy(cli):
    worker = cli.runtime(lake=True)
    worker.acquire(str(uuid4()))
    code, out = run_cli(cli, "--apply")
    assert code == 1 and "busy" in out


def test_the_exit_code_says_when_verification_stopped_the_run(cli, monkeypatch):
    from . import record_lake_conversion

    monkeypatch.setattr(record_lake_conversion, "s3_account", lambda *a, **k: cli.account)
    faults = Faults(cli)
    faults.commit_fault = (
        "shared-batches",
        1,
        "corrupt",
        lambda: cli.flip_byte_in_record("shared-batches", "replies-" + cli.hosted, 2),
    )
    code, out = run_cli(cli, "--apply")
    assert code == 3 and "STOPPED" in out


def test_the_script_is_a_thin_entry_point_to_the_library():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "convert_to_record_lake.py"
    spec = importlib.util.spec_from_file_location("convert_to_record_lake", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main is main


def test_check_hook_abandons_a_commit_after_the_pages_are_written():
    from .record_lake import MemoryRecordLake, commit_with_records
    from .s3_journal_test import journal

    j = journal(name="hook")
    first = j.initialize({"x": 1})
    lake = MemoryRecordLake(j.boundary[0], "runtime", "events")

    def refuse(refs):
        raise ConversionMismatch("no")

    with pytest.raises(ConversionMismatch):
        commit_with_records(j, first, [(lake, ["a"])], check=refuse)
    assert j.read().value == {"x": 1} and lake.sequences() == [1]  # the page is an orphan; the state never moved
