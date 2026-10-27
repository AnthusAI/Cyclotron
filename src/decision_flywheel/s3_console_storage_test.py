"""Cloud catalog/definitions/run commands use one S3 authority, never SQLite."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from .s3_journal_test import journal
from .workspace_catalog_test import configuration


def store(j=None):
    from .s3_console_storage import S3ConsoleStorage

    result = S3ConsoleStorage(j or journal(name="console"), max_records=10000)
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
        "test_event_batches_roll_back_together_and_read_pages_are_bounded",
        "test_final_summary_is_persisted_without_rewriting_run_configuration",
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
    value = initial()
    value.pop("actors")
    value["schema"] = "cyclotron-s3-console/v1"
    j.initialize(value)
    s = store(j)
    run = s.create_run("Local import", "live", {})
    assert j.read().value["schema"] == "cyclotron-s3-console/v2"
    assert j.read().value["actors"] == {}
    assert s.run(run["id"])["name"] == "Local import"


def cached_store(client=None, cache=None, **options):
    from .catalog_snapshot_cache import CatalogSnapshotCache
    from .s3_console_storage import S3ConsoleStorage

    j = journal(client, name="console")
    result = S3ConsoleStorage(j, max_records=10000, snapshot_cache=cache or CatalogSnapshotCache(), **options)
    result.initialize()
    return result


def catalog_payload_gets(client, since):
    return [r["Key"] for kind, r in client.calls[since:] if kind == "get" and not r["Key"].endswith(("head.json", "anchor.json"))]


def test_it_reuses_a_decoded_catalog_while_the_head_is_unchanged_and_reloads_when_it_moves():
    from .catalog_snapshot_cache import CatalogSnapshotCache

    cache = CatalogSnapshotCache()
    writer = cached_store(cache=cache)
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
    assert len(catalog_payload_gets(client, mark)) == 1


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

    reopened = S3ConsoleStorage(journal(client, name="console"), max_records=10000)
    mark = len(client.calls)
    reopened.initialize(verify_existing=False)
    keys = [r["Key"] for kind, r in client.calls[mark:] if kind == "get"]
    assert len(keys) == 1 and keys[0].endswith("anchor.json")
