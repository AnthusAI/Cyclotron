"""Cloud catalog/definitions/run commands use one S3 authority, never SQLite."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from .s3_journal_test import journal
from .workspace_catalog_test import configuration


def store(j=None):
    from .s3_console_storage import S3ConsoleStorage

    result = S3ConsoleStorage(j or journal(name="console"))
    result.initialize()
    return result


def seed(s):
    s.save_classifier("topic", "Topic", configuration())
    s.save_item_list("papers", "Papers")
    s.upsert_list_items(
        "papers", [{"id": "one", "occurred_at": "2026-01-01", "values": {"text": "Synthetic 雪", "n": 9007199254740993}}]
    )


def test_classifier_advancement_and_definition_activation_are_one_canonical_cas(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("cloud console opened SQLite")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    s = store()
    seed(s)
    first = s.save_cyclotron_definition("lab", "Lab", [{"id": "topic", "revision": 1}], {})
    s.save_classifier("topic", "Updated", {**configuration(), "question": "Changed?"})
    current = s.cyclotron_definition("lab")
    assert current["revision"] == 2 and current["classifiers"] == [{"id": "topic", "revision": 2}]
    assert s.cyclotron_definition("lab", 1) == first
    s.activate_cyclotron_definition("lab", 1)
    assert s.cyclotron_definition("lab") == first


def test_catalog_feedback_retraction_and_displayed_prediction_are_immutable_and_restartable():
    s = store()
    seed(s)
    vote = s.label_item("topic", 1, "papers", "one", 1, "science", "Original rationale 雪", "vote")
    assert s.label_item("topic", 1, "papers", "one", 1, "science", "Original rationale 雪", "vote") == vote
    with pytest.raises(ValueError):
        s.label_item("topic", 1, "papers", "one", 1, "other", "changed", "vote")
    s.retract_item_label("vote", "undo")
    assert s.item_labels("papers", "one", 1) == []
    s.record_item_prediction("not-a-run", "papers", "one", 1, {"label": "science"})
    with pytest.raises(ValueError):
        s.record_item_prediction("not-a-run", "papers", "one", 1, {"label": "other"})
    other = store(journal(s.journal.payloads.client, name="console"))
    assert other.item_revision("papers", "one", 1)["values"]["n"] == 9007199254740993
    assert other.item_labels("papers", "one", 1) == []


def test_cloud_run_commands_are_fenced_and_never_recovered_by_timeout():
    s = store()
    run = s.create_run("Live", "live", {})
    queued = s.command(run["id"], "prepare-once", "prepare", {})
    assert queued == s.command(run["id"], "prepare-once", "prepare", {})
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner)
    assert claimed["id"] == queued["id"] and claimed["claim_owner"] == owner
    assert s.claim_command(owner=str(uuid4())) is None
    with pytest.raises(ValueError):
        s.finish_command(claimed["id"], "completed", {"finished": True})
    with pytest.raises(ValueError):
        s.finish_command(claimed["id"], "completed", {}, owner=str(uuid4()), fence=claimed["claim_fence"])
    with pytest.raises(ValueError):
        s.recover_interrupted()
    s.finish_command(claimed["id"], "completed", {"finished": True}, owner=owner, fence=claimed["claim_fence"])
    assert s.run(run["id"])["status"] == "completed"
    assert s.jobs(run["id"])[0]["status"] == "completed"


def test_independent_workers_cannot_claim_the_same_queued_command():
    s = store()
    run = s.create_run("Live", "live", {})
    s.command(run["id"], "once", "prepare", {})

    def claim(_):
        return store(journal(s.journal.payloads.client, name="console")).claim_command(owner=str(uuid4()))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    assert sum(result is not None for result in results) == 1


def test_portable_native_console_import_remains_complete_and_has_no_sqlite_cloud_fallback(tmp_path, monkeypatch):
    from .catalog_storage_test import populated

    native = populated(tmp_path / "native.sqlite")
    snapshot = native.export_storage()

    def forbidden(*args, **kwargs):
        raise AssertionError("cloud migration opened SQLite")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    s = store()
    s.restore_storage(snapshot)
    assert s.export_storage() == snapshot
    assert s.item_revision("papers", "one", 1)["values"]["text"] == "雪"
    with pytest.raises(ValueError):
        s.restore_storage(snapshot)


def test_command_bound_mutations_are_fenced_with_claim_and_run_boundary():
    s = store()
    item = {"id": "one", "values": {"text": "Synthetic"}}
    run = s.create_run("Live", "live", {}, items=[item])
    other = s.create_run("Other", "live", {}, items=[item])
    s.command(run["id"], "once", "prepare", {})
    owner = str(uuid4())
    job = s.claim_command(owner=owner)
    bound = s.for_command(job["id"], owner, job["claim_fence"])
    with pytest.raises(ValueError):
        s.update_item(run["id"], "one", reviewed=True)
    with pytest.raises(ValueError):
        bound.update_item(other["id"], "one", reviewed=True)
    bound.update_item(run["id"], "one", prediction={"label": "science"})
    assert bound.current_item(run["id"])["prediction"] == {"label": "science"}
    bound.append_event(run["id"], "prepared", {"kind": "prepared"})
    bound.finish_command(job["id"], "completed", {"finished": True})
    before = s.export_storage()
    with pytest.raises(ValueError):
        bound.update_item(run["id"], "one", reviewed=True)
    with pytest.raises(ValueError):
        bound.append_event(run["id"], "stale", {"kind": "stale"})
    assert s.export_storage() == before


def test_priming_accepts_exact_human_rationale_atomically_into_s3_catalog_without_sqlite(monkeypatch):
    import base64
    import json

    from .priming import PrimingService

    def forbidden(*args, **kwargs):
        raise AssertionError("cloud priming opened SQLite")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    s = store()
    seed(s)
    s.save_item_list("examples", "Examples")
    service = PrimingService(s)
    content = base64.b64encode(
        json.dumps(
            [
                {
                    "id": "prime",
                    "occurred_at": "2026-01-01",
                    "values": {"text": "Synthetic prime"},
                    "labels": {"topic": {"label": "science", "rationale": "Exact human rationale 雪"}},
                }
            ]
        ).encode()
    ).decode()
    draft = service.start_file("draft", "examples", [{"id": "topic", "revision": 1}], "json", content)
    accepted = service.accept("draft", draft["version"], None)
    assert accepted == service.accept("draft", draft["version"], None)
    assert (
        service.examples("examples", [{"id": "topic", "revision": 1}])["topic"][0].context["human_feedback"]
        == "Exact human rationale 雪"
    )
    assert s.export_storage()["tables"]["priming_drafts"]
    service.close()


# Reuse existing public native-domain contracts against the cloud composition.
# Tests of physical SQLite migrations/SQL triggers intentionally stay native.
CONTRACTS = {
    "workspace_catalog_test": [
        "test_classifier_revisions_preserve_order_and_old_configuration",
        "test_classifier_history_returns_immutable_revisions_in_order_without_changing_active_membership",
        "test_item_upserts_are_idempotent_revisioned_and_chronological",
        "test_an_item_list_snapshot_includes_all_latest_revisions_in_one_ordered_read",
        "test_invalid_item_batch_is_atomic_and_lists_do_not_share_items",
    ],
    "cyclotron_definitions_test": [
        "test_cyclotron_definitions_exist_without_runs_and_pin_ordered_classifier_revisions",
        "test_classifier_edits_create_revisions_for_each_active_containing_cyclotron",
        "test_invalid_membership_does_not_partially_create_a_version",
        "test_invalid_shared_settings_cannot_publish_a_partial_revision",
        "test_restoring_and_editing_old_membership_appends_history_without_replacing_newer_definitions",
        "test_run_definition_registration_retains_shared_trigger_cadences",
        "test_run_edition_numbers_cannot_overwrite_independent_definition_revisions",
    ],
    "cyclotrons_test": [
        "test_an_added_classifier_starts_a_fresh_version_with_historical_items_first",
        "test_changing_one_classifier_changes_the_complete_cyclotron_checkpoint",
        "test_a_cyclotron_cannot_be_extended_while_a_submission_is_in_progress",
        "test_protected_comparison_preflight_reads_pinned_checkpoints_without_writing_or_models",
        "test_a_supporting_question_becomes_an_unlabeled_output_of_a_new_version",
        "test_promotion_needs_a_checkpointed_question_and_a_new_classifier_id",
    ],
    "web_store_test": [
        "test_operating_limits_can_change_without_resetting_labels_or_classifier_configuration",
        "test_event_ingestion_is_idempotent_but_cannot_rewrite_a_committed_event",
        "test_run_history_and_cursor_survive_restart_without_mixing_runs",
        "test_event_batches_roll_back_together_and_a_page_must_hold_at_least_one_event",
        "test_event_batches_and_events_past_the_old_count_and_size_caps_are_kept",
        "test_final_summary_is_persisted_without_rewriting_run_configuration",
        "test_a_run_lists_every_job_newest_first_past_the_old_twenty_job_window",
    ],
}


@pytest.mark.parametrize("module_name,test_name", [(module, name) for module, names in CONTRACTS.items() for name in names])
def test_existing_public_domain_contract_runs_on_s3_without_sqlite(module_name, test_name, tmp_path, monkeypatch):
    import importlib
    from hashlib import sha256

    from .s3_journal_test import ConditionalS3

    module = importlib.import_module("decision_flywheel." + module_name)
    client = ConditionalS3()

    def factory(path):
        return store(journal(client, name=sha256(str(path).encode()).hexdigest()))

    monkeypatch.setattr(module, "WebStore", factory)

    def forbidden(*args, **kwargs):
        raise AssertionError("cloud domain contract opened SQLite or local directory")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    monkeypatch.setattr("pathlib.Path.mkdir", forbidden)
    getattr(module, test_name)(tmp_path)


def test_unknown_command_claim_receipt_cannot_become_execution_permission_by_rereading():
    from .s3_journal import JournalUncertain

    s = store()
    run = s.create_run("Live", "live", {})
    s.command(run["id"], "once", "prepare", {})
    owner = str(uuid4())
    s.journal.payloads.client.lose = lambda key: key == s.journal.head_key
    with pytest.raises(JournalUncertain):
        s.claim_command(owner=owner)
    job = s.jobs(run["id"])[0]
    assert job["status"] == "running"
    with pytest.raises(ValueError, match="receipt"):
        s.for_command(job["id"], owner, 1)
    with pytest.raises(ValueError, match="receipt"):
        s.finish_command(job["id"], "completed", {}, owner=owner, fence=1)
    assert s.claim_command(owner=str(uuid4())) is None


def test_lost_file_draft_creation_receipt_can_retry_same_raw_input_without_implicit_acceptance():
    import base64
    import json

    from .priming import PrimingService
    from .s3_journal import JournalUncertain

    s = store()
    seed(s)
    s.save_item_list("examples", "Examples")
    service = PrimingService(s)
    content = base64.b64encode(
        json.dumps(
            [
                {
                    "id": "prime",
                    "occurred_at": "2026-01-01",
                    "values": {"text": "Synthetic"},
                    "labels": {"topic": {"label": "science", "rationale": "Explicit human judgment"}},
                }
            ]
        ).encode()
    ).decode()
    s.journal.payloads.client.lose = lambda key: key == s.journal.head_key
    with pytest.raises(JournalUncertain):
        service.start_file("draft", "examples", [{"id": "topic", "revision": 1}], "json", content)
    assert service.get("draft")["status"] == "starting"
    draft = service.start_file("draft", "examples", [{"id": "topic", "revision": 1}], "json", content)
    assert draft["status"] == "preview" and s.list_items("examples") == []
    assert service.get("draft")["source"]["content"] == content
    service.close()


def test_actual_account_scoped_graphql_switches_s3_workspaces_and_denies_access_without_sqlite(monkeypatch):
    from fastapi.testclient import TestClient

    from .batch_storage import StorageScope
    from .prospectus_accounts import (
        AccountAccessDenied,
        AccountsClient,
        AccountWorkspace,
        AccountWorkspaceAccess,
        VerifiedAccountSession,
    )
    from .s3_journal_test import ConditionalS3
    from .web_api import create_app

    client = ConditionalS3()
    account_a, account_b = str(uuid4()), str(uuid4())
    person = str(uuid4())

    def forbidden(*args, **kwargs):
        raise AssertionError("hosted GraphQL opened SQLite or local directory")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    monkeypatch.setattr("pathlib.Path.mkdir", forbidden)
    workspaces = {a: store(journal(client, StorageScope(a, "workspace"), name="console")) for a in (account_a, account_b)}
    for name, s in workspaces.items():
        s.create_run("Only " + name, "recorded", {})

    def central(method, path, token, body=None):
        account = path.split("/")[3]
        if token == "revoked" or account not in workspaces or token == "viewer" and body["operation"] == "console:write":
            raise AccountAccessDenied("account access refused")
        return dict(
            allowed=True,
            accountId=account,
            personId=person,
            product="cyclotron",
            role="viewer" if token == "viewer" else "member",
            operation=body["operation"],
            revision=1,
        )

    async def session(connection):
        token = connection.cookies.get("synthetic-session")
        return VerifiedAccountSession(token, person, connection.cookies.get("selected-account", account_a)) if token else None

    access = AccountWorkspaceAccess(AccountsClient(central), session, lambda account: AccountWorkspace(workspaces[account]))
    http = TestClient(create_app(None, account_access=access))
    query = {"query": "{runs{id name} classifiers cyclotrons cyclotronDefinitions}"}
    assert http.post("/graphql", json=query).status_code == 401
    http.cookies.set("synthetic-session", "member")
    http.cookies.set("selected-account", account_a)
    first = http.post("/graphql", json=query).json()
    assert "errors" not in first and first["data"]["runs"][0]["name"] == "Only " + account_a
    http.cookies.set("selected-account", account_b)
    second = http.post("/graphql", json=query).json()
    assert second["data"]["runs"][0]["name"] == "Only " + account_b
    http.cookies.set("synthetic-session", "viewer")
    before = len(client.calls)
    denied = http.post("/graphql", json={"query": 'mutation{saveItemList(identifier:"x",name:"Denied")}'}).json()
    assert "errors" in denied and len(client.calls) == before
    http.cookies.set("synthetic-session", "revoked")
    before = len(client.calls)
    assert "errors" in http.post("/graphql", json=query).json() and len(client.calls) == before
    http.cookies.set("synthetic-session", "member")
    http.cookies.set("selected-account", str(uuid4()))
    before = len(client.calls)
    assert "errors" in http.post("/graphql", json=query).json() and len(client.calls) == before


def test_authorized_job_and_original_actor_commit_together_and_cannot_be_rebound():
    from dataclasses import replace

    from .queued_account_authorization_test import fixture

    boundary, _, _, actor, _ = fixture()
    s = store()
    run = s.create_run("Live", "live", {})
    import asyncio

    actor = asyncio.run(
        boundary.capture(boundary.sessions.get_ref(actor.session_ref), actor.account_id, run["id"], actor.binding)
    )
    job = s.enqueue_authorized_command(run["id"], "once", "prepare", {}, actor)
    assert job == s.enqueue_authorized_command(run["id"], "once", "prepare", {}, actor)
    with pytest.raises(ValueError):
        s.enqueue_authorized_command(run["id"], "once", "prepare", {}, replace(actor, person_id=str(uuid4())))
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner)
    assert s.command_actor(job["id"], owner, claimed["claim_fence"]) == actor
    fresh = store(journal(s.journal.payloads.client, name="console"))
    with pytest.raises(ValueError, match="receipt"):
        fresh.command_actor(job["id"], owner, claimed["claim_fence"])
    seen = fresh.command_recovery(job["id"])
    assert {k: seen[k] for k in ("jobId", "runId", "status", "owner", "fence", "hasActor", "automaticTakeover")} == {
        "jobId": job["id"],
        "runId": run["id"],
        "status": "running",
        "owner": owner,
        "fence": 1,
        "hasActor": True,
        "automaticTakeover": False,
    }
    assert seen["stuck"] is False and seen["blockedJobs"] == 0 and seen["claimedAt"]
    assert [row["jobId"] for row in seen["runQueue"]] == [job["id"]]


def test_legacy_jobs_have_no_invented_actor_and_cannot_enter_authorized_worker():
    s = store()
    run = s.create_run("Live", "live", {})
    job = s.command(run["id"], "legacy", "prepare", {})
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner)
    with pytest.raises(ValueError, match="actor"):
        s.command_actor(job["id"], owner, claimed["claim_fence"])


def test_legacy_command_cannot_acquire_a_new_actor_through_an_idempotency_retry():
    import asyncio

    from .queued_account_authorization_test import fixture

    boundary, _, _, actor, _ = fixture()
    s = store()
    run = s.create_run("Live", "live", {})
    actor = asyncio.run(
        boundary.capture(boundary.sessions.get_ref(actor.session_ref), actor.account_id, run["id"], actor.binding)
    )
    s.command(run["id"], "legacy", "prepare", {})
    with pytest.raises(ValueError, match="legacy"):
        s.enqueue_authorized_command(run["id"], "legacy", "prepare", {}, actor)


def test_v1_console_migrates_without_inventing_job_actors():
    from .s3_console_storage import initial

    j = journal(name="old-console")
    value = {k: initial()[k] for k in ("snapshot", "claims")}
    value["schema"] = "cyclotron-s3-console/v1"
    j.initialize(value)
    s = store(j)
    run = s.create_run("Local import", "live", {})
    assert j.read().value["schema"] == "cyclotron-s3-console/v4"
    assert j.read().value["actors"] == {}
    assert s.run(run["id"])["name"] == "Local import"


def cached_store(client=None, cache=None, **options):
    from .catalog_snapshot_cache import CatalogSnapshotCache
    from .s3_console_storage import S3ConsoleStorage

    j = journal(client, name="console")
    result = S3ConsoleStorage(j, snapshot_cache=CatalogSnapshotCache() if cache is None else cache, **options)
    result.initialize()
    return result


def catalog_payload_gets(client, since):
    return [r["Key"] for kind, r in client.calls[since:] if kind == "get" and not r["Key"].endswith(("head.json", "anchor.json"))]


def test_it_reuses_a_decoded_catalog_while_the_head_is_unchanged_and_reloads_when_it_moves():
    from .catalog_snapshot_cache import CatalogSnapshotCache

    cache = CatalogSnapshotCache()
    writer = cached_store()  # another process: its commits are not in this memory
    seed(writer)
    client = writer.journal.payloads.client
    reader = cached_store(client, cache)
    reader.item_lists()
    mark = len(client.calls)
    reader.item_lists()
    reader.item_lists()
    assert catalog_payload_gets(client, mark) == []
    assert sum(1 for kind, r in client.calls[mark:] if kind == "get" and r["Key"].endswith("head.json")) == 2
    writer.save_item_list("more", "More")
    mark = len(client.calls)
    assert {row["id"] for row in reader.item_lists()} == {"papers", "more"}
    assert len(catalog_payload_gets(client, mark)) == 2  # the new head state, and the one table that changed


def test_it_logs_a_catalog_read_phase_with_cache_hit_or_miss():
    phases = []
    s = cached_store(phase_log=lambda phase, started, **fields: phases.append((phase, fields["cache"])))
    s.item_lists()
    s.item_lists()
    assert phases[-2:] == [("catalog_read", "miss"), ("catalog_read", "hit")]


def test_a_write_after_a_cached_read_still_conflicts_correctly_on_a_moved_head():
    from .storage_errors import StorageConflict

    s = cached_store()
    seed(s)
    client = s.journal.payloads.client
    rival = cached_store(client)
    s.item_lists()
    stale = s._record()
    rival.save_item_list("rival", "Rival")
    with pytest.raises(StorageConflict):
        s.journal.commit(stale, {**stale.value, "claims": {}})
    s.save_item_list("mine", "Mine")
    assert {row["id"] for row in s.item_lists()} == {"papers", "rival", "mine"}


def test_a_write_never_alters_the_cached_snapshot_it_started_from():
    from copy import deepcopy

    s = cached_store()
    seed(s)
    before = s._record()
    frozen = deepcopy(before.value)
    s.save_item_list("more", "More")
    assert before.value == frozen
    assert s.export_storage() is not s.export_storage()
    exported = s.export_storage()
    exported["tables"]["item_lists"].clear()
    assert {row["id"] for row in s.item_lists()} == {"papers", "more"}


def test_one_request_reads_the_catalog_once_across_resolvers_until_its_own_write():
    from .catalog_snapshot_cache import CatalogSnapshotCache

    writer = cached_store()
    seed(writer)
    client = writer.journal.payloads.client
    request = cached_store(client, CatalogSnapshotCache(), reuse_reads_within_request=True)
    mark = len(client.calls)
    for _ in range(7):
        request.item_lists()
    assert len(catalog_payload_gets(client, mark)) == 1
    heads = [r for kind, r in client.calls[mark:] if kind == "get" and r["Key"].endswith("head.json")]
    assert len(heads) == 1
    request.save_item_list("later", "Later")
    assert "later" in {row["id"] for row in request.item_lists()}
    request.release_request_snapshot()
    writer.save_item_list("outside", "Outside")
    assert "outside" in {row["id"] for row in request.item_lists()}


def test_initializing_an_existing_catalog_without_verification_spends_only_the_anchor_read():
    s = cached_store()
    seed(s)
    client = s.journal.payloads.client
    from .s3_console_storage import S3ConsoleStorage

    reopened = S3ConsoleStorage(journal(client, name="console"))
    mark = len(client.calls)
    reopened.initialize(verify_existing=False)
    keys = [r["Key"] for kind, r in client.calls[mark:] if kind == "get"]
    assert len(keys) == 1 and keys[0].endswith("anchor.json")


# --- constant-cost catalog: finished jobs, items and revisions leave the head --------------------------------


def standing(s=None):
    """A store with an operational list and a run that registers standing items into it, as the hosted worker does."""
    s = s or store()
    s.save_item_list("standing", "Standing")
    s.save_item_list("priming", "Priming")
    s.upsert_list_items("priming", [{"id": "primer", "occurred_at": "2026-01-01", "values": {"text": "Primer"}}])
    run = s.create_run("Standing", "live", {"item_list_id": "standing"})
    return s, run["id"]


def decide(s, run_id, n, *, values=None, request=None):
    """One decision: accept the command, claim it, register its item, finish it."""
    item = {"id": f"item-{n}", "values": values or {"text": f"Source {n}"}}
    job = s.command(run_id, request or f"req-{n}", "publication", {"bindings": {"alpha": "1"}, "item": item})
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner, job_id=job["id"])
    view = s.for_command(claimed["id"], owner, claimed["claim_fence"])
    view.register_standing_item(run_id, item["id"], item["values"])
    view.finish_command(claimed["id"], "completed", {"response": {"label": "yes", "n": n}})
    return job["id"]


def written(client, since):
    puts = [r for kind, r in client.calls[since:] if kind == "put"]
    gets = [r for kind, r in client.calls[since:] if kind == "get"]
    return len(puts), len(gets), sum(len(r["Body"]) for r in puts)


def test_the_bytes_a_catalog_writes_per_decision_do_not_depend_on_how_many_jobs_came_before():
    s, run_id = standing()
    client = s.journal.payloads.client
    costs = {}
    for n in range(1, 61):
        mark = len(client.calls)
        decide(s, run_id, n)
        costs[n] = written(client, mark)
    early, late = costs[10], costs[60]
    assert early[:2] == late[:2]  # the same number of writes and reads
    assert abs(late[2] - early[2]) < 64  # the same bytes, give or take the digits of a counter
    head = len(s.journal.read().value["snapshot"]["tables"]["web_jobs"])
    assert head == 1  # only the newest finished job, until the next commit moves it out


def backlog(queued, waiting):
    """A standing run with ``queued`` jobs waiting their turn, and a live run with ``waiting`` items shown for review."""
    s, run_id = standing()
    payload = {"bindings": {"alpha": "1"}, "item": {"id": "queued", "values": {"text": "Queued"}}}
    for n in range(queued):
        s.command(run_id, f"queued-{n}", "publication", payload)
    live = s.create_run("Live", "live", {}, items=[{"id": f"shown-{n}", "values": {"text": "x"}} for n in range(waiting)])
    for n in range(waiting):
        s.update_item(live["id"], f"shown-{n}", prediction={"label": "yes"})
    return s, run_id, live["id"]


def next_decision(s, run_id, n):
    """Accept one more command, then start, register and finish the job at the head of the run's queue."""
    item = {"id": f"item-{n}", "values": {"text": f"Source {n}"}}
    s.command(run_id, f"next-{n}", "publication", {"bindings": {"alpha": "1"}, "item": item})
    head = s.queue_head(run_id)
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner, job_id=head["id"])
    view = s.for_command(claimed["id"], owner, claimed["claim_fence"])
    view.register_standing_item(run_id, item["id"], item["values"])
    view.current_item(run_id)
    view.finish_command(claimed["id"], "completed", {"response": {"label": "yes", "n": n}})


def test_the_bytes_a_decision_writes_do_not_depend_on_how_many_jobs_are_queued_or_items_await_review():
    from .s3_console_storage import encoded

    costs, heads = {}, {}
    for size in (10, 500):
        s, run_id, live_id = backlog(size, size)
        client = s.journal.payloads.client
        next_decision(s, run_id, 1)  # settle the backlog's own last commit
        mark = len(client.calls)
        next_decision(s, run_id, 2)
        costs[size] = written(client, mark)
        heads[size] = len(encoded(s.journal.read().value))
        assert s.current_item(live_id)["item"]["id"] == "shown-0"
        assert s.queue_state(s.jobs(run_id)[0]["id"])["position"] == size + 1 - 2
    assert costs[10][:2] == costs[500][:2]  # the same number of writes and reads
    assert abs(costs[500][2] - costs[10][2]) < 64  # the same bytes, give or take the digits of a counter
    assert abs(heads[500] - heads[10]) < 64


def labelled(labels, padding):
    """A standing run whose config carries ``padding`` bytes, plus ``labels`` human labels on primed items."""
    s = store()
    s.save_classifier("topic", "Topic", configuration())
    s.save_item_list("standing", "Standing")
    s.save_item_list("priming", "Priming")
    items = [{"id": f"p{n}", "occurred_at": "2026-01-01", "values": {"text": f"Primer {n}"}} for n in range(labels)]
    s.upsert_list_items("priming", items or [{"id": "p", "occurred_at": "2026-01-01", "values": {"text": "Primer"}}])
    run = s.create_run("Standing", "live", {"item_list_id": "standing", "notes": "x" * padding})
    for n in range(labels):
        s.label_item("topic", 1, "priming", f"p{n}", 1, "science", "Because", f"label-{n}")
    return s, run["id"]


def test_the_bytes_a_decision_writes_do_not_depend_on_how_many_labels_exist_or_how_large_run_configs_are():
    costs = {}
    for labels, padding in ((10, 10), (500, 10), (10, 200_000)):
        s, run_id = labelled(labels, padding)
        client = s.journal.payloads.client
        decide(s, run_id, 1)  # settle the setup's own last commit
        mark = len(client.calls)
        decide(s, run_id, 2)
        costs[labels, padding] = written(client, mark)
    base = costs[10, 10]
    for other in (costs[500, 10], costs[10, 200_000]):
        assert other[:2] == base[:2]  # the same number of writes and reads
        assert abs(other[2] - base[2]) < 64  # the same bytes, give or take the digits of a counter


def test_labels_keep_their_latest_active_value_retractions_and_request_ids_after_leaving_the_head():
    s, _ = labelled(3, 10)
    s.label_item("topic", 1, "priming", "p0", 1, "sport", "Changed my mind", "label-0-again")
    s.retract_item_label("label-1", "undo-1")
    s.save_item_list("later", "Later")  # moves every label and retraction out of the head
    fresh = store(journal(s.journal.payloads.client, name="console"))
    assert fresh.journal.read().value["snapshot"]["tables"]["item_feedback"] == []
    assert [v["label"] for v in fresh.item_labels("priming", "p0", 1)] == ["sport"]
    assert fresh.item_labels("priming", "p1", 1) == []
    assert [v["request_id"] for v in fresh.item_labels("priming", "p2", 1)] == ["label-2"]
    assert fresh.label_item("topic", 1, "priming", "p2", 1, "science", "Because", "label-2")["request_id"] == "label-2"
    with pytest.raises(ValueError, match="different content"):
        fresh.label_item("topic", 1, "priming", "p2", 1, "sport", "Because", "label-2")
    assert fresh.retract_item_label("label-1", "undo-1")["feedback_request_id"] == "label-1"
    with pytest.raises(ValueError, match="current label"):
        fresh.retract_item_label("label-0", "undo-0")  # a later label replaced it
    with pytest.raises(ValueError, match="already retracted"):
        fresh.retract_item_label("label-1", "undo-1-again")
    fresh.retract_item_label("label-0-again", "undo-0")
    assert fresh.item_labels("priming", "p0", 1) == []
    assert [r["request_id"] for r in fresh.export()["tables"]["item_feedback"]] == [
        "label-0",
        "label-1",
        "label-2",
        "label-0-again",
    ]


def test_run_events_list_from_a_cursor_after_they_leave_the_head():
    s, run_id = standing()
    job = s.command(run_id, "req", "publication", {"bindings": {"alpha": "1"}, "item": {"id": "a", "values": {}}})
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner, job_id=job["id"])
    view = s.for_command(claimed["id"], owner, claimed["claim_fence"])
    for n in range(7):
        view.append_event(run_id, f"source-{n}", {"kind": "step", "n": n})
    view.append_event(run_id, "source-3", {"kind": "step", "n": 3})  # the same event again changes nothing
    view.finish_command(claimed["id"], "completed", {"response": 1})
    s.save_item_list("later", "Later")
    fresh = store(journal(s.journal.payloads.client, name="console"))
    every = fresh.all_events(run_id)
    assert [e["payload"]["n"] for e in every] == list(range(7)) and fresh.event_cursor(run_id) == every[-1]["sequence"]
    assert [e["payload"]["n"] for e in fresh.events(run_id, after=every[2]["sequence"], limit=3)] == [3, 4, 5]
    assert fresh.events(run_id, after=every[-1]["sequence"]) == []
    assert fresh.counts(run_id)["predictions"] == 0


def test_rarely_changing_tables_are_written_only_when_they_change():
    s, run_id = labelled(1, 50_000)
    client = s.journal.payloads.client
    mark = len(client.calls)
    decide(s, run_id, 1)
    big = [r for kind, r in client.calls[mark:] if kind == "put" and len(r["Body"]) > 40_000]
    assert big == []  # the run's config was not rewritten
    fresh = store(journal(client, name="console"))
    assert fresh.run(run_id)["config"]["notes"] == "x" * 50_000
    fresh.update_run_limits(run_id, 5, 5)
    assert store(journal(client, name="console")).run(run_id)["config"]["max_requests"] == 5


def test_a_v3_head_moves_its_labels_and_events_out_at_its_next_commit():
    from .s3_console_storage import _V4_COLD

    s, _ = labelled(3, 10)
    with s.transaction():  # one commit, so these labels are still in the head
        s.label_item("topic", 1, "priming", "p0", 1, "sport", "Later", "label-later")
    record = s.journal.read()
    value = s._read()
    old = {k: v for k, v in value.items() if k != "parts"}
    old["schema"] = "cyclotron-s3-console/v3"
    old["cold"] = {
        k: ({t: x for t, x in v.items() if t not in _V4_COLD} if k in ("rowids", "ordinals", "hot") else v)
        for k, v in value["cold"].items()
    }
    rows_left = len(value["snapshot"]["tables"]["item_feedback"])
    # A v3 head holds every label whole; put back the ones already filed as records.
    old["snapshot"] = {
        **value["snapshot"],
        "tables": {**value["snapshot"]["tables"], "item_feedback": s.export()["tables"]["item_feedback"]},
    }
    old["snapshot"]["tables"]["item_feedback"] = [
        {**r, "_rowid": r["sequence"]} for r in old["snapshot"]["tables"]["item_feedback"]
    ]
    s.journal.commit(record, old)
    fresh = store(journal(s.journal.payloads.client, name="console"))
    assert rows_left >= 1 and [v["label"] for v in fresh.item_labels("priming", "p0", 1)] == ["sport"]
    fresh.save_item_list("later", "Later")
    head = fresh.journal.read().value
    assert head["schema"] == "cyclotron-s3-console/v4" and head["snapshot"]["tables"]["item_feedback"] == []
    reopened = store(journal(s.journal.payloads.client, name="console"))
    assert [v["label"] for v in reopened.item_labels("priming", "p0", 1)] == ["sport"]
    assert len(reopened.export()["tables"]["item_feedback"]) == 4


def test_queued_jobs_start_in_acceptance_order_from_their_records():
    from .storage_errors import CommandNotReady

    s, run_id, _ = backlog(5, 0)
    fresh = store(journal(s.journal.payloads.client, name="console"))
    assert fresh.journal.read().value["snapshot"]["tables"]["web_jobs"] == []  # every waiting job is a record
    order = [j["id"] for j in reversed(fresh.jobs(run_id))]
    assert [fresh.queue_state(job)["position"] for job in order] == [0, 1, 2, 3, 4]
    assert fresh.ready_heads() == [{"id": order[0], "run_id": run_id}]
    with pytest.raises(CommandNotReady):
        fresh.claim_command(owner=str(uuid4()), job_id=order[1])
    for expected in order:
        owner = str(uuid4())
        claimed = fresh.claim_command(owner=owner)
        assert claimed["id"] == expected and fresh.claim_command(owner=str(uuid4())) is None
        assert fresh.command_recovery(expected)["blockedJobs"] == len(order) - order.index(expected) - 1
        fresh.finish_command(expected, "completed", {"n": 1}, owner=owner, fence=claimed["claim_fence"])
    assert not fresh.run_has_active_work(run_id) and fresh.ready_heads() == []
    assert [j["status"] for j in fresh.jobs(run_id)] == ["completed"] * 5


def test_items_awaiting_review_are_found_in_order_without_their_rows_in_the_head():
    s, _, live_id = backlog(0, 4)
    s.update_item(live_id, "shown-0", reviewed=True)
    s.update_item(live_id, "shown-2", reviewed=True)
    fresh = store(journal(s.journal.payloads.client, name="console"))
    assert fresh.journal.read().value["cold"]["review"][live_id] == [[2, 2], [4, 4]]
    assert fresh.current_item(live_id)["item"]["id"] == "shown-1"
    fresh.update_item(live_id, "shown-1", reviewed=True)
    assert fresh.current_item(live_id)["item"]["id"] == "shown-3"
    fresh.update_item(live_id, "shown-0", reviewed=False)  # an undo puts an item back in front
    assert fresh.current_item(live_id)["item"]["id"] == "shown-0"


def test_finished_jobs_items_and_revisions_still_list_and_serve_after_they_leave_the_head():
    s, run_id = standing()
    ids = [decide(s, run_id, n) for n in range(1, 6)]
    reopened = store(journal(s.journal.payloads.client, name="console"))
    assert [j["id"] for j in reopened.jobs(run_id)] == list(reversed(ids))
    assert reopened.job_result(ids[0]) == {"response": {"label": "yes", "n": 1}}
    assert reopened.queue_state(ids[0])["status"] == "completed"
    # The same request id returns the same job; different content is refused.
    again = reopened.command(run_id, "req-1", "publication", reopened.jobs(run_id)[-1]["payload"])
    assert again["id"] == ids[0]
    with pytest.raises(ValueError, match="different content"):
        reopened.command(run_id, "req-1", "publication", {"bindings": {"alpha": "2"}, "item": {"id": "x", "values": {}}})
    assert [i["id"] for i in reopened.items(run_id)] == [f"item-{n}" for n in range(1, 6)]
    assert [i["id"] for i in reopened.snapshot_items("standing")] == [f"item-{n}" for n in range(1, 6)]
    assert reopened.item_revision("standing", "item-3", 1)["values"] == {"text": "Source 3"}
    assert {r["id"]: r["count"] for r in reopened.item_lists()} == {"standing": 5, "priming": 1}
    assert reopened.export_storage()["tables"]["web_jobs"][0]["id"] == ids[0]


def test_a_standing_item_registered_again_keeps_its_disjointness_and_its_newest_revision():
    s, run_id = standing()
    decide(s, run_id, 1)
    decide(s, run_id, 2)
    decide(s, run_id, 3)
    decide(
        s, run_id, 1, values={"text": "Changed"}, request="req-1-again"
    )  # a new immutable revision of an item that left the head
    decide(s, run_id, 4)
    reopened = store(journal(s.journal.payloads.client, name="console"))
    [item] = [i for i in reopened.items(run_id) if i["id"] == "item-1"]
    assert item["values"] == {"text": "Changed"} and item["revision"] == 2
    assert reopened.item_revision("standing", "item-1", 1)["values"] == {"text": "Source 1"}
    with pytest.raises(ValueError, match="disjoint"):
        job = s.command(run_id, "req-primer", "publication", {"bindings": {"alpha": "1"}, "item": {"id": "primer", "values": {}}})
        owner = str(uuid4())
        claimed = s.claim_command(owner=owner, job_id=job["id"])
        s.for_command(claimed["id"], owner, claimed["claim_fence"]).register_standing_item(run_id, "primer", {"t": 1})


def test_a_run_a_worker_ever_claimed_in_stays_worker_only_after_its_jobs_leave_the_head():
    s, run_id = standing()
    decide(s, run_id, 1)
    decide(s, run_id, 2)
    with pytest.raises(ValueError, match="command-bound"):
        s.set_status(run_id, "paused")


def old_catalog():
    """A catalog in the whole-state (v2) layout production catalogs have today, with a running job and a review item."""
    from .s3_console_storage import SCHEMA_V2

    s, run_id = standing()
    ids = [decide(s, run_id, n) for n in range(1, 4)]
    live = s.create_run("Live", "live", {}, items=[{"id": "shown", "values": {"text": "Shown"}}])
    s.update_item(live["id"], "shown", prediction={"label": "yes"})
    running = s.command(run_id, "req-running", "publication", {"bindings": {"alpha": "1"}, "item": {"id": "r", "values": {}}})
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner, job_id=running["id"])
    value = s._read()
    # Export refuses while a job runs, so assemble the same complete tables directly.
    tables = {**value["snapshot"]["tables"]}
    for table in ("web_jobs", "web_items", "list_item_revisions"):
        tables[table] = s._all_rows(value, table)
    claims = {job_id: {"owner": str(uuid4()), "fence": 1, "at": "2026-10-01T00:00:00+00:00"} for job_id in ids}
    claims[running["id"]] = value["claims"][running["id"]]
    old = {"schema": SCHEMA_V2, "snapshot": {**value["snapshot"], "tables": tables}, "claims": claims, "actors": {}}
    j = journal(s.journal.payloads.client, name="old-catalog")
    j.initialize(old)
    return j, run_id, live["id"], ids, running["id"], (owner, claimed["claim_fence"])


def test_an_old_whole_state_catalog_still_lists_and_serves_its_jobs_and_items_and_migrates_on_its_first_write():
    from .s3_console_storage import S3ConsoleStorage, encoded

    j, run_id, live_id, ids, running, (owner, fence) = old_catalog()
    old_bytes = len(encoded(j.read().value))
    s = S3ConsoleStorage(j)
    # Reads before any write see the old catalog exactly.
    assert [job["id"] for job in s.jobs(run_id)] == [running, *reversed(ids)]
    assert s.job_result(ids[0]) == {"response": {"label": "yes", "n": 1}}
    assert s.command_recovery(ids[0])["owner"] is not None and s.command_recovery(running)["status"] == "running"
    assert s.current_item(live_id)["item"]["id"] == "shown"
    assert [i["id"] for i in s.snapshot_items("standing")] == ["item-1", "item-2", "item-3"]
    assert s.command(run_id, "req-2", "publication", s.jobs(run_id)[2]["payload"])["id"] == ids[1]
    assert j.read().value["schema"] == "cyclotron-s3-console/v2"  # an idempotent retry writes nothing
    # The first write migrates: the head names the old blob instead of carrying its rows.
    s.save_item_list("later", "Later")
    head = j.read().value
    assert head["schema"] == "cyclotron-s3-console/v4" and head["legacy"]["open"] == {run_id: 4}
    assert [r["id"] for r in head["snapshot"]["tables"]["web_jobs"]] == [running]
    assert len(encoded(head)) < old_bytes
    # A fresh process reads the migrated catalog the same way, and the running job still finishes under its fence.
    fresh = S3ConsoleStorage(journal(j.payloads.client, name="old-catalog"))
    assert [job["id"] for job in fresh.jobs(run_id)] == [running, *reversed(ids)]
    assert fresh.item_revision("standing", "item-2", 1)["values"] == {"text": "Source 2"}
    s._granted.add((running, owner, fence))
    s.finish_command(running, "completed", {"response": "done"}, owner=owner, fence=fence)
    s.save_item_list("after", "After")  # the next commit moves the finished job out of the head
    fresh = S3ConsoleStorage(journal(j.payloads.client, name="old-catalog"))
    assert fresh.job_result(running) == {"response": "done"} and fresh.jobs(run_id)[0]["status"] == "completed"
    assert j.read().value["snapshot"]["tables"]["web_jobs"] == []
    new = decide(fresh, run_id, 9)
    assert [job["id"] for job in fresh.jobs(run_id)][:2] == [new, running]
    assert fresh.export_storage()["tables"]["web_jobs"][-1]["id"] == new


def test_a_command_view_reuses_the_record_that_showed_its_claim_instead_of_rereading_the_head():
    from .catalog_snapshot_cache import CatalogSnapshotCache

    cache = CatalogSnapshotCache()
    s, run_id = standing(cached_store(cache=cache))
    client = s.journal.payloads.client
    job = s.command(run_id, "req", "publication", {"bindings": {"alpha": "1"}, "item": {"id": "a", "values": {}}})
    owner = str(uuid4())
    claimed = s.claim_command(owner=owner, job_id=job["id"])
    view = s.for_command(claimed["id"], owner, claimed["claim_fence"])
    other = cached_store(client, cache)  # another store in the same process, as the binding check uses
    mark = len(client.calls)
    for _ in range(5):
        other.run(run_id)
        view.items(run_id)
        view.current_item(run_id)
    assert not [r for kind, r in client.calls[mark:] if kind == "get" and r["Key"].endswith("head.json")]
    view.run(run_id)  # the execution fence itself always reads the head
    assert [r for kind, r in client.calls[mark:] if kind == "get" and r["Key"].endswith("head.json")]
    view.finish_command(claimed["id"], "completed", {"response": 1})
    mark = len(client.calls)
    other.run(run_id)  # the hold ended with the job
    assert [r for kind, r in client.calls[mark:] if kind == "get" and r["Key"].endswith("head.json")]


def test_an_item_state_an_abandoned_commit_wrote_never_hides_the_state_that_committed():
    from .catalog_cold_rows import ColdRows

    s = store()
    run = s.create_run("Live", "live", {}, items=[{"id": "a", "values": {"text": "A"}}])
    stale = s._item_row(s._read(), run["id"], "a")
    # A writer that lost its commit had already written the item's committed state into the next slot ...
    assert s._cold().put(ColdRows.item(run["id"], "a"), 1, {"row": stale, "ordinal": 1, "slot": 1})
    s.update_item(run["id"], "a", reviewed=True)  # ... while the commit that won changed the item.
    s.save_item_list("one", "One")  # this commit finds the slot taken, so the item keeps the head one commit longer
    assert s._read()["cold"]["hot"]["web_items"]
    s.save_item_list("two", "Two")
    assert not s._read()["snapshot"]["tables"]["web_items"]
    fresh = store(journal(s.journal.payloads.client, name="console"))
    assert fresh.unreviewed_items(run["id"]) == [] and fresh._item_row(fresh._read(), run["id"], "a")["reviewed"] == 1


def test_a_lookup_refused_with_access_denied_refuses_the_command_instead_of_creating_a_duplicate_job():
    from .record_lake import RecordUnavailable
    from .s3_journal_test import S3Error

    s, run_id = standing()
    first = decide(s, run_id, 1)
    decide(s, run_id, 2)  # the first job has left the head; its request id is found only by its record
    client = s.journal.payloads.client
    s = store(journal(client, name="console"))  # a fresh process: nothing about these records is in memory
    real = client.get_object

    def denied(**request):
        if "/records/catalog/" in request["Key"]:
            raise S3Error("AccessDenied")
        return real(**request)

    client.get_object = denied
    before = s.journal.read().value
    payload = {"bindings": {"alpha": "1"}, "item": {"id": "item-1", "values": {"text": "Source 1"}}}
    with pytest.raises(RecordUnavailable):
        s.command(run_id, "req-1", "publication", payload)
    with pytest.raises(RecordUnavailable):
        s.command(run_id, "req-new", "publication", payload)
    assert s.journal.read().value == before  # nothing accepted, so no second job for the same request
    client.get_object = real
    assert s.command(run_id, "req-1", "publication", payload)["id"] == first
    assert [j["id"] for j in s.jobs(run_id)].count(first) == 1
