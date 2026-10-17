"""One live-authority flag governs the held-out service, the console capabilities and the hosted workspace."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from .account_sessions_test import CLIENT, ISSUER
from .embedded_cyclotron import Cyclotron
from .held_out_service import HeldOutService
from .held_out_test import RubricReadingModel, decide_and_review, learning_definition
from .s3_journal_test import ConditionalS3
from .s3_sessions_test import Cipher
from .web_api import create_app
from .web_store import WebStore

CAPABILITIES_QUERY = '{capabilities{liveEnabled}}'
COMMAND_MUTATION = 'mutation($action:String!,$payload:JSON!){heldOutCommand(action:$action,payload:$payload)}'
SESSION_KEY_ARN = "arn:aws:kms:us-east-1:111111111111:key/11111111-1111-4111-8111-111111111111"


def seeded_opener(tmp_path, model, requested_budgets):
    with Cyclotron.open(tmp_path / "c", learning_definition(), model, max_requests=500) as cyclotron:
        decide_and_review(cyclotron, 4, audit_only=True)

    def open_authorized_cyclotron(cyclotron_id, max_requests=None):
        requested_budgets.append(max_requests)
        return Cyclotron.open(tmp_path / "c", learning_definition(), model, max_requests=max_requests or 1)
    return open_authorized_cyclotron


def graphql(client, query, **variables):
    return client.post("/graphql", json={"query": query, "variables": variables}).json()


def hosted_runtime(allow_live, open_cyclotron):
    from .hosted_runtime import HostedConfig, HostedRuntime
    config = HostedConfig(issuer=ISSUER, client_id=CLIENT, authorization_origin="https://synthetic.auth.example.invalid",
        public_origin="https://console.example.test", account="111111111111", region="us-east-1",
        books_bucket="synthetic-books", auth_bucket="synthetic-auth", session_key_arn=SESSION_KEY_ARN,
        queue_url="https://sqs.us-east-1.amazonaws.com/111111111111/synthetic",
        central_endpoint="https://central.example.test/api", allow_live=allow_live)
    return HostedRuntime(config, s3=ConditionalS3(), cipher=Cipher(), central=lambda *args, **kwargs: None,
        verifier=SimpleNamespace(), send=lambda message: None,
        model_factory=(lambda model_config: None) if allow_live else None, open_cyclotron=open_cyclotron)


def test_evaluate_through_graphql_is_refused_with_live_disabled_and_capabilities_agree(tmp_path):
    model = RubricReadingModel()
    held_out = HeldOutService(seeded_opener(tmp_path, model, []), allow_live=False)
    client = TestClient(create_app(WebStore(tmp_path / "db"), held_out=held_out))
    assert graphql(client, CAPABILITIES_QUERY)["data"]["capabilities"]["liveEnabled"] is False
    graphql(client, COMMAND_MUTATION, action="freeze", payload={"cyclotronId": "papyrus-relevance"})
    calls_before = model.calls
    refused = graphql(client, COMMAND_MUTATION, action="evaluate",
                      payload={"cyclotronId": "papyrus-relevance", "confirmedRequestBound": 2})
    assert "live authority" in refused["errors"][0]["message"] and model.calls == calls_before


def test_a_live_held_out_service_makes_the_console_report_live_enabled(tmp_path):
    held_out = HeldOutService(seeded_opener(tmp_path, RubricReadingModel(), []), allow_live=True)
    client = TestClient(create_app(WebStore(tmp_path / "db"), held_out=held_out))
    assert graphql(client, CAPABILITIES_QUERY)["data"]["capabilities"]["liveEnabled"] is True


def test_services_that_disagree_about_live_authority_are_refused_at_construction(tmp_path):
    held_out = HeldOutService(seeded_opener(tmp_path, RubricReadingModel(), []), allow_live=True)
    with pytest.raises(ValueError, match="must agree"):
        create_app(WebStore(tmp_path / "db"), service=SimpleNamespace(allow_live=False), held_out=held_out)


def test_evaluating_sizes_the_request_budget_from_the_confirmed_bound(tmp_path):
    requested_budgets = []
    held_out = HeldOutService(seeded_opener(tmp_path, RubricReadingModel(), requested_budgets), allow_live=True)
    client = TestClient(create_app(WebStore(tmp_path / "db"), held_out=held_out))
    frozen = graphql(client, COMMAND_MUTATION, action="freeze",
                     payload={"cyclotronId": "papyrus-relevance"})["data"]["heldOutCommand"]
    bound = 2 * frozen["items"]
    evaluated = graphql(client, COMMAND_MUTATION, action="evaluate",
                        payload={"cyclotronId": "papyrus-relevance", "confirmedRequestBound": bound})
    assert "errors" not in evaluated and requested_budgets[-1] == bound


@pytest.mark.parametrize("allow_live", [False, True])
def test_a_hosted_workspace_exposes_the_held_out_service_with_the_single_live_flag(allow_live):
    opened = []
    runtime = hosted_runtime(allow_live, lambda account_id, cyclotron_id, max_requests: opened.append(
        (account_id, cyclotron_id, max_requests)))
    account_id = str(uuid4())
    workspace = runtime.workspace(account_id)
    assert isinstance(workspace.held_out, HeldOutService)
    assert workspace.held_out.allow_live is allow_live and workspace.service.allow_live is allow_live
    workspace.held_out.open_cyclotron("papyrus-relevance", 6)
    assert opened == [(account_id, "papyrus-relevance", 6)]


def test_a_hosted_workspace_without_an_opener_declares_no_held_out_service():
    assert hosted_runtime(False, None).workspace(str(uuid4())).held_out is None
