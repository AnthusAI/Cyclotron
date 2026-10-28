"""Core runtime repositories use S3 with exclusive fenced ownership, never SQLite."""

import asyncio
from uuid import uuid4

import pytest

from .s3_journal_test import journal


def stores(j=None):
    from .s3_runtime_storage import S3RuntimeStores

    result = S3RuntimeStores(j or journal(name="classifier-a"), max_records=10000)
    result.initialize()
    result.acquire(str(uuid4()))
    return result


def cloud_agent(s):
    from .flywheel_test import agent
    from .s3_batch_storage import S3BatchStorage
    from .s3_optimizer_transport import S3OptimizerTransport

    complete = agent([])
    ledger = S3BatchStorage(journal(s.journal.payloads.client, name="optimizer-budget"), max_records=1000)
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
    other = S3RuntimeStores(j, max_records=10000)
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
    s = S3RuntimeStores(j, max_records=10000)
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


def test_runtime_size_limit_does_not_partially_commit_learning_aliases():
    from .s3_runtime_storage import S3RuntimeStores

    j = journal(name="small")
    s = S3RuntimeStores(j, max_records=2)
    s.initialize()
    s.acquire(str(uuid4()))
    learn = s.learning_storage
    learn.begin("rubric", "old", '{"pending_evaluation":true}')
    learn.complete("rubric", "old", '{"pending_evaluation":true}')
    learn.begin("rubric", "new", "{}")
    with pytest.raises(ValueError, match="record budget"):
        learn.complete("rubric", "new", '{"done":true}', aliases=("extra",), clear_pending_key="old")
    assert learn.artifact("rubric", "new") == ("pending", "{}")
    assert learn.artifact("rubric", "old") == ("complete", '{"pending_evaluation":true}')
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

    result = S3RuntimeStores(journal(client, name=name), max_records=10000, cache_owned=True, **options)
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


def test_flush_and_refresh_publish_held_events():
    s = owned_stores()
    s.runtime_storage.append_event('{"kind":"a"}')
    s.flush()
    assert len(s.journal.read().value["data"]["events"]) == 1
    s.runtime_storage.append_event('{"kind":"b"}')
    s.refresh()
    assert len(s.journal.read().value["data"]["events"]) == 2
    s.flush()  # nothing held: no commit
    revision = heads(s)
    s.flush()
    assert heads(s) == revision
    s.close()


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
    """(commits, bytes) of canonical transaction payloads written, counted at the S3 boundary."""
    puts = [r for kind, r in client.calls if kind == "put" and "/payloads/" in r["Key"]]
    return len(puts), sum(len(r["Body"]) for r in puts)


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

    result = S3RuntimeStores(journal(client, name=name), max_records=10000, cache_owned=True, coalesce_events=coalesce)
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
    # reservation, completion and a release carrying the last events = 4. Each replay is acquire + release.
    assert before_commits == 3 * 12 + 2 * 3 * 2
    assert after_commits == 3 * 4 + 2 * 3 * 2
    assert after_commits <= before_commits * 0.55
    assert after_bytes <= before_bytes * 0.55
