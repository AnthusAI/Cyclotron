"""Same authenticated GraphQL application boundary for embedded and standalone hosts."""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from .web_api import create_app
from .web_store import WebStore


@pytest.mark.parametrize(
    "vote",
    [
        {"label": "publish"},
        {"label": "publish", "rationale": None},
        {"label": "publish", "rationale": ""},
        {"label": "publish", "rationale": "   "},
        {"label": "publish", "rationale": "Synthetic reason"},
    ],
)
def test_file_import_api_requires_human_acceptance_and_supports_cancel_and_retry(tmp_path, vote):
    store = WebStore(tmp_path / "db")
    store.save_item_list("examples", "Examples")
    store.save_classifier(
        "editor", "Editorial", {"question": "Publish?", "classes": [{"label": "publish"}, {"label": "not publish"}]}
    )
    client = TestClient(create_app(store))
    refs = [{"id": "editor", "revision": 1}]
    query = 'query($refs:JSON!){primingContract(listId:"examples",classifiers:$refs) primingTemplate(listId:"examples",classifiers:$refs,format:"csv")}'
    result = client.post("/graphql", json={"query": query, "variables": {"refs": refs}}).json()
    assert "errors" not in result and "editor.rationale" in result["data"]["primingTemplate"]["content"]
    row = {"id": "synthetic", "occurred_at": "2026-10-14", "values": {"text": "Synthetic content"}, "labels": {"editor": vote}}

    def command(action, payload):
        return client.post(
            "/graphql",
            json={
                "query": "mutation($action:String!,$payload:JSON!){primingCommand(action:$action,payload:$payload)}",
                "variables": {"action": action, "payload": payload},
            },
        ).json()

    payload = {
        "identifier": "draft",
        "list_id": "examples",
        "refs": refs,
        "kind": "json",
        "content": base64.b64encode(json.dumps([row]).encode()).decode(),
    }
    draft = command("file", payload)["data"]["primingCommand"]
    assert draft["status"] == "preview"
    assert store.list_items("examples") == []
    acceptance = {"identifier": "draft", "version": draft["version"], "labels": None}
    assert command("accept", acceptance)["data"]["primingCommand"]["status"] == "accepted"
    assert command("accept", acceptance)["data"]["primingCommand"]["status"] == "accepted"
    assert len(store.list_items("examples")) == 1
    assert store.item_labels("examples", "synthetic", 1)[0]["comment"] == (vote.get("rationale") or "")
    cancelled = command("file", {**payload, "identifier": "cancel"})["data"]["primingCommand"]
    assert command("cancel", {"identifier": "cancel"})["data"]["primingCommand"]["status"] == "cancelled"
    assert "errors" in command("accept", {**acceptance, "identifier": "cancel", "version": cancelled["version"]})


def test_priming_api_preserves_auth_origin_and_credential_controls(tmp_path):
    store = WebStore(tmp_path / "db")
    client = TestClient(create_app(store, token="test-secret", redact=("provider-secret",)))
    payload = {"query": '{primingDraft(identifier:"missing")}'}
    assert client.post("/graphql", json=payload).status_code == 401
    headers = {"authorization": "Bearer test-secret"}
    assert client.post("/graphql", json=payload, headers={**headers, "origin": "https://evil.invalid"}).status_code == 403
    mutation = {"query": 'mutation{primingCommand(action:"file",payload:{identifier:"provider-secret"})}'}
    response = client.post("/graphql", json=mutation, headers=headers).json()
    assert "credential" in response["errors"][0]["message"]
    assert "provider-secret" not in json.dumps(response)


def test_hosted_priming_cannot_fall_back_to_a_global_catalog_or_transformer(tmp_path, monkeypatch):
    from .priming import PrimingService
    from .prospectus_accounts_test import hosted_client

    client, stores, operations = hosted_client(tmp_path)
    client.cookies.set("synthetic-session", "member")
    client.cookies.set("selected-account", "a")
    before = {name: store.export_storage() for name, store in stores.items()}

    def forbidden(*args, **kwargs):
        raise AssertionError("hosted priming constructed a global service")

    monkeypatch.setattr(PrimingService, "__init__", forbidden)
    result = client.post(
        "/graphql", json={"query": 'mutation{primingCommand(action:"file",payload:{identifier:"draft"})}'}
    ).json()
    assert "account-scoped service composition" in result["errors"][0]["message"]
    assert operations[-1] == "console:write"
    assert {name: store.export_storage() for name, store in stores.items()} == before


def test_hosted_app_rejects_a_global_priming_service(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="global priming service"):
        create_app(WebStore(tmp_path / "db"), account_access=object(), priming=object())
