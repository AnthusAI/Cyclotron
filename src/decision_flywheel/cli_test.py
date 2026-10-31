"""The ``cyclotron`` command, with a fake lab and fake session (no network or credentials)."""

import json
import uuid

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


NORTHWIND, CONTOSO = str(uuid.uuid4()), str(uuid.uuid4())
ACCOUNTS = [
    {
        "id": NORTHWIND,
        "name": "Northwind",
        "role": "owner",
        "entitlement": {
            "accountId": NORTHWIND,
            "product": "cyclotron",
            "status": "active",
            "capabilities": ["console:read", "console:write"],
        },
    },
    {
        "id": CONTOSO,
        "name": "Contoso",
        "role": "viewer",
        "entitlement": {
            "accountId": CONTOSO,
            "product": "cyclotron",
            "status": "active",
            "capabilities": ["console:read"],
        },
    },
]


class MemorySelections:
    def __init__(self, saved=None):
        self.saved = dict(saved or {})

    def get(self, lab, subject):
        record = self.saved.get(lab)
        return record["accountId"] if record and record["subject"] == subject else None

    def set(self, lab, subject, account_id):
        self.saved[lab] = {"subject": subject, "accountId": account_id}

    def clear(self, lab):
        self.saved.pop(lab, None)


def account_lab(selected=None, refuse=()):
    """A lab whose bearer sessions start with no account, as each renewed access token does."""
    get_json, calls = lab(
        session_answer={
            "authenticated": True,
            "personId": "person-1",
            "accounts": ACCOUNTS,
            "selectedAccountId": selected,
            "canWrite": False,
        }
    )
    posts = []

    def post_json(url, body, headers):
        posts.append((url, body, headers))
        assert url == LAB + "/api/accounts/select" and headers == {"Authorization": "Bearer access-token"}
        if body["accountId"] in refuse:
            return 403, {"detail": "account access refused"}
        return 200, {
            "authenticated": True,
            "personId": "person-1",
            "accounts": ACCOUNTS,
            "selectedAccountId": body["accountId"],
            "canWrite": body["accountId"] == NORTHWIND,
        }

    return get_json, post_json, calls, posts


def run(argv, get_json, post_json, selections):
    printed = []
    code = main(
        argv + ["--lab", LAB],
        out=printed.append,
        get_json=get_json,
        post_json=post_json,
        session_factory=FakeSession,
        selections=selections,
    )
    return code, printed


def test_accounts_lists_each_account_with_its_role_and_marks_the_chosen_one():
    get_json, post_json, _, _ = account_lab()
    code, printed = run(["accounts"], get_json, post_json, MemorySelections({LAB: {"subject": "person-1", "accountId": CONTOSO}}))
    assert code == 0
    shown = json.loads(printed[0])
    assert [(row["name"], row["role"], row["selected"]) for row in shown] == [
        ("Northwind", "owner", False),
        ("Contoso", "viewer", True),
    ]
    assert shown[0]["capabilities"] == ["console:read", "console:write"]


def test_use_chooses_an_account_by_name_after_the_lab_confirms_access():
    get_json, post_json, _, posts = account_lab()
    selections = MemorySelections()
    code, printed = run(["use", "northwind"], get_json, post_json, selections)
    assert code == 0 and posts[0][1] == {"accountId": NORTHWIND}
    assert selections.get(LAB, "person-1") == NORTHWIND
    assert "Northwind" in printed[-1]


def test_use_accepts_an_account_id():
    get_json, post_json, _, _ = account_lab()
    selections = MemorySelections()
    assert run(["use", CONTOSO], get_json, post_json, selections)[0] == 0
    assert selections.get(LAB, "person-1") == CONTOSO


def test_use_refuses_an_unknown_account_and_names_the_choices():
    get_json, post_json, _, posts = account_lab()
    selections = MemorySelections()
    code, printed = run(["use", "Fabrikam"], get_json, post_json, selections)
    assert code == 1 and not posts and selections.saved == {}
    assert "Northwind" in printed[0] and "Contoso" in printed[0]


def test_use_keeps_nothing_when_the_lab_refuses_the_account():
    get_json, post_json, _, _ = account_lab(refuse=(CONTOSO,))
    selections = MemorySelections()
    code, printed = run(["use", "Contoso"], get_json, post_json, selections)
    assert code == 1 and selections.saved == {}
    assert "access" in printed[0]


def test_use_clear_forgets_the_choice():
    get_json, post_json, _, posts = account_lab()
    selections = MemorySelections({LAB: {"subject": "person-1", "accountId": NORTHWIND}})
    assert run(["use", "--clear"], get_json, post_json, selections)[0] == 0
    assert selections.saved == {} and not posts


def test_whoami_reselects_the_chosen_account_for_a_renewed_token_so_the_lab_rechecks_it():
    get_json, post_json, _, posts = account_lab(selected=None)
    code, printed = run(["whoami"], get_json, post_json, MemorySelections({LAB: {"subject": "person-1", "accountId": NORTHWIND}}))
    assert code == 0 and posts[0][1] == {"accountId": NORTHWIND}
    shown = json.loads(printed[0])
    assert shown["selectedAccountId"] == NORTHWIND and shown["canWrite"] is True


def test_whoami_does_not_reselect_when_the_lab_already_has_the_account():
    get_json, post_json, _, posts = account_lab(selected=NORTHWIND)
    assert run(["whoami"], get_json, post_json, MemorySelections({LAB: {"subject": "person-1", "accountId": NORTHWIND}}))[0] == 0
    assert not posts


def test_whoami_drops_a_choice_whose_access_has_ended_and_says_so():
    get_json, post_json, _, _ = account_lab(refuse=(CONTOSO,))
    selections = MemorySelections({LAB: {"subject": "person-1", "accountId": CONTOSO}})
    code, printed = run(["whoami"], get_json, post_json, selections)
    assert code == 1 and selections.saved == {}
    assert "cyclotron use" in printed[-1]


def test_a_choice_made_by_another_person_is_ignored():
    get_json, post_json, _, posts = account_lab()
    selections = MemorySelections({LAB: {"subject": "someone-else", "accountId": NORTHWIND}})
    code, printed = run(["whoami"], get_json, post_json, selections)
    assert code == 0 and not posts and json.loads(printed[0])["selectedAccountId"] is None


def test_choices_are_kept_per_lab():
    selections = MemorySelections()
    selections.set(LAB, "person-1", NORTHWIND)
    selections.set("https://other.example.test", "person-1", CONTOSO)
    assert selections.get(LAB, "person-1") == NORTHWIND


def test_file_selections_keep_one_private_file_per_machine(tmp_path):
    from .cli import FileSelections

    selections = FileSelections(tmp_path / "state")
    selections.set(LAB, "person-1", NORTHWIND)
    selections.set("https://other.example.test", "person-1", CONTOSO)
    again = FileSelections(tmp_path / "state")
    assert again.get(LAB, "person-1") == NORTHWIND and again.get(LAB, "someone-else") is None
    again.clear(LAB)
    assert FileSelections(tmp_path / "state").get(LAB, "person-1") is None
    assert FileSelections(tmp_path / "state").get("https://other.example.test", "person-1") == CONTOSO
    path = next((tmp_path / "state").iterdir())
    assert path.stat().st_mode & 0o077 == 0
