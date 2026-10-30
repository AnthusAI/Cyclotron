"""The lab's remote CLI sign-in routes, against an in-process broker from cli-login (no AWS)."""

import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .account_sessions import OAuthConfig
from .account_sessions_test import identity  # noqa: F401 - the RSA token fixture
from .cli_login_routes import CliLoginConfig, install_cli_login_routes, lambda_invoker
from .prospectus_accounts import AccountAccessDenied

broker_handler = pytest.importorskip("cli_login.broker.handler")
transactions = pytest.importorskip("cli_login.broker.transactions")

ORIGIN = "https://lab.example.test"
AUTH = "https://synthetic.auth.us-east-1.amazoncognito.com"
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_synthetic"
BROKER_ARN = "arn:aws:lambda:us-east-1:123456789012:function:cli-login-broker"


@pytest.fixture
def lab():
    broker = broker_handler.Broker(
        transactions.MemoryTransactions(),
        broker_handler.BrokerConfig("console-client", (ORIGIN,), ORIGIN + "/cli/approve", ORIGIN + "/cli/callback"),
    )
    oauth = OAuthConfig(ISSUER, "console-client", AUTH, ORIGIN)
    app = FastAPI()
    install_cli_login_routes(app, oauth, CliLoginConfig(BROKER_ARN, "https://broker.lambda-url.us-east-1.on.aws"), broker.handle)
    return TestClient(app, base_url=ORIGIN), broker


def start(broker):
    _, answer = broker.start(
        {
            "protocol": 1,
            "client_id": "console-client",
            "target": ORIGIN,
            "code_challenge": "c" * 43,
            "code_challenge_method": "S256",
            "poll_secret_hash": broker_handler.sha256_hex("p"),
            "nonce": "n" * 16,
            "requester": {"host": "cloud-box", "version": "0.2.0", "note": "nightly agent"},
        },
        "198.51.100.1",
    )
    return answer["transaction"]


def test_discovery_names_this_lab_its_provider_and_its_broker(lab):
    client, _ = lab
    answer = client.get("/cli/discovery")
    assert answer.headers["cache-control"] == "no-store"
    assert answer.json() == {
        "protocol": 1,
        "issuer": ISSUER,
        "client_id": "console-client",
        "oauth_domain": AUTH,
        "token_endpoint": AUTH + "/oauth2/token",
        "api_endpoint": ORIGIN,
        "remote_login_endpoint": "https://broker.lambda-url.us-east-1.on.aws",
        "remote_callback_uri": ORIGIN + "/cli/callback",
    }


def test_approving_on_the_lab_sends_the_browser_to_google_and_the_callback_hands_the_code_to_the_broker(lab):
    client, broker = lab
    transaction = start(broker)
    page = client.get("/cli/approve", params={"t": transaction})
    assert page.status_code == 200 and "cloud-box" in page.text and "Cyclotron lab" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    csrf = client.cookies.get("__Secure-cli_login_csrf")
    approved = client.post(
        "/cli/approve", data={"t": transaction, "csrf": csrf, "action": "approve", "confirm": "yes"}, follow_redirects=False
    )
    assert approved.status_code == 303
    location = urlsplit(approved.headers["location"])
    query = {key: value[0] for key, value in parse_qs(location.query).items()}
    assert f"{location.scheme}://{location.netloc}{location.path}" == AUTH + "/oauth2/authorize"
    assert query["identity_provider"] == "Google" and query["redirect_uri"] == ORIGIN + "/cli/callback"
    assert len(approved.headers.get_list("set-cookie")) == 2  # the nonce cookie set, the CSRF cookie cleared
    done = client.get("/cli/callback", params={"state": query["state"], "code": "authorization-code"})
    assert done.status_code == 200 and "approved" in done.text
    assert broker.store.get(transaction)["code"] == "authorization-code"


def test_a_callback_without_the_approving_browsers_cookie_is_refused(lab):
    client, broker = lab
    transaction = start(broker)
    assert client.get("/cli/callback", params={"state": f"{transaction}.forged", "code": "c"}).status_code == 400
    assert broker.store.get(transaction)["state"] == "pending"


def test_remote_sign_in_is_off_unless_both_broker_settings_are_given():
    assert CliLoginConfig.from_environment({}) is None
    with pytest.raises(ValueError):
        CliLoginConfig.from_environment({"CYCLOTRON_CLI_BROKER_ARN": BROKER_ARN})
    with pytest.raises(ValueError):
        CliLoginConfig("arn:aws:lambda:us-east-1:123:function:x", "https://broker.example.test")


class FakeLambda:
    def __init__(self, answer, *, error=False):
        self.answer, self.error, self.calls = answer, error, []

    def invoke(self, **call):
        self.calls.append(call)
        result = {"Payload": io.BytesIO(json.dumps(self.answer).encode())}
        if self.error:
            result["FunctionError"] = "Unhandled"
        return result


def test_the_invoker_calls_the_broker_synchronously_and_reads_a_failure_as_unavailable():
    client = FakeLambda({"ok": True})
    assert lambda_invoker(client, BROKER_ARN)({"operation": "describe"}) == {"ok": True}
    assert client.calls[0]["FunctionName"] == BROKER_ARN and client.calls[0]["InvocationType"] == "RequestResponse"
    assert lambda_invoker(FakeLambda({"ok": True}, error=True), BROKER_ARN)({}) == {"ok": False, "error": "unavailable"}
    assert lambda_invoker(FakeLambda(["not", "a", "dict"]), BROKER_ARN)({}) == {"ok": False, "error": "unavailable"}


# --- enrollment of each CLI login ---------------------------------------------------------------------------------


class FakeIdentity:
    """Prospectus's identity service: a challenge, then one resolution per challenge."""

    def __init__(self):
        import time
        from uuid import uuid4

        from .google_identity import GoogleChallenge

        self.person_id = str(uuid4())
        self.challenge_value = GoogleChallenge(
            str(uuid4()),
            "central-nonce-" + "n" * 32,
            int(time.time()) + 300,
            "v1.synthetic.c3ludGhldGlj.bWFj",
        )
        self.resolved = []

    def challenge(self):
        return self.challenge_value

    def resolve(self, access_token, id_token, challenge_id, *, challenge_token=None):
        if self.resolved:
            raise AccountAccessDenied("challenge already used")
        self.resolved.append((challenge_id, challenge_token))
        return {"id": self.person_id, "createdAt": "2026-10-30T00:00:00Z"}


@pytest.fixture
def enrolling(identity):  # noqa: F811 - the imported RSA token fixture
    from types import SimpleNamespace

    from .account_sessions import OAuthSessions
    from .cli_login_routes import CliEnrollment
    from .s3_sessions_test import Cipher

    verifier, token = identity
    broker = broker_handler.Broker(
        transactions.MemoryTransactions(),
        broker_handler.BrokerConfig("console-client", (ORIGIN,), ORIGIN + "/cli/approve", ORIGIN + "/cli/callback"),
    )
    oauth = OAuthConfig(ISSUER, "console-client", AUTH, ORIGIN)
    gateway = SimpleNamespace(verifier=verifier, google_identity=FakeIdentity(), sessions=OAuthSessions(verifier))
    app = FastAPI()
    install_cli_login_routes(
        app,
        oauth,
        CliLoginConfig(BROKER_ARN, "https://broker.lambda-url.us-east-1.on.aws"),
        broker.handle,
        CliEnrollment(gateway, Cipher()),
    )
    return TestClient(app, base_url=ORIGIN), broker, gateway, token


def approve_with_enrollment(client, broker):
    transaction = start(broker)
    client.get("/cli/approve", params={"t": transaction})
    csrf = client.cookies.get("__Secure-cli_login_csrf")
    approved = client.post(
        "/cli/approve", data={"t": transaction, "csrf": csrf, "action": "approve", "confirm": "yes"}, follow_redirects=False
    )
    query = {key: value[0] for key, value in parse_qs(urlsplit(approved.headers["location"]).query).items()}
    client.get("/cli/callback", params={"state": query["state"], "code": "authorization-code"})
    return transaction, query, broker.store.get(transaction)


def test_the_approval_page_uses_the_central_challenge_nonce_and_seals_its_proof(enrolling):
    client, broker, gateway, _ = enrolling
    transaction, query, item = approve_with_enrollment(client, broker)
    assert query["nonce"] == gateway.google_identity.challenge_value.nonce
    assert item["oidc_nonce"] == query["nonce"] and item["enrollment"]
    challenge_id = gateway.google_identity.challenge_value.challenge_id
    assert "v1.synthetic" not in item["enrollment"] and challenge_id not in item["enrollment"]
    assert client.get("/cli/discovery").json()["enrollment_endpoint"] == ORIGIN + "/cli/enroll"


def tokens_for(token, nonce):
    import time

    now = int(time.time())
    return token(auth_time=now), token("id", nonce=nonce, auth_time=now)


def test_enrolling_a_cli_login_resolves_the_person_and_keeps_the_session(enrolling):
    client, broker, gateway, token = enrolling
    transaction, query, item = approve_with_enrollment(client, broker)
    access, id_token = tokens_for(token, query["nonce"])
    answer = client.post(
        "/cli/enroll",
        json={"transaction": transaction, "enrollment": item["enrollment"]},
        headers={"Authorization": "Bearer " + access, "X-Identity-Token": id_token},
    )
    assert answer.status_code == 200 and answer.json()["personId"] == gateway.google_identity.person_id
    assert gateway.google_identity.resolved == [
        (gateway.google_identity.challenge_value.challenge_id, "v1.synthetic.c3ludGhldGlj.bWFj")
    ]
    from .account_routes import bearer_identifier

    assert gateway.sessions.get(bearer_identifier(access)).person_id == gateway.google_identity.person_id


def test_a_sealed_proof_opens_only_for_its_own_transaction_and_its_own_tokens(enrolling):
    client, broker, gateway, token = enrolling
    transaction, query, item = approve_with_enrollment(client, broker)
    access, id_token = tokens_for(token, query["nonce"])
    headers = {"Authorization": "Bearer " + access, "X-Identity-Token": id_token}
    other = start(broker)
    assert (
        client.post("/cli/enroll", json={"transaction": other, "enrollment": item["enrollment"]}, headers=headers).status_code
        == 401
    )
    wrong_access, wrong_id = tokens_for(token, "another-nonce-0000000")
    assert (
        client.post(
            "/cli/enroll",
            json={"transaction": transaction, "enrollment": item["enrollment"]},
            headers={"Authorization": "Bearer " + wrong_access, "X-Identity-Token": wrong_id},
        ).status_code
        == 401
    )
    assert gateway.google_identity.resolved == []


@pytest.mark.parametrize("headers", [{}, {"X-Identity-Token": "x"}, {"Authorization": "Bearer a"}])
def test_enrolling_needs_both_tokens(enrolling, headers):
    client, broker, _, _ = enrolling
    transaction, _, item = approve_with_enrollment(client, broker)
    assert (
        client.post(
            "/cli/enroll", json={"transaction": transaction, "enrollment": item["enrollment"]}, headers=headers
        ).status_code
        == 400
    )
