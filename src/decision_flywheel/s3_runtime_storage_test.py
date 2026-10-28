"""Core runtime repositories use S3 with exclusive fenced ownership, never SQLite."""

import asyncio
from uuid import uuid4

import pytest

from .s3_journal_test import journal


def stores(j=None):
    from .s3_runtime_storage import S3RuntimeStores

    result = S3RuntimeStores(j or journal(name="classifier-a"))
    result.initialize()
    result.acquire(str(uuid4()))
    return result


def cloud_agent(s):
    from .flywheel_test import agent
    from .s3_batch_storage import S3BatchStorage
    from .s3_optimizer_transport import S3OptimizerTransport

    complete = agent([])
    ledger = S3BatchStorage(journal(s.journal.payloads.client, name="optimizer-budget"))
    ledger.initialize()
    complete.complete = S3OptimizerTransport(
        complete.complete,
        ledger,
        identity={"provider": "synthetic", "model": "fake-optimizer"},
        max_calls=20,
    )
    return complete


def test_runtime_owner_is_exclusive_releases_explicitly_and_fences_the_old_handle():
    from .s3_runtime_storage import RuntimeOwnershipError, S3RuntimeStores

    first = stores()
    j = journal(first.journal.payloads.client, name="classifier-a")
    other = S3RuntimeStores(j)
    other.initialize()
    with pytest.raises(RuntimeOwnershipError):
        other.acquire(str(uuid4()))
    first.runtime_storage.put_state("exact", '{ "note":"雪" }')
    first.close()
    other.acquire(str(uuid4()))
    assert other.runtime_storage.state("exact") == '{ "note":"雪" }'
    with pytest.raises(RuntimeOwnershipError):
        first.runtime_storage.put_state("exact", "{}")
    other.close()


def test_cache_refresh_learning_aliases_and_pending_cleanup_preserve_domain_semantics():
    s = stores()
    cache = s.runtime_cache
    cache.begin_answer("old", None)
    cache.complete_answer("old", '{ "original":true }', previous=None)
    prior = cache.answer("old")
    cache.begin_answer("old", prior)
    cache.complete_answer("old", '{"new":true}', previous=prior, current_key="new")
    assert cache.answer("old") == prior and cache.answer("new") == ("complete", '{"new":true}')
    learn = s.learning_storage
    learn.ensure("rubric")
    learn.begin("rubric", "old", '{"pending_evaluation":true}')
    learn.complete("rubric", "old", '{"pending_evaluation":true}')
    learn.begin("rubric", "next", "{}")
    learn.complete("rubric", "next", '{"done":true}', aliases=("alias",), clear_pending_key="old")
    assert learn.artifact("rubric", "next") == learn.artifact("rubric", "alias")
    cache.begin_round("pending")
    marks = s.pending_storage.marks()
    rows = s.pending_storage.pending()
    row = next(row for row in rows if row[0] == "runtime_rounds")
    assert not s.pending_storage.discard(row[0], row[1], "wrong-key")
    assert s.pending_storage.discard(*row) and cache.round("pending") is None
    cache.begin_round("later")
    assert s.pending_storage.marks()["runtime_rounds"] > marks["runtime_rounds"]
    s.close()


def test_real_core_predict_restart_and_improve_use_only_injected_s3_repositories(monkeypatch):
    from .classifier_config import ClassifierConfig
    from .flywheel import DecisionFlywheel
    from .flywheel_test import DEV, TASK, TRAIN, FakeModel

    def forbidden(*args, **kwargs):
        raise AssertionError("cloud core opened SQLite")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    s = stores()
    j = s.journal
    model = FakeModel()
    wheel = DecisionFlywheel(None, ClassifierConfig(TASK), model, cloud_agent(s), storage=s)
    asyncio.run(wheel.predict(DEV[0].item, TRAIN))
    calls = model.calls
    before = wheel.history(10000)
    wheel.close()
    s = stores(journal(j.payloads.client, name="classifier-a"))
    wheel = DecisionFlywheel(None, ClassifierConfig(TASK), model, cloud_agent(s), storage=s)
    assert wheel.history(10000) == before
    asyncio.run(wheel.predict(DEV[0].item, TRAIN))
    assert model.calls == calls
    result = asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={row.item.id: 1.0 for row in TRAIN}))
    assert isinstance(result, dict) and wheel.history(10000)
    wheel.close()


def test_unknown_acquire_outcome_does_not_grant_runtime_execution():
    from .s3_journal import JournalUncertain
    from .s3_runtime_storage import RuntimeOwnershipError, S3RuntimeStores

    j = journal(name="classifier-a")
    s = S3RuntimeStores(j)
    s.initialize()
    j.payloads.client.lose = lambda key: key == j.head_key
    with pytest.raises(JournalUncertain):
        s.acquire(str(uuid4()))
    with pytest.raises(RuntimeOwnershipError):
        s.runtime_storage.put_state("x", "{}")
    with pytest.raises(RuntimeOwnershipError):
        stores(journal(j.payloads.client, name="classifier-a"))


def test_hypothesis_diagnosis_and_shadow_records_retain_native_policy_inputs():
    from .hypothesis_ledger import HypothesisLedger

    s = stores()
    ledger = HypothesisLedger(storage=s.hypothesis_storage, clock=lambda: "same timestamp")
    ledger.record_evaluations("idea", [{"item_id": "one", "correct": True}], "v1")
    ledger.record_evaluations("idea", [{"item_id": "one", "correct": False}], "v2")
    ledger.record_verdict("idea", "advanced", "original rationale", {"note": "雪"})
    ledger.record_verdict("idea", "retired", "later rationale")
    assert ledger.correctness_vector("idea", ["one"]) == [False]
    assert ledger._latest("idea", context_version="v1") == {"one": True}
    assert [r["reason"] for r in ledger.verdicts("idea")] == ["original rationale", "later rationale"]
    assert ledger.running_score("idea")["n"] == 2
    assert ledger.running_score("idea", "same timestamp", strict=True)["n"] == 0
    s.control_storage.save("idea", '{"id":"idea","operator":"revise","parents":["ancestor"]}')
    assert ledger.lineage("idea")["parents"] == ["ancestor"]
    shadow = s.shadow_storage
    shadow.ensure()
    shadow.join("b", "rubric", 3)
    shadow.join("a", "rubric", 3)
    shadow.scored("a", "item", 4)
    shadow.scored("a", "item", 5)
    shadow.incumbent("inc", "exact rubric", 4)
    shadow.incumbent("inc", "changed", 5)
    shadow.look(("a", "inc", 1, 20, 5, 0, 0.02, 1, 4))
    assert shadow.active() == [("a", "rubric", 3), ("b", "rubric", 3)]
    assert shadow.calls() == 1 and shadow.looks_taken("a", "inc") == 1
    with pytest.raises(ValueError):
        shadow.look(("a", "inc", 1, 20, 5, 0, 0.02, 1, 4))
    shadow.set_status("a", "evicted")
    assert shadow.statuses() == {"b": "active", "a": "evicted"}
    s.close()


def test_learning_aliases_past_the_old_runtime_record_budget_commit_together():
    from .s3_runtime_storage import S3RuntimeStores

    # This runtime used to hold at most two records, so the alias below was refused.
    j = journal(name="small")
    s = S3RuntimeStores(j)
    s.initialize()
    s.acquire(str(uuid4()))
    learn = s.learning_storage
    learn.begin("rubric", "old", '{"pending_evaluation":true}')
    learn.complete("rubric", "old", '{"pending_evaluation":true}')
    learn.begin("rubric", "new", "{}")
    learn.complete("rubric", "new", '{"done":true}', aliases=("extra",), clear_pending_key="old")
    assert learn.artifact("rubric", "new") == ("complete", '{"done":true}')
    assert learn.artifact("rubric", "extra") == ("complete", '{"done":true}')
    assert j.read().revision == s.journal.read().revision
    s.close()


def test_uncertain_runtime_commit_poisoning_prevents_followup_exception_cleanup_writes():
    from .s3_journal import JournalUncertain
    from .s3_runtime_storage import RuntimeOwnershipError

    s = stores()
    s.journal.payloads.client.lose = lambda key: key == s.journal.head_key
    with pytest.raises(JournalUncertain):
        s.runtime_storage.append_event('{"kind":"committed-response-lost"}')
    before = len(s.journal.payloads.client.calls)
    with pytest.raises(RuntimeOwnershipError):
        s.runtime_storage.append_event('{"kind":"unsafe-followup"}')
    assert len(s.journal.payloads.client.calls) == before
    s.close()
    restarted = stores(journal(s.journal.payloads.client, name="classifier-a"))
    assert [e["kind"] for e in restarted.runtime_storage.history(10)] == ["committed-response-lost"]
    restarted.close()


# --- Write amplification: every journal commit rewrites the whole runtime snapshot. ---


def owned_stores(client=None, name="classifier-a", **options):
    from .s3_runtime_storage import S3RuntimeStores

    result = S3RuntimeStores(journal(client, name=name), cache_owned=True, **options)
    result.initialize()
    result.acquire(str(uuid4()))
    return result


def heads(s):
    return s.journal.read().revision


def test_held_events_are_visible_to_the_owner_and_publish_with_the_next_durable_change():
    s = owned_stores()
    before = heads(s)
    first = s.runtime_storage.append_event('{"kind":"a"}')
    second = s.runtime_storage.append_event('{"kind":"b"}')
    assert (first, second) == (1, 2) and heads(s) == before
    assert [e["kind"] for e in s.runtime_storage.history()] == ["a", "b"]
    s.runtime_cache.begin_answer("k", None)
    assert heads(s) == before + 1
    stored = s.journal.read().value["data"]
    assert [e[0] for e in stored["events"]] == [1, 2] and stored["answers"]["k"][0] == "pending"
    s.close()


def test_release_publishes_held_events_in_the_same_commit_as_the_release():
    s = owned_stores()
    s.runtime_storage.append_event('{"kind":"a"}')
    s.runtime_storage.append_event('{"kind":"b"}')
    before = heads(s)
    s.close()
    final = journal(s.journal.payloads.client, name="classifier-a").read()
    assert final.revision == before + 1 and final.value["owner"] is None
    assert len(final.value["data"]["events"]) == 2


def test_flush_publishes_held_events_and_refresh_keeps_them_while_the_head_has_not_moved():
    s = owned_stores()
    s.runtime_storage.append_event('{"kind":"a"}')
    s.flush()
    assert len(s.journal.read().value["data"]["events"]) == 1
    s.runtime_storage.append_event('{"kind":"b"}')
    revision = heads(s)
    s.refresh()  # a guarded call checks ownership with one head read; it does not cost a commit
    assert heads(s) == revision and [e["kind"] for e in s.runtime_storage.history()] == ["a", "b"]
    s.flush()
    assert len(s.journal.read().value["data"]["events"]) == 2
    revision = heads(s)
    s.flush()  # nothing held: no commit
    assert heads(s) == revision
    s.close()


def test_a_refresh_after_the_head_moved_under_held_changes_poisons_the_handle():
    from .s3_runtime_storage import RuntimeOwnershipError

    s = owned_stores()
    s.runtime_storage.append_event('{"kind":"a"}')
    rival = journal(s.journal.payloads.client, name="classifier-a")
    rival.commit(rival.read(), {**rival.read().value, "ordinal": 99})
    with pytest.raises(RuntimeOwnershipError):
        s.refresh()
    assert s.poisoned


def test_events_held_by_a_crashed_owner_are_not_durable_but_the_reservation_is():
    s = owned_stores()
    s.runtime_cache.begin_answer("k", None)  # the paid-action reservation is always durable at once
    s.runtime_storage.append_event('{"kind":"after-dispatch"}')
    stored = journal(s.journal.payloads.client, name="classifier-a").read().value["data"]
    assert stored["answers"]["k"][0] == "pending" and stored["events"] == []


def test_a_head_moved_while_events_are_held_poisons_the_handle_instead_of_dropping_them_silently():
    from .s3_runtime_storage import RuntimeOwnershipError

    s = owned_stores()
    s.runtime_storage.append_event('{"kind":"a"}')
    rival = journal(s.journal.payloads.client, name="classifier-a")
    rival.commit(rival.read(), {**rival.read().value, "ordinal": 99})
    with pytest.raises(RuntimeOwnershipError):
        s.runtime_cache.begin_answer("k", None)
    with pytest.raises(RuntimeOwnershipError):
        s.runtime_storage.history()


def test_handles_that_do_not_own_a_cache_keep_one_commit_per_event():
    s = stores()
    before = heads(s)
    s.runtime_storage.append_event('{"kind":"a"}')
    s.runtime_storage.append_event('{"kind":"b"}')
    assert heads(s) == before + 2
    s.close()


def test_coalescing_can_be_switched_off_for_an_owning_handle():
    s = owned_stores(coalesce_events=False)
    before = heads(s)
    s.runtime_storage.append_event('{"kind":"a"}')
    assert heads(s) == before + 1
    s.close()


def journal_writes(client):
    """(commits, bytes of every payload written: heads' states and values), at the S3 boundary."""
    heads = [r for kind, r in client.calls if kind == "put" and r["Key"].endswith("/head.json")]
    puts = [r for kind, r in client.calls if kind == "put" and "/payloads/" in r["Key"]]
    return len(heads), sum(len(r["Body"]) for r in puts)


def publication_jobs(coalesce):
    """One new item decided across three classifiers, then two replays of the same item.

    Mirrors what the hosted worker does per publication job: acquire each runtime, run the decision
    (trace events around an answer reservation and completion), release. A replay finds the answer
    cached, so it only takes and releases the lease. Each runtime starts with a realistic history.
    """
    from .s3_journal_test import ConditionalS3

    client = ConditionalS3()
    names = ["classifier-a", "classifier-b", "classifier-c"]
    for name in names:
        seed = owned_stores(client, name, coalesce_events=coalesce)
        for n in range(60):
            seed.runtime_storage.append_event('{"kind":"history","note":"' + "x" * 3000 + str(n) + '"}')
        seed.close()
    client.calls.clear()
    for job in range(3):
        handles = [owned_stores_reopen(client, name, coalesce) for name in names]
        if job == 0:
            for handle in handles:
                events = handle.runtime_storage.append_event
                events('{"kind":"item","n":1}')
                events('{"kind":"request"}')
                handle.runtime_cache.begin_answer("answer-" + handle.journal.namespace, None)
                events('{"kind":"dispatch"}')
                events('{"kind":"response","text":"' + "y" * 20000 + '"}')
                events('{"kind":"parsed"}')
                handle.runtime_cache.complete_answer("answer-" + handle.journal.namespace, '{"label":"yes"}', previous=None)
                events('{"kind":"decision"}')
                events('{"kind":"explanation"}')
                events('{"kind":"done"}')
        for handle in handles:
            handle.close()
    return journal_writes(client)


def owned_stores_reopen(client, name, coalesce):
    from .s3_runtime_storage import S3RuntimeStores

    result = S3RuntimeStores(journal(client, name=name), cache_owned=True, coalesce_events=coalesce)
    result.acquire(str(uuid4()))
    return result


def test_a_publication_job_sequence_writes_far_fewer_snapshots_when_events_are_coalesced(capsys):
    before_commits, before_bytes = publication_jobs(coalesce=False)
    after_commits, after_bytes = publication_jobs(coalesce=True)
    with capsys.disabled():
        print(
            f"\nruntime journal writes for 1 new item + 2 replays x 3 classifiers: "
            f"{before_commits} commits / {before_bytes / 1e6:.1f} MB -> {after_commits} commits / {after_bytes / 1e6:.1f} MB"
        )
    # New-item job, per runtime: acquire + 8 events + 2 answer changes + release = 12 commits, now acquire,
    # the reservation, and a release carrying the completion and the events = 3. Each replay is acquire + release.
    assert before_commits == 3 * 12 + 2 * 3 * 2
    assert after_commits == 3 * 3 + 2 * 3 * 2
    assert after_commits <= before_commits * 0.55
    assert after_bytes <= before_bytes * 0.55


# --- Constant cost: what a decision writes does not depend on the decisions before it ---------------------


def lake_stores(client, name="classifier-a"):
    from .s3_runtime_storage import S3RuntimeStores, runtime_events_lake

    j = journal(client, name=name)
    result = S3RuntimeStores(j, cache_owned=True, events_lake=runtime_events_lake(j))
    result.initialize()
    result.acquire(str(uuid4()))
    return result


def one_decision(client, n):
    """What a hosted job does to one output's runtime for one new item, on a fresh handle as each job opens."""
    import json

    s = lake_stores(client)
    item, key = f"item-{n}", f"request-{n}"
    event = lambda **fields: s.runtime_storage.append_event(json.dumps(fields))  # noqa: E731
    assert s.runtime_storage.unfinished_cycles(item) == ()
    number = s.runtime_storage.next_cycle_number()
    event(kind="cycle-started", cycle_id=f"cycle-{n}", cycle_number=number, cycle_item_id=item, item={"id": item})
    assert s.runtime_cache.answer(key) is None
    event(kind="decision-cache", request_fingerprint=key, status="missing")
    s.runtime_cache.begin_answer(key, None)
    event(kind="features-requested", target_id=item, request_fingerprint=key)
    s.runtime_cache.complete_answer(key, json.dumps({"answers": {"q": {"label": "yes"}}, "n": n}), previous=None)
    event(kind="features-completed", target_id=item, request_fingerprint=key)
    event(kind="prediction", target_id=item, label="yes", cycle_id=f"cycle-{n}", cycle_item_id=item)
    event(kind="cycle-waiting-for-feedback", cycle_id=f"cycle-{n}", cycle_item_id=item)
    s.runtime_storage.save_derived(
        {"status-fold": json.dumps({"cursor": n, "awaiting": n})},
        {"status": {f"row/{item}": json.dumps({"shown": ["yes", None], "waiting": True})}},
    )
    s.close()


def writes_of(client, start):
    """What one decision cost since ``start``: (commits, chunk records, keyed documents written, GETs) and
    (head-state bytes, keyed-document bytes, other payload bytes)."""
    calls = [request for kind, request in client.calls[start:] if kind == "put"]
    payloads = [request["Body"] for request in calls if "/payloads/" in request["Key"]]
    documents = [request["Body"] for request in calls if "/keys/" in request["Key"]]
    state = sum(len(body) for body in payloads if body.startswith(b'{"anchor"'))
    counts = (
        sum(request["Key"].endswith("/head.json") for request in calls),
        sum("/records/runtime/" in request["Key"] for request in calls),
        len(documents),
        sum(kind == "get" for kind, _ in client.calls[start:]),
    )
    return counts, (state, sum(map(len, documents)), sum(len(body) for body in payloads) - state)


def test_a_decision_reads_and_writes_the_same_after_a_long_history_and_commits_once_while_it_decides(capsys):
    from .s3_journal_test import ConditionalS3

    client = ConditionalS3()
    seen = {}
    for n in range(1, 401):
        start = len(client.calls)
        one_decision(client, n)
        if n in (20, 400):
            seen[n] = writes_of(client, start)
    with capsys.disabled():
        print(f"\nper decision ((commits, chunks, keyed documents, GETs), (head, keyed, other bytes)): {seen}")
    (early, early_bytes), (late, late_bytes) = seen[20], seen[400]
    # Acquire, the reservation (the one commit while deciding: it grants the paid call), and the release that
    # carries the completion, the events and the status summary. Each commit's events are one chunk record.
    # The settled answer, the item's cycles and its status row are one document each, found by the same reads.
    assert early == late and early[:3] == (3, 2, 3)
    # Nothing written holds rows of earlier decisions, so the bytes are the same twenty times further on, but
    # for the extra digits of revisions, fences, ordinals and lake positions.
    assert all(abs(b - a) <= 3 * 12 for a, b in zip(early_bytes, late_bytes))


def test_a_runtime_head_holds_no_answers_cycles_or_status_rows_however_many_items_it_decided():
    from .journal import encode
    from .s3_journal_test import ConditionalS3

    client = ConditionalS3()
    sizes = []
    for n in range(1, 61):
        one_decision(client, n)
        if n in (10, 60):
            sizes.append(len(encode(journal(client, name="classifier-a").read().value)))
    assert abs(sizes[1] - sizes[0]) <= 8
    s = lake_stores(client)
    assert s.runtime_cache.answer("request-7")[0] == "complete"
    assert s.runtime_storage.unfinished_cycles("item-7")[0][0] == "cycle-7"
    assert s.runtime_storage.keyed("status", "row/item-7") is not None
    assert s.runtime_cache.answer("request-60")[0] == "complete" and s.runtime_cache.answer("request-61") is None
    assert [e["kind"] for e in s.runtime_storage.history(2)] == ["prediction", "cycle-waiting-for-feedback"]
    s.close()


def test_a_refused_keyed_read_fails_closed_instead_of_reading_as_a_new_request():
    from .keyed_records import KeyedUnavailable
    from .s3_journal_test import ConditionalS3, S3Error

    client = ConditionalS3()
    one_decision(client, 1)
    s = lake_stores(client)
    real = client.get_object

    def refused(**request):
        if "/keys/" in request["Key"]:
            raise S3Error("AccessDenied")
        return real(**request)

    client.get_object = refused
    with pytest.raises(KeyedUnavailable):
        s.runtime_cache.answer("request-1")
    with pytest.raises(KeyedUnavailable):
        s.runtime_cache.answer("never-asked")  # only NoSuchKey means absent


# --- Runtimes written by the previous layout ------------------------------------------------------------


def old_format_runtime(client):
    """A runtime exactly as the v1 layout left it: everything inline in the head, one lake record per event."""
    import json

    from .learning_storage import ARTIFACTS
    from .record_lake import RECORDS_KEY, Coverage
    from .s3_runtime_storage import CYCLE_INDEX_KEY, CYCLE_MARK_KEY, DICT_FIELDS, LIST_FIELDS, runtime_events_lake

    j = journal(client, name="classifier-a")
    lake = runtime_events_lake(j)
    events = [
        {"kind": "cycle-started", "cycle_id": "c-1", "cycle_number": 1, "cycle_item_id": "item-1"},
        {"kind": "prediction", "target_id": "item-1", "label": "yes"},
        {"kind": "cycle-started", "cycle_id": "c-2", "cycle_number": 2, "cycle_item_id": "item-2"},
        {"kind": "cycle-completed", "cycle_id": "c-2", "cycle_number": 2, "cycle_item_id": "item-2"},
    ]
    coverage, _ = lake.append(Coverage(), [json.dumps(e) for e in events])
    data = {**{k: {} for k in DICT_FIELDS}, **{k: [] for k in LIST_FIELDS}, "artifacts": {k: {} for k in ARTIFACTS}}
    data["answers"] = {"done": ["complete", '{"label":"yes"}', 2], "waiting": ["pending", None, 4]}
    data["answer_history"] = [[1, "done", "failed", None]]
    data["rounds"] = {"r1": ["complete", '{"round":1}', 1]}
    data["state"] = {
        "contract": '{"task":"t"}',
        CYCLE_MARK_KEY: "2",
        CYCLE_INDEX_KEY: json.dumps({"item-1": [["c-1", 1, "item-processing", False]]}),
        "status-fold": '{"cursor":4,"fold":{"old":true},"origin":"o"}',
    }
    value = {"schema": "cyclotron-s3-runtime/v1", "owner": None, "fence": 3, "ordinal": 4, "data": data}
    value[RECORDS_KEY] = {lake.name: coverage.to_json()}
    j.initialize(value)
    return [json.dumps(e) for e in events]


def test_an_old_format_runtime_keeps_its_events_answers_cycles_and_status_and_converts_on_its_first_write():
    import json

    from .s3_journal_test import ConditionalS3

    client = ConditionalS3()
    events = old_format_runtime(client)
    s = lake_stores(client)  # acquiring is the first write
    stored = journal(client, name="classifier-a").read().value
    assert stored["schema"] == "cyclotron-s3-runtime/v2" and stored["maps"] == {}
    assert sum("/keys/" in k for k in client.objects) == 3  # "done", history 1 and item-1's cycles, one each
    assert stored["data"]["answers"] == {"waiting": ["pending", None, 4]} and stored["data"]["answer_history"] == []
    assert "unfinished-cycles" not in stored["data"]["state"] and set(stored["refs"]) == {"rounds"}

    def readable(s):
        rows = [(e.pop("event_id"), json.dumps(e)) for e in map(dict, s.runtime_storage.history(-1))]
        assert rows[:4] == list(enumerate(events, 1))
        assert s.runtime_cache.answer("done") == ("complete", '{"label":"yes"}')
        assert s.runtime_cache.answer("waiting") == ("pending", None)
        assert s.pending_storage.pending() == [("runtime_answers", 4, "waiting")]
        assert s.pending_storage.marks()["runtime_answers"] == 4
        assert s.runtime_storage.keyed("answer-history", "1") == '[1,"done","failed",null]'
        assert s.runtime_storage.unfinished_cycles("item-1") == (("c-1", 1, "item-processing", False),)
        assert s.runtime_storage.unfinished_cycles("item-2") == ()
        assert s.runtime_storage.state("status-fold") == '{"cursor":4,"fold":{"old":true},"origin":"o"}'
        assert s.runtime_storage.state("contract") == '{"task":"t"}'
        assert s.runtime_cache.round("r1") == ("complete", '{"round":1}')

    readable(s)
    assert s.runtime_storage.next_cycle_number() == 3
    assert s.runtime_storage.append_event('{"kind":"new"}') == 5
    s.close()
    again = lake_stores(client)
    readable(again)
    assert [e["kind"] for e in again.runtime_storage.history(2)] == ["cycle-completed", "new"]
    # The four old event records are untouched. After them: the conversion's chunk (no events; it stamps the
    # moved documents) and the chunk holding the new event.
    keys = sorted(k.rsplit("/", 1)[-1] for k in client.objects if "/records/runtime/" in k)
    assert keys == [f"{n:012d}.json" for n in range(1, 7)]
    again.close()


def test_an_old_format_runtime_reads_without_any_write_before_it_is_owned():
    from .s3_journal_test import ConditionalS3
    from .s3_runtime_storage import S3RuntimeStores, runtime_events_lake

    client = ConditionalS3()
    events = old_format_runtime(client)
    j = journal(client, name="classifier-a")
    audit = S3RuntimeStores(j, events_lake=runtime_events_lake(j)).audit_events()
    assert audit.record.value["schema"] == "cyclotron-s3-runtime/v1"
    assert [p for _, p in audit.stored] == events and audit.embedded == []
