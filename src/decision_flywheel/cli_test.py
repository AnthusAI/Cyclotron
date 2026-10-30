"""The ``cyclotron`` command, with a fake lab and fake session (no network or credentials)."""

import json

import pytest

from .cli import CliError, discover, main

pytest.importorskip("cli_login.target")

LAB = "https://lab.example.test"
DISCOVERY = {
    "protocol": 1,
    "issuer": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_synthetic",
    "client_id": "console-client",
    "oauth_domain": "https://synthetic.auth.us-east-1.amazoncognito.com",
    "token_endpoint": "https://synthetic.auth.us-east-1.amazoncognito.com/oauth2/token",
    "api_endpoint": LAB,
    "remote_login_endpoint": "https://broker.lambda-url.us-east-1.on.aws",
    "remote_callback_uri": LAB + "/cli/callback",
}


def lab(discovery=None, session_answer=None):
    calls = []

    def get_json(url, headers=None):
        calls.append((url, headers))
        if url.endswith("/cli/discovery"):
            return 200, discovery if discovery is not None else DISCOVERY
        if url.endswith("/api/session"):
            return 200, session_answer or {
                "authenticated": True,
                "personId": "person-1",
                "csrfToken": "secret-csrf",
                "accounts": [],
                "selectedAccountId": None,
                "canWrite": False,
            }
        raise AssertionError(url)

    return get_json, calls


class FakeSession:
    def __init__(self, target):
        self.target = target

    def store_token_set(self, tokens):
        return {"subject": "person-1"}

    def headers(self, endpoint):
        assert endpoint == LAB
        return {"Authorization": "Bearer access-token"}

    def status(self):
        return {"signed_in": True, "subject": "person-1"}

    def logout(self):
        return {"local_session_removed": True, "remote_revocation": "unconfirmed"}


def test_discovery_becomes_a_target_bound_to_this_lab():
    get_json, _ = lab()
    target, enrollment = discover(LAB, get_json=get_json)
    assert enrollment is None
    assert target.api_endpoint == LAB and target.remote_callback_uri == LAB + "/cli/callback"
    assert target.namespace == "cyclotron-lab" and target.key == "lab.example.test"
    assert target.broker_endpoint == "https://broker.lambda-url.us-east-1.on.aws"


@pytest.mark.parametrize(
    "change",
    [
        {"api_endpoint": "https://elsewhere.example.test"},
        {"remote_callback_uri": "https://elsewhere.example.test/cli/callback"},
        {"protocol": 2},
    ],
)
def test_discovery_that_points_elsewhere_is_refused(change):
    get_json, _ = lab(DISCOVERY | change)
    with pytest.raises(CliError):
        discover(LAB, get_json=get_json)


def test_login_remote_signs_in_through_the_library_with_the_expected_person():
    get_json, _ = lab()
    seen, printed = {}, []

    def remote(target, store, **kwargs):
        seen.update(kwargs, target=target)
        store({"id_token": "x"})

    assert (
        main(
            ["login", "--remote", "--lab", LAB, "--as", "person@example.test", "--note", "nightly"],
            out=printed.append,
            get_json=get_json,
            session_factory=FakeSession,
            remote=remote,
        )
        == 0
    )
    assert seen["expected_email"] == "person@example.test" and seen["note"] == "nightly"
    assert seen["target"].api_endpoint == LAB


def test_whoami_asks_the_lab_with_the_bearer_token_and_never_prints_the_csrf_token():
    get_json, calls = lab()
    printed = []
    assert main(["whoami", "--lab", LAB], out=printed.append, get_json=get_json, session_factory=FakeSession) == 0
    assert calls[-1] == (LAB + "/api/session", {"Authorization": "Bearer access-token"})
    shown = json.loads(printed[0])
    assert shown["personId"] == "person-1" and "csrfToken" not in printed[0] and "access-token" not in printed[0]


def test_whoami_says_to_sign_in_when_the_lab_refuses_the_session():
    get_json, _ = lab(session_answer={"authenticated": False})
    printed = []
    assert main(["whoami", "--lab", LAB], out=printed.append, get_json=get_json, session_factory=FakeSession) == 1
    assert "cyclotron login --remote" in printed[0]


def test_logout_reports_an_unconfirmed_revocation():
    get_json, _ = lab()
    printed = []
    assert main(["logout", "--lab", LAB], out=printed.append, get_json=get_json, session_factory=FakeSession) == 0
    assert "could not be confirmed" in printed[-1]


def test_a_lab_that_is_not_https_is_refused():
    printed = []
    assert main(["status", "--lab", "http://lab.example.test"], out=printed.append) == 1
    assert "https origin" in printed[0]


def test_a_lab_that_enrolls_logins_gets_the_enroll_step():
    get_json, _ = lab(DISCOVERY | {"enrollment_endpoint": LAB + "/cli/enroll"})
    seen = {}

    def remote(target, store, **kwargs):
        seen.update(kwargs)

    assert (
        main(
            ["login", "--remote", "--lab", LAB],
            out=lambda _m: None,
            get_json=get_json,
            session_factory=FakeSession,
            remote=remote,
        )
        == 0
    )
    assert callable(seen["enroll"])


def test_the_enroll_step_sends_both_tokens_and_the_sealed_proof_and_refuses_on_failure():
    from cli_login.remote import RemoteLoginError

    from .cli import enroller

    calls = []

    def post_json(url, body, headers):
        calls.append((url, body, headers))
        return (200, {"ok": True, "personId": "p"}) if len(calls) == 1 else (401, {"error": "enrollment_refused"})

    enroll = enroller(LAB, LAB + "/cli/enroll", post_json=post_json)
    tokens = {"access_token": "access", "id_token": "identity"}
    enroll(tokens, "transaction-1", "sealed")
    assert calls[0] == (
        LAB + "/cli/enroll",
        {"transaction": "transaction-1", "enrollment": "sealed"},
        {"Authorization": "Bearer access", "X-Identity-Token": "identity"},
    )
    with pytest.raises(RemoteLoginError) as raised:
        enroll(tokens, "transaction-1", "sealed")
    assert raised.value.code == "enrollment_refused"
    with pytest.raises(RemoteLoginError):
        enroll(tokens, "transaction-1", None)
