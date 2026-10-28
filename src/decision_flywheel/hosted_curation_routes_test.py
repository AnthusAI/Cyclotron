"""Bearer curation routes over the actual hosted app, queue worker and S3 fake; no network or credentials."""

import asyncio
import base64
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from . import hosted_publications_test as hosted
from .hosted_publications import targets_from_environment
from .prospectus_accounts import ProspectusUnavailable
from .publication_lab import PublicationBinding, PublicationError
from .site_credentials import VerifiedSiteCredential

identity = hosted.identity
ROUTES = "/api/newsroom/curation"


class Authority:
    """Stands in for the account service: tokens map to a person and a role on one account."""

    def __init__(self, account, person):
        self.account, self.person = account, person
        self.roles = {"editor-token": "member", "viewer-token": "viewer", "foreign-token": "owner"}
        self.calls, self.unavailable, self.revoked, self.revoke_after = [], False, set(), None

    def __call__(self, token, account, operation):
        self.calls.append((token, account, operation))
        if self.revoke_after is not None and len(self.calls) > self.revoke_after:
            self.revoked.add(token)
        if self.unavailable:
            raise ProspectusUnavailable("down")
        role = self.roles.get(token)
        if role is None:
            raise PublicationError("bearer credential refused", 401)
        if token in self.revoked or account != self.account and token != "foreign-token":
            raise PublicationError("curation access refused", 403)
        if operation == "console:write" and role == "viewer":
            raise PublicationError("curation access refused", 403)
        if token == "foreign-token":
            # A person who is real but belongs to a different account than the one pinned.
            return VerifiedSiteCredential(str(uuid4()), "foreign-person", role)
        return VerifiedSiteCredential(account, self.person, role)


class RecordingModel(hosted.OfflineModel):
    def __init__(self):
        super().__init__()
        self.training = []

    async def classify_many(self, configs, target, training, **kwargs):
        self.training.append(training)
        return await super().classify_many(configs, target, training, **kwargs)


def build(identity, monkeypatch, *, lists=("priming-alpha", "priming-beta")):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = hosted.setup(identity, monkeypatch)
    recording = RecordingModel()
    for host in (bff, worker):
        host.model_factory = lambda config: (recording, hosted.DisabledOptimizer())
    target = targets[0]
    bindings = tuple(
        replace(binding, priming_list_id=name) if binding.publication_id in ("alpha", "beta") else binding
        for binding, name in zip(target.bindings, ("priming-alpha", "priming-beta", None, None))
    )
    targets[0] = replace(target, bindings=bindings, model_identity=recording.model_identity)
    for name in lists:
        store.save_item_list(name, "Synthetic human priming")
    authority = Authority(account, session.person_id)
    bff.curation_authority = worker.curation_authority = authority
    session.account_id = account
    bff.sessions.save(session)
    return SimpleHost(bff, worker, recording, store, config, account, cookie, session, sent, targets, app, authority)


class SimpleHost:
    def __init__(self, bff, worker, model, store, config, account, cookie, session, sent, targets, app, authority):
        self.bff, self.worker, self.model, self.store, self.config = bff, worker, model, store, config
        self.account, self.cookie, self.session, self.sent, self.targets = account, cookie, session, sent, targets
        self.app, self.authority = app, authority

    def client(self, token="editor-token", publication="alpha", binding="binding-one", account=None, **extra):
        headers = {"X-Cyclotron-Account": account or self.account, "X-Papyrus-Publication": publication}
        if binding is not None:
            headers["X-Papyrus-Binding"] = binding
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://lab.example.test", headers={**headers, **extra}
        )

    def cookie_client(self):
        return hosted.priming_client(self.app, self.config, self.cookie, self.session, self.account)


GRAPHQL_COMMAND = "mutation($action:String!,$payload:JSON!){primingCommand(action:$action,payload:$payload)}"
GRAPHQL_CONTRACT = "query($list:ID!,$refs:JSON!){primingContract(listId:$list,classifiers:$refs)}"
GRAPHQL_DRAFT = "query($id:String!){primingDraft(identifier:$id)}"
REFS = [{"id": "alpha", "revision": 1}]


def rows(item_id="synthetic-human-example", output="alpha", label="include"):
    row = {
        "id": item_id,
        "occurred_at": "2026-01-01",
        "values": {"text": "Synthetic human-approved context"},
        "labels": {output: {"label": label, "rationale": "Synthetic human explanation"}},
    }
    return base64.b64encode(json.dumps([row]).encode()).decode()


async def command(http, action, payload):
    return await http.post(
        f"{ROUTES}/priming", json={"query": GRAPHQL_COMMAND, "variables": {"action": action, "payload": payload}}
    )


async def import_file(http, identifier="draft-one", list_id="priming-alpha", refs=REFS, content=None, **kw):
    return await command(
        http,
        "file",
        {"identifier": identifier, "list_id": list_id, "refs": refs, "kind": "json", "content": content or rows(**kw)},
    )


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]["primingCommand"]


def run(coroutine):
    return asyncio.run(coroutine)


# ---- authentication and capability matrix ----


@pytest.mark.parametrize("route", ["context", "publication", "priming"])
def test_a_missing_or_unrecognized_bearer_is_unauthorized_before_any_storage_read(identity, monkeypatch, route):
    host = build(identity, monkeypatch)
    reads = len(host.bff.s3.calls)

    async def scenario():
        for token, code in ((None, "bearer_required"), ("unknown-token", "bearer_invalid")):
            async with host.client(token=token) as http:
                if route == "context":
                    response = await http.get(f"{ROUTES}/context")
                else:
                    body = {"operation": "status", "payload": {}} if route == "publication" else {"query": GRAPHQL_DRAFT}
                    response = await http.post(f"{ROUTES}/{route}", json=body)
                assert response.status_code == 401 and response.json()["code"] == code, response.text

    run(scenario())
    assert len(host.bff.s3.calls) == reads


def test_the_authorized_account_must_equal_the_pinned_account_and_a_wrong_publication_leaks_nothing(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client(token="foreign-token") as http:
            mismatch = await http.get(f"{ROUTES}/context")
        async with host.client(publication="no-such-publication") as http:
            unknown = await http.get(f"{ROUTES}/context")
        async with host.client(publication="gamma") as http:
            unconfigured_for_priming = await http.get(f"{ROUTES}/context")
        assert mismatch.status_code == unknown.status_code == 403
        assert mismatch.json() == unknown.json() == {"error": mismatch.json()["error"], "code": "access_refused"}
        assert unconfigured_for_priming.status_code == 200 and unconfigured_for_priming.json()["priming"] is None

    run(scenario())


def test_a_revoked_membership_is_forbidden_and_an_unavailable_account_service_is_503(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            assert (await http.get(f"{ROUTES}/context")).status_code == 200
            host.authority.revoked.add("editor-token")
            revoked = await http.get(f"{ROUTES}/context")
            assert revoked.status_code == 403 and revoked.json()["code"] == "access_refused"
            host.authority.revoked.clear()
            host.authority.unavailable = True
            for response in (
                await http.get(f"{ROUTES}/context"),
                await http.post(f"{ROUTES}/publication", json={"operation": "status", "payload": {}}),
                await http.post(f"{ROUTES}/priming", json={"query": GRAPHQL_DRAFT, "variables": {"id": "x"}}),
            ):
                assert response.status_code == 503 and response.json()["code"] == "authority_unavailable"

    run(scenario())


def test_without_verifier_configuration_every_curation_route_fails_closed_with_503_and_reads_nothing(identity, monkeypatch):
    host = build(identity, monkeypatch)
    for runtime in (host.bff, host.worker):
        runtime.curation_authority = None  # no injected authority and no verifier role or accounts URL configured
    assert host.bff.config.lab_verifier_role_arn == "" and host.bff.config.accounts_api_url == ""
    reads = len(host.bff.s3.calls)

    async def scenario():
        async with host.client() as http:
            for response in (
                await http.get(f"{ROUTES}/context"),
                await http.post(f"{ROUTES}/publication", json={"operation": "status", "payload": {}}),
                await http.post(f"{ROUTES}/priming", json={"query": GRAPHQL_DRAFT, "variables": {"id": "x"}}),
            ):
                assert response.status_code == 503 and response.json()["code"] == "authority_unavailable"

    run(scenario())
    assert len(host.bff.s3.calls) == reads


def test_curation_routes_authorize_through_the_assumed_verifier_role_end_to_end(identity, monkeypatch):
    from .lab_verifier import lab_verifier_authority
    from .lab_verifier_test import ROLE, URL, Clock, FakeSTS

    host = build(identity, monkeypatch)
    seen = []

    def central(request):
        seen.append(request)
        token = request.headers["x-prospectus-human-token"]
        if token != "editor-token":
            return httpx.Response(401, json={"error": "invalid_human"})
        if request.method == "GET":
            return httpx.Response(200, json={"person": {"id": host.session.person_id}})
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "allowed": True,
                "accountId": request.url.path.split("/")[4],
                "personId": host.session.person_id,
                "product": "cyclotron",
                "role": "member",
                "operation": body["operation"],
                "revision": 1,
            },
        )

    clock = Clock()
    sts = FakeSTS(clock)
    authority = lab_verifier_authority(
        ROLE, URL, "us-east-1", sts=sts, client=httpx.Client(transport=httpx.MockTransport(central)), now=clock
    )
    host.bff.curation_authority = host.worker.curation_authority = authority

    async def scenario():
        async with host.client() as http:
            assert (await http.get(f"{ROUTES}/context")).status_code == 200
        async with host.client(token="unknown-token") as http:
            response = await http.get(f"{ROUTES}/context")
            assert response.status_code == 401 and response.json()["code"] == "bearer_invalid"

    run(scenario())
    assert len(sts.calls) == 1
    assert all(r.headers["x-amz-security-token"] == "fake-session-token-1" for r in seen)
    assert [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in seen[:2]] == [("GET", "me"), ("POST", "authorize")]


def test_every_success_echoes_the_scope_pins_and_is_not_cacheable(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            for response in (
                await http.get(f"{ROUTES}/context"),
                await http.post(
                    f"{ROUTES}/priming", json={"query": GRAPHQL_CONTRACT, "variables": {"list": "priming-alpha", "refs": REFS}}
                ),
            ):
                assert response.status_code == 200, response.text
                assert response.headers["x-cyclotron-account"] == host.account
                assert response.headers["x-papyrus-publication"] == "alpha"
                assert response.headers["x-papyrus-binding"] == "binding-one"
                assert response.headers["cache-control"] == "no-store"

    run(scenario())


def test_pins_are_required_and_a_changed_binding_is_a_conflict(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client(binding=None) as http:
            missing = await http.post(f"{ROUTES}/publication", json={"operation": "status", "payload": {}})
            assert missing.status_code == 400 and missing.json()["code"] == "pins_required"
            assert (await http.get(f"{ROUTES}/context")).status_code == 200
        async with host.client(account="not-a-uuid") as http:
            assert (await http.get(f"{ROUTES}/context")).status_code == 400
        async with host.client(binding="stale") as http:
            stale = await http.post(f"{ROUTES}/publication", json={"operation": "status", "payload": {}})
            assert stale.status_code == 409 and stale.json()["code"] == "account_changed"

    run(scenario())


def test_a_curation_request_body_over_four_megabytes_is_read_whole_and_answered(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            body = {"query": GRAPHQL_DRAFT, "variables": {"id": "no-such-draft", "pad": "x" * 5_000_000}}
            response = await http.post(f"{ROUTES}/priming", json=body)
            # The whole body was read, authorized and executed: the draft lookup itself answers.
            assert response.status_code == 409 and response.json()["error"] == "unknown import draft", response.text
            not_json = await http.post(f"{ROUTES}/publication", content="{}", headers={"Content-Type": "text/plain"})
            assert not_json.status_code == 415

    run(scenario())


def test_capabilities_follow_the_role_and_viewers_cannot_mutate(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client(token="viewer-token") as http:
            context = (await http.get(f"{ROUTES}/context")).json()
            assert context["capabilities"] == ["read"] and context["priming"] is not None
            refused = await import_file(http)
            assert refused.status_code == 403 and refused.json()["code"] == "access_refused"
            query = await http.post(
                f"{ROUTES}/priming", json={"query": GRAPHQL_CONTRACT, "variables": {"list": "priming-alpha", "refs": REFS}}
            )
            assert query.status_code == 200
            decide = await http.post(
                f"{ROUTES}/publication",
                json={"operation": "decide", "payload": {"bindingRevision": "binding-one", "item": hosted.ITEM}},
            )
            assert decide.status_code == 403
        async with host.client(token="editor-token") as http:
            context = (await http.get(f"{ROUTES}/context")).json()
            assert context["capabilities"] == ["read", "review"]

    run(scenario())
    assert not host.store.draft("pub-anything")


def test_operations_other_than_status_and_decide_are_refused_with_a_stable_code(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            for operation in ("review", "steer", "history", "events", "replay"):
                response = await http.post(f"{ROUTES}/publication", json={"operation": operation, "payload": {}})
                assert response.status_code == 409 and response.json()["code"] == "operation_unavailable", operation
            unknown = await http.post(f"{ROUTES}/publication", json={"operation": "drop-everything", "payload": {}})
            assert unknown.status_code == 400
            extra = await http.post(f"{ROUTES}/publication", json={"operation": "status", "payload": {}, "other": 1})
            assert extra.status_code == 400
        async with host.client(token="viewer-token") as http:
            # Unavailable operations never ask for a write role.
            await http.post(f"{ROUTES}/publication", json={"operation": "steer", "payload": {}})
        assert not any(op == "console:write" for token, _, op in host.authority.calls if token == "viewer-token")

    run(scenario())


def test_work_is_withheld_when_authority_is_lost_while_it_ran(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            # The first authorization admits the request; the post-work check finds the membership revoked.
            host.authority.revoke_after = len(host.authority.calls) + 1
            response = await http.post(
                f"{ROUTES}/priming", json={"query": GRAPHQL_CONTRACT, "variables": {"list": "priming-alpha", "refs": REFS}}
            )
            assert response.status_code == 403 and "data" not in response.json()
            assert [operation for _, _, operation in host.authority.calls[-2:]] == ["console:read", "console:read"]

    run(scenario())


# ---- context ----


def test_context_lists_the_configured_priming_contract_and_no_transformers(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            context = (await http.get(f"{ROUTES}/context")).json()
        assert context == {
            "scope": {"accountId": host.account, "publicationId": "alpha", "bindingRevision": "binding-one"},
            "mode": "remote",
            "priming": {"listId": "priming-alpha", "classifiers": REFS, "transformers": []},
            "capabilities": ["read", "review"],
            "bindingRef": None,
        }
        async with host.client(token="editor-token", publication="gamma") as http:
            plain = (await http.get(f"{ROUTES}/context")).json()
        assert plain["priming"] is None and plain["capabilities"] == ["read"]

    run(scenario())


def environment_target(binding):
    account, run_id = str(uuid4()), str(uuid4())
    return json.dumps([{"accountId": account, "runId": run_id, "bindings": [binding], "modelIdentity": "reviewed-model"}])


def test_priming_list_configuration_is_parsed_strictly_and_optionally():
    base = {"publicationId": "papyrus", "outputs": ["alpha"], "revision": "one"}
    parsed = targets_from_environment(
        {"CYCLOTRON_PUBLICATION_TARGETS": environment_target({**base, "priming": {"listId": "accepted"}})}
    )
    assert parsed[0].bindings[0].priming_list_id == "accepted"
    long_list = "accepted-" + "l" * 1_000
    parsed = targets_from_environment(
        {"CYCLOTRON_PUBLICATION_TARGETS": environment_target({**base, "priming": {"listId": long_list}})}
    )
    assert parsed[0].bindings[0].priming_list_id == long_list
    parsed = targets_from_environment({"CYCLOTRON_PUBLICATION_TARGETS": environment_target(base)})
    assert parsed[0].bindings[0].priming_list_id is None
    for invalid in (
        {"priming": {}},
        {"priming": {"listId": ""}},
        {"priming": {"listId": 3}},
        {"priming": {"listId": "ok", "extra": 1}},
        {"priming": "accepted"},
        {"priming": None},
        {"surprise": True},
    ):
        with pytest.raises(ValueError, match="invalid"):
            targets_from_environment({"CYCLOTRON_PUBLICATION_TARGETS": environment_target({**base, **invalid})})
    with pytest.raises(ValueError):
        PublicationBinding("papyrus", ("alpha",), "one", "")


# ---- publication ----


def test_status_follows_the_existing_hosted_publication_rules(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            body = {"operation": "status", "payload": {}}
            none_yet = await http.post(f"{ROUTES}/publication", json=body)
            assert none_yet.status_code == 503 and none_yet.json()["code"] == "status_unavailable"
            async with host.cookie_client() as console:
                decided = await console.post(
                    "/publications/alpha/operations",
                    json={
                        "contractVersion": "cyclotron-publication/v1",
                        "operation": "decide",
                        "payload": {"bindingRevision": "binding-one", "item": hosted.ITEM},
                    },
                )
                assert decided.status_code == 202, decided.text
                await host.worker.worker().process(host.sent.pop())
            status = await http.post(f"{ROUTES}/publication", json=body)
            assert status.status_code == 200, status.text
            value = status.json()
            assert value["contractVersion"] == "cyclotron-publication/v1"
            assert (value["accountId"], value["publicationId"], value["bindingRevision"]) == (
                host.account,
                "alpha",
                "binding-one",
            )
            assert status.headers["x-papyrus-publication"] == "alpha"
            assert (await http.post(f"{ROUTES}/publication", json={**body, "payload": {"x": 1}})).status_code == 400

    run(scenario())


def test_decide_keeps_the_live_disabled_refusal_and_is_not_activated_for_bearer_callers(identity, monkeypatch):
    host = build(identity, monkeypatch)
    decide = {"operation": "decide", "payload": {"bindingRevision": "binding-one", "item": hosted.ITEM}}

    async def scenario():
        async with host.client() as http:
            host.bff.config = replace(host.config, allow_live=False)
            disabled = await http.post(f"{ROUTES}/publication", json=decide)
            assert disabled.status_code == 503 and disabled.json()["code"] == "live_disabled"
            host.bff.config = host.config
            changed = await http.post(
                f"{ROUTES}/publication", json={**decide, "payload": {**decide["payload"], "bindingRevision": "stale"}}
            )
            assert changed.status_code == 409 and changed.json()["code"] == "binding_changed"
            refused = await http.post(f"{ROUTES}/publication", json=decide)
            assert refused.status_code == 409 and refused.json()["code"] == "operation_unavailable"
            assert not host.sent and not host.model.calls

    run(scenario())


# ---- priming ----


def test_a_file_import_previews_under_the_callers_identity_and_stores_a_publication_namespace(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            draft = data(await import_file(http))
            assert draft["id"] == "draft-one" and draft["status"] == "preview" and draft["version"] == 1
            assert draft["contract"]["transformers"] == []
            polled = data(await command(http, "poll", {"identifier": "draft-one"}))
            assert polled["id"] == "draft-one"
            read = await http.post(f"{ROUTES}/priming", json={"query": GRAPHQL_DRAFT, "variables": {"id": "draft-one"}})
            assert read.json()["data"]["primingDraft"]["id"] == "draft-one"
        stored = [row for row in host.store.export_storage()["tables"]["priming_drafts"]]
        assert len(stored) == 1 and stored[0]["id"].startswith("pub-") and stored[0]["id"].endswith(":draft-one")

    run(scenario())


def test_another_publications_list_classifiers_and_draft_ids_are_refused_before_any_storage_read(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            first = data(await import_file(http))
            assert first["status"] == "preview"
            reads = len(host.bff.s3.calls)
            beta_refs = [{"id": "beta", "revision": 1}]
            attempts = [
                await import_file(http, identifier="other", list_id="priming-beta"),
                await import_file(http, identifier="other", refs=beta_refs),
                await import_file(http, identifier="other", refs=[*REFS, *beta_refs]),
                await import_file(http, identifier="pub-" + "0" * 32 + ":draft-one"),
                await command(http, "poll", {"identifier": "pub-" + "0" * 32 + ":draft-one"}),
                await command(http, "cancel", {"identifier": "pub-" + "0" * 32 + ":draft-one"}),
                await command(http, "accept", {"identifier": "pub-" + "0" * 32 + ":draft-one", "version": 1, "labels": None}),
                await http.post(
                    f"{ROUTES}/priming", json={"query": GRAPHQL_CONTRACT, "variables": {"list": "priming-beta", "refs": REFS}}
                ),
                await http.post(
                    f"{ROUTES}/priming",
                    json={"query": GRAPHQL_CONTRACT, "variables": {"list": "priming-alpha", "refs": beta_refs}},
                ),
                await http.post(
                    f"{ROUTES}/priming", json={"query": GRAPHQL_DRAFT, "variables": {"id": "pub-" + "1" * 32 + ":x"}}
                ),
            ]
            assert [r.status_code for r in attempts] == [403] * len(attempts), [r.text for r in attempts]
            assert {r.json()["code"] for r in attempts} == {"outside_publication"}
            assert len(host.bff.s3.calls) == reads, "a refusal opened storage"
            # A stale classifier revision needs the run configuration, but still no draft or catalog read.
            stale = await import_file(http, identifier="other", refs=[{"id": "alpha", "revision": 2}])
            assert stale.status_code == 403 and stale.json()["code"] == "outside_publication"
            assert len(host.store.export_storage()["tables"]["priming_drafts"]) == 1

    run(scenario())


def test_publications_cannot_see_each_others_drafts_even_with_the_same_draft_name(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client(publication="alpha") as alpha, host.client(publication="beta") as beta:
            data(await import_file(alpha, identifier="shared-name"))
            missing = await command(beta, "poll", {"identifier": "shared-name"})
            assert missing.status_code == 409 and "unknown import draft" in missing.json()["error"]
            beta_draft = data(
                await import_file(
                    beta, identifier="shared-name", list_id="priming-beta", refs=[{"id": "beta", "revision": 1}], output="beta"
                )
            )
            assert beta_draft["contract"]["list_id"] == "priming-beta"
            assert data(await command(alpha, "poll", {"identifier": "shared-name"}))["contract"]["list_id"] == "priming-alpha"
        assert len(host.store.export_storage()["tables"]["priming_drafts"]) == 2

    run(scenario())


def test_slice_one_has_no_hosted_transformer(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            reads = len(host.bff.s3.calls)
            response = await command(
                http,
                "transform",
                {
                    "identifier": "t",
                    "list_id": "priming-alpha",
                    "refs": REFS,
                    "transformer_id": "papyrus",
                    "inputs": {"url": "https://x.test"},
                },
            )
            assert response.status_code == 400 and response.json()["code"] == "no_hosted_transformer"
            assert "transformer" in response.json()["error"]
            assert len(host.bff.s3.calls) == reads

    run(scenario())


def test_an_import_over_one_megabyte_is_previewed_whole(identity, monkeypatch):
    host = build(identity, monkeypatch)
    row = {
        "id": "long-example",
        "occurred_at": "2026-01-01",
        "values": {"text": "x" * 1_100_000},
        "labels": {"alpha": {"label": "include", "rationale": "Synthetic human explanation"}},
    }
    content = base64.b64encode(json.dumps([row]).encode()).decode()
    assert len(content) > 1_000_000

    async def scenario():
        async with host.client() as http:
            assert data(await import_file(http, content=content))["status"] == "preview"

    run(scenario())


def test_an_import_beyond_the_catalog_journal_fails_cleanly_with_no_partial_commit(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            assert host.store.export_storage()["tables"]["priming_drafts"] == []
            # Beyond what the account's catalog journal can hold.
            current = len(json.dumps(host.store.export_storage()))
            host.bff.config = replace(host.config, max_bytes=current + 4096)
            filler = base64.b64encode(
                json.dumps(
                    [
                        {
                            "id": "big",
                            "occurred_at": "2026-01-01",
                            "values": {"text": "x" * 20000},
                            "labels": {"alpha": {"label": "include", "rationale": "r"}},
                        }
                    ]
                ).encode()
            ).decode()
            full = await import_file(http, content=filler)
            assert full.status_code == 413 and full.json()["code"] == "storage_limit", full.text
            host.bff.config = host.config
            assert host.store.export_storage()["tables"]["priming_drafts"] == []
            assert data(await import_file(http, identifier="after"))["status"] == "preview"

    run(scenario())


def test_accepting_applies_the_examples_so_the_next_decision_carries_them_and_is_idempotent(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            draft = data(await import_file(http))
            accepted = data(
                await command(http, "accept", {"identifier": "draft-one", "version": draft["version"], "labels": None})
            )
            assert accepted["status"] == "accepted" and accepted["id"] == "draft-one"
            assert accepted["autoPrime"]["added"] == {"alpha": ["synthetic-human-example"]}
            assert accepted["autoPrime"]["primedExamples"] == {"alpha": 1}
            assert not host.sent and not host.model.calls, "applying examples must not queue work or call a provider"
            again = data(await command(http, "accept", {"identifier": "draft-one", "version": draft["version"], "labels": None}))
            assert again["status"] == "accepted" and again["autoPrime"]["added"] == {}
            assert again["autoPrime"]["primedExamples"] == {"alpha": 1}
            assert len(host.store.list_items("priming-alpha")) == 1
            different = await command(http, "accept", {"identifier": "draft-one", "version": draft["version"], "labels": [{}]})
            assert different.status_code == 409
        # The next decision request for the same publication uses the accepted example.
        async with host.cookie_client() as console:
            decided = await console.post(
                "/publications/alpha/operations",
                json={
                    "contractVersion": "cyclotron-publication/v1",
                    "operation": "decide",
                    "payload": {"bindingRevision": "binding-one", "item": hosted.ITEM},
                },
            )
            assert decided.status_code == 202, decided.text
            await host.worker.worker().process(host.sent.pop())
            status = await console.post(
                "/publications/alpha/operations",
                json={"contractVersion": "cyclotron-publication/v1", "operation": "status", "payload": {}},
            )
            assert status.json()["classifiers"]["alpha"]["cyclotron"]["primedExamples"] == 1
        training = host.model.training[-1]
        carried = _ids(training)
        assert "synthetic-human-example" in carried
        # The imported example is never the item being decided.
        assert hosted.ITEM["id"] not in carried

    run(scenario())


def _ids(training):
    """Every example id the model request carried, whatever the container shape."""
    found = set()

    def walk(value):
        if isinstance(value, str):
            return
        if hasattr(value, "item") and hasattr(value.item, "id"):
            found.add(value.item.id)
        if isinstance(value, dict):
            for part in value.values():
                walk(part)
        elif isinstance(value, (list, tuple, set)):
            for part in value:
                walk(part)
        elif hasattr(value, "__dict__"):
            for part in vars(value).values():
                walk(part)

    walk(training)
    return found


def test_acceptance_is_refused_up_front_without_a_reviewed_model_identity(identity, monkeypatch):
    host = build(identity, monkeypatch)
    host.targets[0] = replace(host.targets[0], model_identity=None)

    async def scenario():
        async with host.client() as http:
            assert (await http.get(f"{ROUTES}/context")).json()["priming"] is None
            refused = await http.post(
                f"{ROUTES}/priming", json={"query": GRAPHQL_CONTRACT, "variables": {"list": "priming-alpha", "refs": REFS}}
            )
            assert refused.status_code == 503 and refused.json()["code"] == "priming_not_configured"

    run(scenario())


def test_an_example_that_is_also_an_operational_item_is_refused_before_anything_is_accepted(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            draft = data(await import_file(http, item_id=hosted.ITEM["id"]))
            # The operational run already contains this identity, so the firewall refuses to prime it.
            refused = await command(http, "accept", {"identifier": "draft-one", "version": draft["version"], "labels": None})
            assert refused.status_code == 409 and refused.json()["code"] == "firewall_overlap"
            assert data(await command(http, "poll", {"identifier": "draft-one"}))["status"] == "preview"
            assert host.store.list_items("priming-alpha") == []

    run(scenario())


def test_the_priming_surface_is_a_closed_schema_without_introspection(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            for query in (
                "{__schema{types{name}}}",
                "{ runs { id } }",
                'mutation{ createRun(name:"x") {id} }',
                "subscription { x }",
                "{",
            ):
                response = await http.post(f"{ROUTES}/priming", json={"query": query})
                assert response.status_code == 400 and response.json()["code"] == "invalid_request", query
            two = await http.post(
                f"{ROUTES}/priming", json={"query": 'query A{primingDraft(identifier:"a")} query B{primingDraft(identifier:"b")}'}
            )
            assert two.status_code == 400
            extra = await http.post(
                f"{ROUTES}/priming", json={"query": GRAPHQL_DRAFT, "variables": {"id": "x"}, "operationName": "x"}
            )
            assert extra.status_code == 400

    run(scenario())


def test_a_failed_auto_prime_leaves_the_draft_accepted_and_the_same_accept_completes_it(identity, monkeypatch):
    from . import hosted_curation_routes as routes

    host = build(identity, monkeypatch)
    real = routes.apply_priming
    attempts = []

    async def flaky(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("synthetic storage failure")
        return await real(request)

    monkeypatch.setattr(routes, "apply_priming", flaky)

    async def scenario():
        async with host.client() as http:
            draft = data(await import_file(http))
            body = {"identifier": "draft-one", "version": draft["version"], "labels": None}
            failed = await command(http, "accept", body)
            assert failed.status_code == 409 and failed.json()["code"] == "auto_prime_failed"
            assert "synthetic" not in failed.text
            assert data(await command(http, "poll", {"identifier": "draft-one"}))["status"] == "accepted"
            healed = data(await command(http, "accept", body))
            assert healed["autoPrime"]["added"] == {"alpha": ["synthetic-human-example"]}
            assert len(host.store.list_items("priming-alpha")) == 1

    run(scenario())


def test_a_mutation_whose_authority_is_lost_mid_request_is_not_reported_as_done(identity, monkeypatch):
    host = build(identity, monkeypatch)

    async def scenario():
        async with host.client() as http:
            host.authority.revoke_after = len(host.authority.calls) + 1
            response = await import_file(http)
            assert response.status_code == 403 and "data" not in response.json()
            assert [operation for _, _, operation in host.authority.calls[-2:]] == ["console:write", "console:write"]

    run(scenario())
