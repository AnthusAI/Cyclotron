"""One-time conversion of embedded history into the record lake, for one lab account.

  python scripts/dev/convert_to_record_lake.py --profile P --expect-aws-account 123456789012 \\
      --books-bucket B --cyclotron-account UUID [--journal runtime/ab12] [--apply]

In plain language. Before the lake, each journal kept its whole history inside its one big file. This
tool moves that history out into the lake's small write-once files, using the same two-step protocol
the live code uses: write the new files first (harmless if abandoned), then ONE small commit that
removes the embedded copies and records which files now count. Three kinds of history move:

* runtime events in each runtime journal (they become the lake's first records, ids and order kept);
* raw model replies in each shared-batches ledger (each reply text becomes a note ``{seq, sha256, length}``);
* finished job results in the catalog (each result text becomes a reference).

Safety.

* Dry run is the default and reads only. It prints, per journal, how many records would move, the
  control-state bytes before and after, the bytes the lake would receive, and any problems.
* ``--apply`` takes each journal's writer ownership exactly as the live worker does: the workspace root
  (which guards the ledger and runtimes under it) and the runtime's own owner and fence. A journal that
  is owned by anyone else is skipped and reported busy; the tool never steals ownership. A catalog with
  running jobs is busy too. The tool's own owner id is fixed per account, so a run that stopped without
  releasing is resumed by the next run (the fence moves and fences the older run out); nobody else's
  ownership is ever taken.
* Idempotent and resumable. A journal with nothing embedded is left alone. Records are deterministic, so a
  re-run adopts pages an interrupted run created (identical bytes at the same sequence) instead of
  writing them twice.
* After each journal it reads everything back through fresh handles and the same code the live system
  uses, and compares each moved record byte for byte (by SHA-256) with the embedded original, plus ids,
  order and coverage. Pages are also checked before the commit, so a corrupt page never becomes real. Any
  mismatch stops the whole run.
* Fail closed. Nothing is deleted or altered except through the normal commit protocol; the lake has no
  delete grant. ``--apply`` prints the revision and digest of each journal's state before it changed,
  which is the exact earlier transaction (still in ``payloads/``) an operator would restore from.

A runtime that already received lake events (after the new code was deployed, before this tool ran) is
handled however many there are: the embedded events go first and the earlier lake records are copied
after them (their old sequences marked skipped) so ids and order are unchanged.
"""

import argparse
import os
import sys
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import UUID

from .batch_storage import StorageScope
from .cyclotrons import load_config
from .journal import encode
from .record_lake import RECORDS_KEY, RecordLimit
from .s3_batch_storage import EMPTY as LEDGER_FIELDS
from .s3_batch_storage import S3BatchStorage, reply_lake_for
from .s3_console_storage import S3ConsoleStorage, one, results_lake_for, rows
from .s3_embedded_storage import S3EmbeddedStorage
from .s3_runtime_storage import CYCLE_MARK_KEY, RuntimeOwnershipError, S3RuntimeStores, runtime_events_lake
from .storage_errors import StorageUncertain


class ConversionMismatch(RuntimeError):
    """A converted record is not identical to the embedded original, or ids, order or coverage differ."""


class Stop(Exception):
    """Internal: stop the run after reporting a mismatch."""


@dataclass
class Report:
    journal: str  # "domain/name"
    kind: str  # runtime-events | reply-ledger | job-results
    status: str = "nothing-to-convert"  # would-convert | converted | busy | problem | mismatch
    records: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    lake_bytes: int = 0
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    revision_before: int = 0
    digest_before: str = ""
    revision_after: int = 0

    def line(self):
        head = f"{self.journal:<80} {self.kind:<14} {self.status}"
        if self.status in ("would-convert", "converted", "mismatch"):
            head += (
                f"  records={self.records}  state {self.bytes_before:,} -> {self.bytes_after:,} bytes"
                f"  lake +{self.lake_bytes:,} bytes"
            )
        elif self.status == "nothing-to-convert":
            head += f"  state {self.bytes_before:,} bytes"
        return "\n".join(
            [head]
            + [f"    problem: {p}" for p in self.problems]
            + [f"    note: {n}" for n in self.notes]
            + (
                [
                    f"    before: revision {self.revision_before}, state digest {self.digest_before}; after: revision {self.revision_after}"
                ]
                if self.status == "converted"
                else []
            )
        )


@dataclass(frozen=True)
class Unit:
    kind: str  # runtime | ledger
    domain: str  # journal domain
    name: str
    label: str | None  # the ledger's reply-lake label (None for a runtime)
    root: str | None  # the workspace root whose ownership guards it


class Account:
    """How to open one account's journals and lakes. Every call returns a fresh, independent handle."""

    def __init__(
        self,
        journal,
        names,
        *,
        events_lake=runtime_events_lake,
        reply_lake=reply_lake_for,
        results_lake=results_lake_for,
    ):
        self.journal, self.names = journal, names
        self.events_lake, self.reply_lake, self.results_lake = events_lake, reply_lake, results_lake

    def catalog(self):
        journal = self.journal("console", "catalog")
        return S3ConsoleStorage(journal, results=self.results_lake(journal))


def conversion_owner(namespace):
    """A fixed, valid UUIDv4 owner id for this account's conversion runs (never a worker's random id)."""
    raw = bytearray(sha256(("record-lake-conversion\0" + namespace).encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(raw)))


def _describe(error):
    return f"{type(error).__name__}: {error}"


def _read_back(label, read):
    """Run a read-back through the live code; a read that fails is a verification failure, not a skipped check."""
    try:
        return read()
    except Exception as error:
        raise ConversionMismatch(f"{label}: cannot be read back after the commit: {_describe(error)}") from None


def _failed(report, error):
    report.status = "problem"
    report.problems.append(_describe(error))
    if isinstance(error, StorageUncertain):
        report.notes.append("the commit's outcome is unknown; re-run (dry run first) to see whether it landed")
    return report


def _without(value):
    """A control state with the parts a conversion changes taken out, to compare the rest."""
    kept = {k: v for k, v in value.items() if k != RECORDS_KEY}
    if "data" in kept:
        data = {k: v for k, v in kept["data"].items() if k != "events"}
        data["state"] = {k: v for k, v in data.get("state", {}).items() if k != CYCLE_MARK_KEY}
        kept["data"] = data
    return kept


def _pages_intact(lake):
    """Pre-commit check: every page just written reads back with exactly the bytes that were intended."""

    def check(refs):
        for ref in refs[lake.name]:
            stored = lake.stored_ref(ref.seq)
            if (stored.digest, stored.length) != (ref.digest, ref.length):
                raise ConversionMismatch(f"{lake.name} record {ref.seq} was stored with different bytes; commit abandoned")

    return check


def _digest(text):
    return sha256(text.encode()).hexdigest()


def _begin(report, record, value=None):
    report.revision_before, report.digest_before = record.revision, record.reference.digest
    report.bytes_before = len(encode(record.value if value is None else value))


def _settle(report, status, after_record, after_value=None):
    report.status = status
    report.revision_after = after_record.revision
    report.bytes_after = len(encode(after_record.value if after_value is None else after_value))


# --- runtime events -----------------------------------------------------------------------------------


def convert_runtime(account, unit, *, apply, owner):
    report = Report(f"runtime/{unit.name}", "runtime-events")
    journal = account.journal("runtime", unit.name)
    lake = account.events_lake(journal)
    store = S3RuntimeStores(journal, events_lake=lake)
    try:
        audit = store.audit_events()
    except Exception as error:
        report.status = "problem"
        report.problems.append(f"cannot read the runtime: {_describe(error)}")
        return report
    _begin(report, audit.record)
    report.records = len(audit.embedded)
    held = audit.record.value["owner"]
    if held is not None and held != owner:
        report.status = "busy"
        report.notes.append("owned by another holder (a worker, or a crashed one that needs reconciling); skipped")
        return report
    if held is not None:
        report.notes.append("left held by an earlier conversion run; this run resumes it")
    if not audit.embedded and not (apply and held is not None):
        report.bytes_after = report.bytes_before
        return report
    try:
        if audit.embedded and [i for i, _ in audit.embedded] != list(range(1, len(audit.embedded) + 1)):
            raise ValueError("embedded event ids are not 1..n; converting would renumber them")
        if audit.embedded:
            projected, report.lake_bytes = store.preview_events_conversion(audit)
            report.bytes_after = len(encode(projected))
    except (ValueError, RecordLimit) as error:
        report.status = "problem"
        report.problems.append(_describe(error))
        return report
    if audit.stored:
        report.notes.append(
            f"the lake already holds {len(audit.stored)} records; they are copied after the embedded events "
            "(old sequences marked skipped) so ids and order stay the same"
        )
    if not apply:
        report.status = "would-convert"
        return report
    try:
        store.acquire(owner, resume=True)
    except RuntimeOwnershipError:
        report.status = "busy"
        report.notes.append("ownership was taken by someone else just now; skipped")
        return report
    try:
        moved = store.convert_embedded_events(check=_pages_intact(lake))
        if moved is None:
            report.bytes_after = report.bytes_before
            return report  # nothing was embedded; the finally below releases a hold an earlier run left
        _verify_runtime(account, unit, moved)
    except ConversionMismatch as error:
        report.status, report.problems = "mismatch", [str(error)]
        return report
    except Exception as error:
        return _failed(report, error)
    finally:
        try:
            store.close()
        except Exception as error:
            report.notes.append(f"ownership not released ({_describe(error)}); the next run resumes it")
    _settle(report, "converted", moved.after)
    return report


def _verify_runtime(account, unit, moved):
    journal = account.journal("runtime", unit.name)
    lake = account.events_lake(journal)
    store = S3RuntimeStores(journal, events_lake=lake)
    audit = _read_back("runtime events", store.audit_events)
    bad = []
    if audit.embedded:
        bad.append(f"{len(audit.embedded)} events are still embedded")
    expected, got = moved.rows, audit.stored
    if [i for i, _ in got] != [i for i, _ in expected]:
        bad.append("event ids or their order differ from the original")
    elif [_digest(p) for _, p in got] != [_digest(p) for _, p in expected]:
        bad.append("an event differs from its embedded original (SHA-256)")
    if audit.coverage != moved.coverage:
        bad.append("lake coverage differs from what was committed")
    for ref in moved.refs:
        stored = lake.stored_ref(ref.seq)
        if (stored.digest, stored.length) != (ref.digest, ref.length):
            bad.append(f"record {ref.seq} is not the bytes that were written")
    if _without(audit.record.value) != _without(moved.before.value):
        bad.append("something other than the events and their coverage changed in the control state")
    if bad:
        raise ConversionMismatch("runtime events: " + "; ".join(bad))


# --- shared-batches ledgers ---------------------------------------------------------------------------


def convert_ledger(account, unit, *, apply):
    report = Report(f"shared-batches/{unit.name}", "reply-ledger")
    journal = account.journal("shared-batches", unit.name)
    lake = account.reply_lake(journal, unit.label) if unit.label else None
    if lake is None:
        report.status = "problem"
        report.problems.append("cannot tell which reply lake this ledger uses (no workspace root found for it)")
        return report
    storage = S3BatchStorage(journal, replies=lake)
    try:
        record, originals = storage.audit_replies()
        _begin(report, record)
        report.records = len(originals)
        if not originals:
            report.bytes_after = report.bytes_before
            return report
        projected, report.lake_bytes = storage.preview_replies_conversion(record)
        report.bytes_after = len(encode(projected))
    except Exception as error:
        return _failed(report, error)
    if not apply:
        report.status = "would-convert"
        return report
    try:
        moved = storage.convert_embedded_replies(check=_pages_intact(lake))
        if moved is None:
            return report
        _verify_ledger(account, unit, record, moved)
    except ConversionMismatch as error:
        report.status, report.problems = "mismatch", [str(error)]
        return report
    except Exception as error:
        return _failed(report, error)
    _settle(report, "converted", moved.after)
    return report


def _verify_ledger(account, unit, before, moved):
    journal = account.journal("shared-batches", unit.name)
    lake = account.reply_lake(journal, unit.label)
    fresh = S3BatchStorage(journal, replies=lake)
    bad = []
    record, left = _read_back("reply ledger", fresh.audit_replies)
    if left:
        bad.append(f"{len(left)} replies are still embedded")
    exported = _read_back("reply ledger", fresh.export)  # every reply read back through the lake and checked against its note
    original = {name: before.value[name] for name in LEDGER_FIELDS}
    if exported != original:
        bad.append("the ledger read back through the lake differs from the embedded original")
    else:
        texts = lambda snapshot: [r[2] for r in snapshot["batches"]] + [r[4] for r in snapshot["history"]]  # noqa: E731
        digests = lambda snapshot: [None if t is None else _digest(t) for t in texts(snapshot)]  # noqa: E731
        if digests(exported) != digests(original):
            bad.append("a reply differs from its embedded original (SHA-256)")
    for ref in moved.refs:
        stored = lake.stored_ref(ref.seq)
        if (stored.digest, stored.length) != (ref.digest, ref.length):
            bad.append(f"record {ref.seq} is not the bytes that were written")
    if {k: v for k, v in record.value.items() if k not in (RECORDS_KEY, *LEDGER_FIELDS)} != {
        k: v for k, v in before.value.items() if k not in (RECORDS_KEY, *LEDGER_FIELDS)
    }:
        bad.append("something other than the ledger rows and coverage changed")
    if bad:
        raise ConversionMismatch("reply ledger: " + "; ".join(bad))


# --- catalog job results ------------------------------------------------------------------------------


def convert_catalog(account, *, apply):
    report = Report("console/catalog", "job-results")
    store = account.catalog()
    try:
        record, movable, unmovable, running = store.audit_results()
        _begin(report, record)
        report.records = len(movable)
        for job_id, *_ in unmovable:
            report.notes.append(f"job {job_id}: result text is not canonical JSON; left embedded")
        if running:
            report.status = "busy"
            report.notes.append(f"{len(running)} job(s) running; convert when none are")
            return report
        if not movable:
            report.bytes_after = report.bytes_before
            return report
        projected, report.lake_bytes = store.preview_results_conversion(record, movable)
        report.bytes_after = len(encode(projected))
    except Exception as error:
        return _failed(report, error)
    if not apply:
        report.status = "would-convert"
        return report
    try:
        moved = store.convert_embedded_results(check=_pages_intact(store.results))
        if moved is None:
            return report
        _verify_catalog(account, record, moved)
    except ConversionMismatch as error:
        report.status, report.problems = "mismatch", [str(error)]
        return report
    except Exception as error:
        return _failed(report, error)
    _settle(report, "converted", moved.after)
    return report


def _verify_catalog(account, before, moved):
    fresh = account.catalog()
    record = fresh._record()
    bad, converted = [], {job_id: raw for job_id, _, _, raw in moved.originals}
    after, prior = record.value, before.value
    for job_id, raw in converted.items():
        row = one(after, "web_jobs", id=job_id)
        try:
            text = fresh._result_text(after, row)
        except Exception as error:
            bad.append(f"job {job_id} cannot be read back: {_describe(error)}")
            continue
        if _digest(text) != _digest(raw):
            bad.append(f"job {job_id}: result differs from its embedded original (SHA-256)")
    tables_a, tables_b = after["snapshot"]["tables"], prior["snapshot"]["tables"]
    if set(tables_a) != set(tables_b) or any(tables_a[t] != tables_b[t] for t in tables_a if t != "web_jobs"):
        bad.append("a table other than web_jobs changed")
    jobs_a, jobs_b = tables_a["web_jobs"], tables_b["web_jobs"]
    if [r["id"] for r in jobs_a] != [r["id"] for r in jobs_b]:
        bad.append("job ids or their order changed")
    else:
        for row_a, row_b in zip(jobs_a, jobs_b):
            same = {**row_a, "result": None} == {**row_b, "result": None}
            if not same or (row_a["id"] not in converted and row_a != row_b):
                bad.append(f"job {row_a['id']} changed beyond its result")
    if {k: v for k, v in after.items() if k not in (RECORDS_KEY, "snapshot")} != {
        k: v for k, v in prior.items() if k not in (RECORDS_KEY, "snapshot")
    }:
        bad.append("claims or actors changed")
    if after["snapshot"]["sequences"] != prior["snapshot"]["sequences"]:
        bad.append("sequences changed")
    for ref in moved.refs:
        stored = fresh.results.stored_ref(ref.seq)
        if (stored.digest, stored.length) != (ref.digest, ref.length):
            bad.append(f"record {ref.seq} is not the bytes that were written")
    if bad:
        raise ConversionMismatch("job results: " + "; ".join(bad))


# --- finding the journals and running them ------------------------------------------------------------


def classifier_ids(account):
    value = account.catalog()._record().value
    ids = {r["id"] for r in rows(value, "classifiers")}
    for run in rows(value, "web_runs"):
        try:
            ids |= {c["id"] for c in load_config(run["config"]).get("classifiers", []) if isinstance(c, dict) and "id" in c}
        except ValueError:
            continue
    return sorted(ids)


def attribute(account):
    """Which ledgers and runtimes belong to which workspace root, from the names the live code derives."""
    ids = classifier_ids(account)
    ledgers, runtimes = set(account.names("shared-batches")), set(account.names("runtime"))
    by_root, seen = {}, set()
    for root in sorted(account.names("workspace")):
        namespace = account.journal("workspace", root).namespace
        hosted = sha256(namespace.encode()).hexdigest()
        units = []
        for name, label in ((hosted, hosted), (sha256((namespace + "\0ledger").encode()).hexdigest(), "ledger")):
            if name in ledgers:
                units.append(Unit("ledger", "shared-batches", name, label, root))
                seen.add(("ledger", name))
        for identifier in ids:
            for name in (
                sha256((hosted + "\0" + identifier).encode()).hexdigest(),
                sha256((namespace + "\0" + identifier).encode()).hexdigest(),
            ):
                if name in runtimes and ("runtime", name) not in seen:
                    units.append(Unit("runtime", "runtime", name, None, root))
                    seen.add(("runtime", name))
        by_root[root] = units
    loose = [Unit("ledger", "shared-batches", n, None, None) for n in sorted(ledgers) if ("ledger", n) not in seen]
    loose += [Unit("runtime", "runtime", n, None, None) for n in sorted(runtimes) if ("runtime", n) not in seen]
    return by_root, loose


def convert_account(account, *, apply=False, only=(), allow_unattributed=False, emit=lambda report: None):
    """Dry run or convert every journal of the account. Returns the reports; raises ConversionMismatch to stop."""
    owner = conversion_owner(account.journal("console", "catalog").namespace)
    by_root, loose = attribute(account)
    wanted = lambda domain, name: not only or any(f"{domain}/{name}".startswith(prefix) for prefix in only)  # noqa: E731
    reports = []

    def finish(report):
        reports.append(report)
        emit(report)
        if report.status == "mismatch":
            raise ConversionMismatch(f"{report.journal}: " + "; ".join(report.problems))

    def run(unit):
        if unit.kind == "runtime":
            return convert_runtime(account, unit, apply=apply, owner=owner)
        return convert_ledger(account, unit, apply=apply)

    for root, units in by_root.items():
        units = [u for u in units if wanted(u.domain, u.name)]
        if not units:
            continue
        journal = account.journal("workspace", root)
        holder = S3EmbeddedStorage(journal)
        busy = None
        try:
            if apply:
                holder.acquire(owner, resume=True)
            else:
                current = journal.read().value["owner"]
                busy = current is not None and current != owner
        except RuntimeOwnershipError:
            busy = True
        except Exception as error:
            for unit in units:
                finish(
                    Report(
                        f"{unit.domain}/{unit.name}",
                        "ownership",
                        "problem",
                        problems=[f"workspace root unreadable: {_describe(error)}"],
                    )
                )
            continue
        try:
            for unit in units:
                if busy:
                    kind = "runtime-events" if unit.kind == "runtime" else "reply-ledger"
                    finish(
                        Report(
                            f"{unit.domain}/{unit.name}",
                            kind,
                            "busy",
                            notes=[f"workspace root {root} is owned by a worker; skipped"],
                        )
                    )
                else:
                    finish(run(unit))
        finally:
            if apply and holder.authority is not None:
                try:
                    holder.release()
                except Exception as error:
                    emit(
                        Report(
                            f"workspace/{root}",
                            "ownership",
                            "problem",
                            problems=[f"not released ({_describe(error)}); the next run resumes it"],
                        )
                    )
    for unit in loose:
        if not wanted(unit.domain, unit.name):
            continue
        if unit.kind == "runtime" and allow_unattributed:
            finish(run(unit))
        else:
            kind = "runtime-events" if unit.kind == "runtime" else "reply-ledger"
            why = (
                "no workspace root found for it, so its ownership cannot be taken like the worker's; "
                "pass --allow-unattributed to convert holding only the runtime's own ownership"
                if unit.kind == "runtime"
                else "no workspace root found for it, so its ownership cannot be taken and its reply lake is unknown"
            )
            finish(Report(f"{unit.domain}/{unit.name}", kind, "problem", problems=[why]))
    if wanted("console", "catalog"):
        finish(convert_catalog(account, apply=apply))
    return reports


# --- command line -------------------------------------------------------------------------------------


class ReadOnlyS3:
    """Wraps an S3 client so a dry run cannot write: only reads and listings pass."""

    ALLOWED = ("get_object", "list_objects_v2", "head_object")

    def __init__(self, client):
        self._client = client
        self.meta, self.exceptions = getattr(client, "meta", None), getattr(client, "exceptions", None)

    def __getattr__(self, name):
        if name in self.ALLOWED:
            return getattr(self._client, name)
        raise PermissionError(f"dry run is read-only; {name} refused")


def s3_account(s3, bucket, aws_account, cyclotron_account):
    from .s3_journal import S3Journal
    from .storage_payloads import S3Payloads

    scope = StorageScope(cyclotron_account, "console")

    def journal(domain, name):
        payloads = S3Payloads(s3, bucket, aws_account, scope)
        return S3Journal(payloads, domain, name, create_only_initialization=True)

    def names(domain):
        prefix, found, token = f"{scope.key}/coordination/{domain}/", [], None
        while True:
            request = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/", "ExpectedBucketOwner": aws_account}
            if token:
                request["ContinuationToken"] = token
            page = s3.list_objects_v2(**request)
            found += [p["Prefix"][len(prefix) : -1] for p in page.get("CommonPrefixes", [])]
            token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
            if not token:
                return sorted(found)

    return Account(journal, names)


def file_account(directory, scope):
    """The same account over a local directory (the file journal and file lakes), for specs and rehearsals."""
    from pathlib import Path

    from .file_journal import LocalFileJournal
    from .storage_payloads import FilePayloads

    root = Path(directory)

    def journal(domain, name):
        return LocalFileJournal(FilePayloads(root, scope), domain, name)

    def names(domain):
        try:
            return sorted(os.listdir(root / scope.account / scope.workspace / "coordination" / domain))
        except FileNotFoundError:
            return []

    return Account(journal, names)


def summary(reports, apply):
    counts = {}
    for report in reports:
        counts[report.status] = counts.get(report.status, 0) + 1
    moved = sum(r.records for r in reports if r.status in ("would-convert", "converted"))
    before = sum(r.bytes_before for r in reports if r.status in ("would-convert", "converted"))
    after = sum(r.bytes_after for r in reports if r.status in ("would-convert", "converted"))
    lake = sum(r.lake_bytes for r in reports if r.status in ("would-convert", "converted"))
    verb = "converted" if apply else "would convert"
    return (
        f"{len(reports)} journals: "
        + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        + f"\n{verb} {moved:,} records; control state {before:,} -> {after:,} bytes; lake +{lake:,} bytes"
    )


def run(args, *, session, out=print):
    identity = session.client("sts").get_caller_identity()
    if identity["Account"] != args.expect_aws_account:
        print(f"Refusing: caller account is not {args.expect_aws_account}", file=sys.stderr)
        return 2
    s3 = session.client("s3")
    if not args.apply:
        s3 = ReadOnlyS3(s3)
    account = s3_account(
        s3,
        args.books_bucket,
        identity["Account"],
        args.cyclotron_account,
    )
    out(
        ("APPLY: converting" if args.apply else "DRY RUN (reads only; nothing is written; pass --apply to convert)")
        + f" account {args.cyclotron_account} in {args.books_bucket}"
    )
    try:
        reports = convert_account(
            account,
            apply=args.apply,
            only=tuple(args.journal),
            allow_unattributed=args.allow_unattributed,
            emit=lambda report: out(report.line()),
        )
    except ConversionMismatch as error:
        out(f"STOPPED: verification failed, no further journal was touched: {error}")
        return 3
    out(summary(reports, args.apply))
    return 1 if any(r.status in ("busy", "problem") for r in reports) else 0


def aws_session(profile, region):
    from .command_recovery import aws_session as session

    return session(profile, region)


def parser():
    p = argparse.ArgumentParser(prog="convert_to_record_lake", description=__doc__.split("\n\n")[0])
    p.add_argument("--profile")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--expect-aws-account", required=True)
    p.add_argument("--books-bucket", required=True)
    p.add_argument("--cyclotron-account", required=True)
    p.add_argument(
        "--journal", action="append", default=[], help="only journals whose 'domain/name' starts with this (repeatable)"
    )
    p.add_argument(
        "--allow-unattributed",
        action="store_true",
        help="also convert runtimes with no workspace root, holding only their own ownership",
    )
    p.add_argument("--apply", action="store_true", help="write; the default is a read-only dry run")
    return p


def main(argv=None, *, session=None, out=print):
    args = parser().parse_args(argv)
    if session is None:
        session = aws_session(args.profile, args.region)
    return run(args, session=session, out=out)


if __name__ == "__main__":
    sys.exit(main())
