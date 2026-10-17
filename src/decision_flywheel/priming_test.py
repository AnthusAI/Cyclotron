"""Synthetic HTTP job contract, explicit human acceptance and persistence firewalls."""

import base64
import json

import httpx
import pytest

from .priming import HTTPTransformer, PrimingService
from .web_store import WebStore

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "text": {"type": "string", "minLength": 1}},
    "required": ["title", "text"],
    "additionalProperties": False,
}
ITEM = {
    "id": "synthetic-paper",
    "occurred_at": "2026-10-14",
    "values": {"title": "Synthetic paper", "text": "Synthetic research content"},
    "provenance": {"source_url": "https://example.invalid/paper"},
}
LABELS = {
    "editor": {"label": "publish", "rationale": "Synthetic novelty"},
    "scope": {"label": "yes", "rationale": "Synthetic scope"},
}


@pytest.fixture
def setup(tmp_path):
    store = WebStore(tmp_path / "db")
    store.save_item_list("examples", "Examples")
    for cid, labels in [("editor", ["publish", "not publish"]), ("scope", ["yes", "no"])]:
        store.save_classifier(cid, cid, {"question": cid + "?", "classes": [{"label": v} for v in labels], "item_schema": SCHEMA})
    requests = []
    responses = [{"job_id": "job", "status": "running"}]

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=responses.pop(0))

    client = httpx.Client(transport=httpx.MockTransport(handle))
    service = PrimingService(
        store,
        transformers=[
            HTTPTransformer(
                "papyrus", "Papyrus", "https://transformer.invalid", ({"name": "url", "label": "Source URL", "required": True},)
            )
        ],
        client=client,
    )
    return store, service, requests, responses


def refs():
    return [{"id": "editor", "revision": 1}, {"id": "scope", "revision": 1}]


def start(service):
    return service.start_transform("draft", "examples", refs(), "papyrus", {"url": "https://example.invalid/paper"})


def test_async_clarification_revision_and_exact_representation_before_human_acceptance(setup):
    store, s, requests, responses = setup
    draft = start(s)
    assert draft["status"] == "running" and store.list_items("examples") == []
    responses.append(
        {
            "job_id": "job",
            "status": "clarification",
            "message": "Which version?",
            "fields": [{"name": "version", "label": "Version", "required": True}],
        }
    )
    assert s.poll("draft")["status"] == "clarification"
    responses.append({"job_id": "job", "status": "running"})
    draft = s.feedback("draft", draft["version"], "clarify-1", "Use the final version", {"version": "final"})
    responses.append({"job_id": "job", "status": "complete", "candidate": ITEM})
    draft = s.poll("draft")
    assert draft["status"] == "preview" and draft["rows"][0]["model_representation"]["editor"] == ITEM["values"]
    responses.append({"job_id": "job", "status": "running"})
    s.feedback("draft", draft["version"], "revise-1", "Abstract is missing details", {})
    with pytest.raises(ValueError):
        s.accept("draft", draft["version"], [LABELS])
    responses.append(
        {
            "job_id": "job",
            "status": "complete",
            "candidate": {**ITEM, "values": {**ITEM["values"], "text": "Revised synthetic abstract"}},
        }
    )
    draft = s.poll("draft")
    result = s.accept("draft", draft["version"], [LABELS])
    assert result == s.accept("draft", draft["version"], [LABELS])
    assert len(store.list_items("examples")) == 1
    item = store.list_items("examples")[0]
    assert item["provenance"]["priming"]["inputs"] == {"url": "https://example.invalid/paper"}
    assert item["provenance"]["priming"]["feedback"][-1]["reason"] == "Abstract is missing details"
    assert store.item_labels("examples", ITEM["id"], 1)[0]["comment"] == "Synthetic novelty"
    assert all("labels" not in json.loads(r.content) for r in requests if r.content)


def test_direct_file_preview_and_batch_rollback_when_any_row_is_invalid(setup):
    store, s, _, _ = setup
    content = base64.b64encode(
        json.dumps(
            [{**ITEM, "labels": LABELS}, {**ITEM, "id": "second", "labels": {"editor": {"label": "invented", "rationale": "x"}}}]
        ).encode()
    ).decode()
    draft = s.start_file("file", "examples", refs(), "json", content)
    assert draft["status"] == "invalid" and draft["errors"][0]["row"] == 2
    with pytest.raises(ValueError):
        s.accept("file", draft["version"], None)
    assert store.list_items("examples") == []


def test_cancellation_and_late_completion_never_import_and_retry_cancel_is_safe(setup):
    store, s, _, responses = setup
    start(s)
    responses.append({"job_id": "job", "status": "cancelled"})
    assert s.cancel("draft")["status"] == "cancelled"
    assert s.cancel("draft")["status"] == "cancelled"
    with pytest.raises(ValueError):
        s.accept("draft", 1, [LABELS])
    assert store.list_items("examples") == []


def test_acceptance_requires_every_label_and_text_rationale_if_supplied_and_rolls_back_on_sql_failure(setup):
    store, s, _, responses = setup
    start(s)
    responses.append({"job_id": "job", "status": "complete", "candidate": ITEM})
    draft = s.poll("draft")
    for labels in ({}, {"editor": {"label": "publish", "rationale": 23}, "scope": LABELS["scope"]}):
        with pytest.raises(ValueError):
            s.accept("draft", draft["version"], [labels])
    with store.connect() as db:
        db.execute("CREATE TRIGGER fail_label BEFORE INSERT ON item_feedback BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(Exception, match="fixture failure"):
        s.accept("draft", draft["version"], [LABELS])
    assert store.list_items("examples") == [] and s.get("draft")["status"] == "preview"


def test_retries_preserve_start_identity_and_conflicting_retries_fail(setup):
    _, s, requests, _ = setup
    first = start(s)
    assert start(s) == first and len(requests) == 1
    with pytest.raises(ValueError):
        s.start_transform("draft", "examples", refs(), "papyrus", {"url": "https://different.invalid"})


def test_transformer_cannot_supply_labels_or_incomplete_url_only_items(setup):
    _, s, _, responses = setup
    start(s)
    responses.append({"job_id": "job", "status": "complete", "candidate": {**ITEM, "labels": LABELS}})
    assert s.poll("draft")["status"] == "failed"
    responses.append(
        {
            "job_id": "job",
            "status": "complete",
            "candidate": {"id": "x", "occurred_at": "2026-10-14", "values": {"source_url": "https://example.invalid"}},
        }
    )
    assert s.poll("draft")["status"] == "invalid"


def test_changed_classifier_revision_invalidates_acceptance(setup):
    store, s, _, responses = setup
    start(s)
    responses.append({"job_id": "job", "status": "complete", "candidate": ITEM})
    draft = s.poll("draft")
    config = store.classifier("editor")["config"]
    store.save_classifier("editor", "editor", {**config, "question": "Changed?"})
    with pytest.raises(ValueError, match="revision"):
        s.accept("draft", draft["version"], [LABELS])
    assert store.list_items("examples") == []


def test_uncertain_feedback_retry_reuses_remote_idempotency_key(setup):
    _, s, requests, responses = setup
    start(s)
    responses.append({"job_id": "job", "status": "complete", "candidate": ITEM})
    draft = s.poll("draft")
    # Empty response queue simulates a failed transport after recording intent.
    failed = s.feedback("draft", draft["version"], "retry-key", "Fix extraction", {})
    assert failed["status"] == "failed"
    responses.append({"job_id": "job", "status": "running"})
    retried = s.feedback("draft", draft["version"], "retry-key", "Fix extraction", {})
    assert retried["status"] == "running" and retried["version"] == failed["version"]
    assert requests[-1].headers["Idempotency-Key"] == requests[-2].headers["Idempotency-Key"]


def test_cancellation_before_remote_start_response_preserves_pending_remote_cancel(setup):
    _, s, _, _ = setup

    def handle(request):
        s.cancel("draft")
        return httpx.Response(200, json={"job_id": "late-job", "status": "complete", "candidate": ITEM})

    s.client = httpx.Client(transport=httpx.MockTransport(handle))
    draft = start(s)
    assert draft["status"] == "cancelled" and draft["rows"] == []
    assert draft["job_id"] == "late-job" and draft["remote_cancel"] == "pending"


def test_file_credentials_are_rejected_before_a_raw_draft_is_persisted(setup):
    store, s, _, _ = setup
    s.redact = ("synthetic-provider-secret",)
    content = base64.b64encode(
        json.dumps([{**ITEM, "values": {"title": "Synthetic", "text": "synthetic-provider-secret"}, "labels": LABELS}]).encode()
    ).decode()
    with pytest.raises(ValueError, match="credential"):
        s.start_file("secret", "examples", refs(), "json", content)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM priming_drafts").fetchone()[0] == 0
