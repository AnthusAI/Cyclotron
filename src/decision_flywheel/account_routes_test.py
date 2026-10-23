"""Exercise real OAuth/BFF routes with RSA tokens and fake central transport."""

from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from .account_routes import HostedAccounts
from .account_sessions import OAuthConfig
from .account_sessions_test import CLIENT, ISSUER, identity
from .prospectus_accounts import AccountAccessDenied, AccountsClient, AccountWorkspace, ProspectusUnavailable
from .web_api import create_app
from .web_store import WebStore


@pytest.fixture(params=["memory", "s3"])
def hosted(tmp_path, identity, request):
    verifier, token = identity
    state = {"person": None, "accounts": [], "calls": [], "exchanges": [], "nonce": None}
    account_id = str(uuid4())
    person_id = str(uuid4())
    store = WebStore(tmp_path / "account.sqlite")
    store.create_run("Private account run", "recorded", {})
    state["stores"] = {account_id: store}

    def central(method, path, access_token, body=None, key=None):
        verifier.access(access_token)
        state["calls"].append((method, path, body, key))
        if method == "GET":
            return {"person": state["person"], "accounts": state["accounts"]}
        if path == "/v1/identities/resolve":
            state["person"] = {"id": person_id}
            return {"person": state["person"]}
        if path == "/v1/accounts":
            state["person"] = {"id": person_id}
            account = {
                "id": account_id,
                "name": body["name"],
                "role": "owner",
                "entitlement": {
                    "accountId": account_id,
                    "product": "cyclotron",
                    "status": "active",
                    "capabilities": ["console:read", "console:write"],
                },
            }
            state["accounts"] = [account]
            return {"account": account}
        selected = next((account for account in state["accounts"] if path == f"/v1/accounts/{account['id']}/authorize"), None)
        if not selected:
            raise AccountAccessDenied("account access refused")
        return dict(
            allowed=True,
            accountId=selected["id"],
            personId=person_id,
            product="cyclotron",
            role=selected["role"],
            operation=body["operation"],
            revision=1,
        )

    def exchange(request):
        state["exchanges"].append(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "access_token": token(**state.get("access_changes", {})),
                "id_token": token("id", **({"nonce": state["nonce"]} | state.get("id_changes", {}))),
            },
        )

    config = OAuthConfig(ISSUER, CLIENT, "https://synthetic.auth.us-east-1.amazoncognito.com", "https://console.example.test")
    gateway = HostedAccounts(
        config,
        AccountsClient(central),
        lambda identifier: AccountWorkspace(state["stores"][identifier]),
        verifier=verifier,
        token_client=httpx.AsyncClient(transport=httpx.MockTransport(exchange)),
    )
    if request.param == "s3":
        from .s3_journal_test import ConditionalS3, journal
        from .s3_sessions import S3OAuthSessions
        from .s3_sessions_test import Cipher

        s3 = ConditionalS3()
        state["s3"] = s3
        gateway.sessions = S3OAuthSessions(
            verifier, journal_for=lambda kind, ref: journal(s3, name=kind + "-" + ref), cipher=Cipher()
        )
    app = create_app(None, account_access=gateway.access)
    gateway.install(app)
    return TestClient(app, base_url=config.public_origin), gateway, state, account_id


def sign_in(client, state):
    login = client.get("/auth/login", follow_redirects=False)
    query = parse_qs(urlsplit(login.headers["location"]).query)
    state["nonce"] = query["nonce"][0]
    assert query["identity_provider"] == ["Google"]
    assert query["code_challenge_method"] == ["S256"]
    result = client.get("/auth/callback", params={"state": query["state"][0], "code": "synthetic-code"}, follow_redirects=False)
    assert result.status_code == 303
    return query, result


def test_login_callback_verifies_pkce_nonce_and_sets_only_an_opaque_secure_cookie(hosted):
    client, gateway, state, _ = hosted
    with client:
        query, callback = sign_in(client, state)
        cookies = callback.headers.get_list("set-cookie")
        assert any(
            "__Host-cyclotron-session=" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
            for cookie in cookies
        )
        assert "access_token" not in str(cookies)
        assert state["exchanges"][0]["redirect_uri"] == ["https://console.example.test/auth/callback"]
        assert state["exchanges"][0]["client_id"] == [CLIENT]
        assert len(state["exchanges"][0]["code_verifier"][0]) >= 43
        assert client.get("/auth/callback", params={"state": query["state"][0], "code": "again"}).status_code == 400
        assert client.get("/api/session").json()["authenticated"] is True
        assert client.get("/api/session").json()["personId"] is None
        assert all(call[0] == "GET" for call in state["calls"])  # sign-in never creates a central person/account


def test_unknown_user_creates_and_selects_a_central_account_without_browser_tokens(hosted):
    client, _, state, account_id = hosted
    with client:
        assert client.get("/api/session").json() == {"authenticated": False, "loginUrl": "/auth/login"}
        sign_in(client, state)
        session = client.get("/api/session").json()
        key = str(uuid4())
        headers = {"Origin": str(client.base_url).rstrip("/"), "X-CSRF-Token": session["csrfToken"], "Idempotency-Key": key}
        response = client.post("/api/accounts", json={"name": "My team"}, headers=headers)
        assert response.status_code == 201
        result = client.get("/api/session").json()
        assert result["selectedAccountId"] == account_id
        assert result["accounts"][0]["name"] == "My team"
        assert result["canWrite"] is True
        assert "access_token" not in str(result) and "synthetic-code" not in str(result)
        creation = [call for call in state["calls"] if call[1] == "/v1/accounts" and call[0] == "POST"]
        assert creation == [("POST", "/v1/accounts", {"name": "My team"}, key)]
        assert client.post("/graphql", json={"query": "{runs{name}}"}).status_code == 401
        assert (
            client.post("/graphql", params={"account": account_id}, json={"query": "{runs{name}}"}).json()["data"]["runs"][0][
                "name"
            ]
            == "Private account run"
        )


def test_account_changes_require_exact_origin_and_csrf_and_foreign_accounts_are_denied(hosted):
    client, _, state, _ = hosted
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        for headers in (
            {},
            {"Origin": "https://evil.example.test", "X-CSRF-Token": session["csrfToken"]},
            {"Origin": "https://console.example.test", "X-CSRF-Token": "forged"},
        ):
            assert client.post("/api/accounts", json={"name": "Forbidden"}, headers=headers).status_code == 403
        headers = {"Origin": "https://console.example.test", "X-CSRF-Token": session["csrfToken"]}
        assert client.post("/api/accounts/select", json={"accountId": str(uuid4())}, headers=headers).status_code == 403
        assert not any(call[0] == "POST" and call[1] == "/v1/accounts" for call in state["calls"])


def test_logout_revokes_server_session_and_an_old_cookie_cannot_reopen_it(hosted):
    client, gateway, state, _ = hosted
    with client:
        sign_in(client, state)
        cookie = client.cookies.get(gateway.config.cookie_name)
        session = client.get("/api/session").json()
        headers = {"Origin": "https://console.example.test", "X-CSRF-Token": session["csrfToken"]}
        assert client.post("/auth/logout", headers=headers).status_code == 200
        client.cookies.set(gateway.config.cookie_name, cookie)
        assert client.get("/api/session").json()["authenticated"] is False


def test_callback_rejects_a_different_browser_before_token_exchange(hosted):
    client, _, state, _ = hosted
    with client:
        login = client.get("/auth/login", follow_redirects=False)
        query = parse_qs(urlsplit(login.headers["location"]).query)
        client.cookies.clear()
        assert client.get("/auth/callback", params={"state": query["state"][0], "code": "stolen"}).status_code == 400
        assert state["exchanges"] == []


def test_callback_rejects_a_signed_id_token_with_the_wrong_nonce(hosted):
    client, gateway, state, _ = hosted
    with client:
        login = client.get("/auth/login", follow_redirects=False)
        query = parse_qs(urlsplit(login.headers["location"]).query)
        state["nonce"] = "does-not-match-the-login"
        response = client.get("/auth/callback", params={"state": query["state"][0], "code": "synthetic"})
        assert response.status_code == 400
        assert not client.cookies.get(gateway.config.cookie_name)
        assert client.get("/api/session").json()["authenticated"] is False


def test_viewer_state_and_direct_mutations_remain_read_only(hosted):
    client, _, state, account_id = hosted
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        headers = {
            "Origin": "https://console.example.test",
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Viewer fixture"}, headers=headers).status_code == 201
        state["accounts"][0]["role"] = "viewer"
        assert client.get("/api/session").json()["canWrite"] is False
        mutation = 'mutation {createRun(name:"Forbidden",mode:"recorded",config:{}){id}}'
        assert client.post("/graphql", params={"account": account_id}, json={"query": mutation}).json().get("errors")
        assert (
            len(client.post("/graphql", params={"account": account_id}, json={"query": "{runs{id}}"}).json()["data"]["runs"]) == 1
        )


def test_another_tabs_selection_cannot_redirect_an_older_workspaces_writes(hosted, tmp_path):
    client, _, state, alpha_id = hosted
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        headers = {
            "Origin": "https://console.example.test",
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Alpha"}, headers=headers).status_code == 201
        beta_id = str(uuid4())
        beta = WebStore(tmp_path / "beta.sqlite")
        beta.create_run("Beta only", "recorded", {})
        state["stores"][beta_id] = beta
        state["accounts"].append(
            {
                "id": beta_id,
                "name": "Beta",
                "role": "owner",
                "entitlement": {
                    "accountId": beta_id,
                    "product": "cyclotron",
                    "status": "active",
                    "capabilities": ["console:read", "console:write"],
                },
            }
        )
        assert client.post("/api/accounts/select", json={"accountId": beta_id}, headers=headers).status_code == 200
        mutation = 'mutation {createRun(name:"Alpha delayed write",mode:"recorded",config:{}){id}}'
        assert client.post("/graphql", params={"account": alpha_id}, json={"query": mutation}).json()["data"]["createRun"]["id"]
        assert [run["name"] for run in state["stores"][alpha_id].runs()] == ["Alpha delayed write", "Private account run"]
        assert [run["name"] for run in beta.runs()] == ["Beta only"]


def google_mode(gateway, state, *, signed=False):
    import time

    from .google_identity import GoogleIdentityClient

    challenge_id = str(uuid4())
    person = {"id": str(uuid4()), "createdAt": "2026-10-14T00:00:00Z"}
    calls = []

    def transport(method, path, access=None, body=None, *, identity_token=None):
        calls.append((method, path, access, body, identity_token))
        if path.endswith("/challenge"):
            return {
                "challengeId": challenge_id,
                "nonce": "central-nonce-" + "n" * 32,
                "expiresAt": int(time.time()) + 300,
                **({"challengeToken": "v1.synthetic.c3ludGhldGlj.bWFj"} if signed else {}),
            }
        if path.endswith("/resolve"):
            if signed:
                assert body == {"challengeToken": "v1.synthetic.c3ludGhldGlj.bWFj"}
            state["person"] = person
        return {"person": person}

    gateway.google_identity = GoogleIdentityClient(transport)
    return calls, person, challenge_id


def test_signed_central_challenge_completes_existing_callback_without_exposing_opaque_evidence(hosted):
    client, gateway, state, _ = hosted
    calls, person, _ = google_mode(gateway, state, signed=True)
    with client:
        query, callback = sign_in(client, state)
        assert callback.status_code == 303
        assert calls[1][3] == {"challengeToken": "v1.synthetic.c3ludGhldGlj.bWFj"}
        assert "v1.synthetic.c3ludGhldGlj.bWFj" not in str(query) + str(callback.headers) + callback.text
        session = gateway.sessions.get(client.cookies.get(gateway.config.cookie_name))
        assert session.person_id == person["id"] and not hasattr(session, "challenge_token")


def test_google_challenge_precedes_redirect_and_callback_forwards_both_proofs_only_server_side(hosted):
    client, gateway, state, _ = hosted
    calls, person, challenge_id = google_mode(gateway, state)
    with client:
        query, callback = sign_in(client, state)
        assert query["nonce"] == ["central-nonce-" + "n" * 32]
        assert calls[0][:2] == ("POST", "/v1/identities/google/challenge")
        assert calls[0][2] is None
        assert calls[1][1] == "/v1/identities/google/resolve"
        assert calls[1][3] == {"challengeId": challenge_id}
        assert gateway.verifier.access(calls[1][2])["sub"] == "provider-subject"
        assert gateway.verifier._verify(calls[1][4], "id")["nonce"] == query["nonce"][0]
        result = client.get("/api/session")
        assert result.json()["personId"] == person["id"]
        assert not any(call[1] == "/v1/identities/me" for call in calls)
        assert calls[1][2] not in result.text + str(callback.headers)
        assert calls[1][4] not in result.text + str(callback.headers)
        session = gateway.sessions.get(client.cookies.get(gateway.config.cookie_name))
        assert not hasattr(session, "id_token")  # drop ID evidence after successful enrollment


@pytest.mark.parametrize(
    "changes",
    [dict(aud="wrong-client"), dict(iss="https://wrong.example.test"), dict(origin_jti="other-session"), dict(auth_time=1)],
)
def test_google_callback_rejects_bad_independent_id_evidence_before_central_enrollment(hosted, changes):
    client, gateway, state, _ = hosted
    calls, _, _ = google_mode(gateway, state)
    state["id_changes"] = changes
    with client:
        login = client.get("/auth/login", follow_redirects=False)
        query = parse_qs(urlsplit(login.headers["location"]).query)
        state["nonce"] = query["nonce"][0]
        assert client.get("/auth/callback", params={"state": query["state"][0], "code": "synthetic"}).status_code == 400
        assert len(calls) == 1
        assert not client.cookies.get(gateway.config.cookie_name)


def test_account_listing_checks_person_without_me_and_workspace_still_checks_me(hosted):
    client, gateway, state, _ = hosted
    calls, person, _ = google_mode(gateway, state)
    with client:
        sign_in(client, state)
        gateway.google_identity.me = lambda _: {"id": str(uuid4()), "createdAt": person["createdAt"]}
        assert client.get("/api/session").json()["authenticated"] is True
        assert client.get("/api/runs/unknown/events").status_code == 401
        assert client.get("/api/session").json()["authenticated"] is False
        assert sum(call[1].endswith("/resolve") for call in calls) == 1


def test_separate_accounts_person_mismatch_blocks_creation_before_side_effects(hosted):
    client, gateway, state, _ = hosted
    google_mode(gateway, state)
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        state["person"] = {"id": str(uuid4())}
        headers = {
            "Origin": "https://console.example.test",
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Must not create"}, headers=headers).status_code == 403
        assert not any(call[0] == "POST" and call[1] == "/v1/accounts" for call in state["calls"])


def test_identity_outage_never_falls_back_to_existing_local_session_or_workspace(hosted):
    client, gateway, state, account_id = hosted
    google_mode(gateway, state)

    def unavailable(*args):
        raise ProspectusUnavailable("identity service unavailable")

    with client:
        sign_in(client, state)
        gateway.google_identity.me = unavailable
        assert client.get("/api/session").status_code == 200
        response = client.post("/graphql", params={"account": account_id}, json={"query": "{runs{name}}"})
        assert response.status_code == 503
        assert "Private account run" not in response.text
        assert response.headers["Cache-Control"] == "no-store"


def test_challenge_outage_does_not_redirect_or_create_local_pending_login(hosted):
    client, gateway, state, _ = hosted
    google_mode(gateway, state)

    def unavailable():
        raise ProspectusUnavailable("identity service unavailable")

    gateway.google_identity.challenge = unavailable
    with client:
        result = client.get("/auth/login", follow_redirects=False)
        assert result.status_code == 503 and "location" not in result.headers
        assert not state["s3"].objects if "s3" in state else not gateway.sessions.pending and not gateway.sessions.sessions


@pytest.mark.parametrize("changes", [dict(client_id="wrong"), dict(iss="https://wrong.example.test")])
def test_google_callback_rejects_wrong_access_authority_before_identity_resolution(hosted, changes):
    client, gateway, state, _ = hosted
    calls, _, _ = google_mode(gateway, state)
    state["access_changes"] = changes
    with client:
        login = client.get("/auth/login", follow_redirects=False)
        query = parse_qs(urlsplit(login.headers["location"]).query)
        state["nonce"] = query["nonce"][0]
        assert client.get("/auth/callback", params={"state": query["state"][0], "code": "synthetic"}).status_code == 400
        assert len(calls) == 1
        assert not any("/session-" in key for key in state["s3"].objects) if "s3" in state else not gateway.sessions.sessions


def test_http_event_pages_pin_account_bound_cursor_and_reauthorize_every_request(hosted):
    client, gateway, state, account_id = hosted
    with client:
        assert client.get("/api/runs/unknown/events", params={"account": account_id}).status_code == 401
        sign_in(client, state)
        session = client.get("/api/session").json()
        headers = {
            "Origin": "https://console.example.test",
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Events"}, headers=headers).status_code == 201
        store = state["stores"][account_id]
        run = store.runs()[0]
        first = store.append_event(run["id"], "one", {"kind": "synthetic", "text": "exact 雪"})
        second = store.append_event(run["id"], "two", {"kind": "synthetic"})
        url = f"/api/runs/{run['id']}/events"
        result = client.get(url, params={"account": account_id, "after": first["sequence"], "limit": 1})
        assert result.status_code == 200 and "no-store" in result.headers["cache-control"]
        assert result.json() == {
            "events": [{"sequence": second["sequence"], "sourceId": "two", "payload": {"kind": "synthetic"}}]
        }
        assert client.get(url, params={"account": str(uuid4())}).status_code == 403
        state["accounts"] = []
        assert client.get(url, params={"account": account_id}).status_code == 403
        assert client.get(url, params={"account": account_id, "after": -1}).status_code == 422


def test_http_event_page_outage_returns_no_private_events_and_enforces_response_budget(hosted):
    client, gateway, state, account_id = hosted
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        headers = {
            "Origin": "https://console.example.test",
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Events"}, headers=headers).status_code == 201
        store = state["stores"][account_id]
        run = store.runs()[0]
        store.append_event(run["id"], "large", {"kind": "synthetic", "text": "x" * 1048576})
        url = f"/api/runs/{run['id']}/events"
        assert client.get(url, params={"account": account_id}).status_code == 413

        def outage(*args, **kwargs):
            raise ProspectusUnavailable("synthetic unavailable")

        gateway.accounts.authorize = outage
        result = client.get(url, params={"account": account_id})
        assert result.status_code == 503 and "events" not in result.json()


def test_selected_session_lists_and_checks_both_permissions_concurrently(hosted):
    from threading import Barrier

    client, gateway, state, account_id = hosted
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        headers = {
            "Origin": str(client.base_url).rstrip("/"),
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Parallel account"}, headers=headers).status_code == 201
        barrier = Barrier(3, timeout=2)
        listing, authorize = gateway.accounts.accounts, gateway.accounts.authorize

        def list_together(token):
            barrier.wait()
            return listing(token)

        def authorize_together(*args):
            barrier.wait()
            return authorize(*args)

        gateway.accounts.accounts = list_together
        gateway.accounts.authorize = authorize_together
        result = client.get("/api/session")
        assert result.status_code == 200
        assert result.json()["selectedAccountId"] == account_id
        assert result.json()["canWrite"] is True


def test_select_returns_fresh_session_state_without_requiring_another_listing_request(hosted):
    client, _, state, account_id = hosted
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        headers = {
            "Origin": str(client.base_url).rstrip("/"),
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Selected account"}, headers=headers).status_code == 201
        result = client.post("/api/accounts/select", json={"accountId": account_id}, headers=headers)
        assert result.status_code == 200
        assert result.json()["authenticated"] is True
        assert result.json()["selectedAccountId"] == account_id
        assert result.json()["canWrite"] is True
        assert result.json()["accounts"][0]["id"] == account_id


@pytest.mark.parametrize("person_response", [None, {"id": "different-enrolled-person"}])
def test_fresh_listing_null_or_changed_person_revokes_the_original_session(hosted, person_response):
    client, gateway, state, _ = hosted
    google_mode(gateway, state)
    with client:
        sign_in(client, state)
        cookie = client.cookies.get(gateway.config.cookie_name)
        state["person"] = person_response
        result = client.get("/api/session")
        assert result.status_code == 200 and result.json()["authenticated"] is False
        assert gateway.sessions.get(cookie) is None


def test_listing_outage_never_reuses_a_previous_success_or_calls_me_as_fallback(hosted):
    client, gateway, state, _ = hosted
    calls, _, _ = google_mode(gateway, state)
    with client:
        sign_in(client, state)
        assert client.get("/api/session").json()["authenticated"] is True

        def unavailable(*_):
            raise ProspectusUnavailable("listing unavailable")

        gateway.accounts.accounts = unavailable
        assert client.get("/api/session").status_code == 503
        assert not any(call[1] == "/v1/identities/me" for call in calls)


@pytest.mark.parametrize(
    "changes",
    [
        dict(auth_time=float("inf")),
        dict(auth_time=True),
        dict(auth_time=9999999999),
        dict(origin_jti=""),
        dict(origin_jti=None),
        dict(origin_jti="x" * 257),
    ],
)
def test_listing_optimization_rejects_malformed_or_future_session_binding_locally(hosted, identity, changes):
    client, gateway, state, _ = hosted
    _, person, _ = google_mode(gateway, state)
    verifier, token = identity
    with client:
        encoded = token(**changes)
        claims = verifier.access(encoded)
        cookie, session = gateway.sessions.create(encoded, claims)
        session.person_id = person["id"]
        gateway.sessions.save(session)
        client.cookies.set(gateway.config.cookie_name, cookie)
        result = client.get("/api/session")
        assert result.status_code == 200 and result.json()["authenticated"] is False
        assert gateway.sessions.get(cookie) is None
        assert not state["calls"]


def test_select_revokes_changed_person_before_persisting_any_requested_account(hosted):
    client, gateway, state, _ = hosted
    google_mode(gateway, state)
    with client:
        sign_in(client, state)
        cookie = client.cookies.get(gateway.config.cookie_name)
        session = client.get("/api/session").json()
        state["person"] = None
        headers = {"Origin": "https://console.example.test", "X-CSRF-Token": session["csrfToken"]}
        response = client.post("/api/accounts/select", json={"accountId": str(uuid4())}, headers=headers)
        assert response.status_code == 401
        assert gateway.sessions.get(cookie) is None
        assert not any(call[0] == "POST" and call[1] == "/v1/accounts" for call in state["calls"])


def test_account_phase_timings_never_include_session_proofs(hosted, capsys):
    import json

    client, gateway, state, _ = hosted
    with client:
        sign_in(client, state)
        cookie = client.cookies.get(gateway.config.cookie_name)
        token = gateway.sessions.get(cookie).access_token
        capsys.readouterr()
        response = client.get("/api/session")
        assert response.status_code == 200
        output = capsys.readouterr().out
        entries = [json.loads(line) for line in output.splitlines()]
        phases = {entry["phase"] for entry in entries if entry.get("event") == "console_phase"}
        assert {"session_read", "token_verify"} <= phases
        assert cookie not in output and token not in output and response.json()["csrfToken"] not in output


def google_account_mode(gateway, state):
    """Use one synthetic PersonDirectory for identity and membership fixtures."""
    result = google_mode(gateway, state)
    person = result[1]
    transport = gateway.accounts.transport

    def aligned(*args, **kwargs):
        response = transport(*args, **kwargs)
        state["person"] = person
        if "person" in response:
            response["person"] = person
        if response.get("allowed"):
            response["personId"] = person["id"]
        return response

    gateway.accounts.transport = aligned
    return result


def test_http_preflight_is_not_repeated_in_context_but_execution_rechecks_every_request(hosted):
    client, gateway, state, account_id = hosted
    _, person, _ = google_account_mode(gateway, state)
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        headers = {
            "Origin": "https://console.example.test",
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Boundary fixture"}, headers=headers).status_code == 201
        lookups = []

        def me(_):
            lookups.append("fresh")
            return person

        gateway.google_identity.me = me
        for expected in (1, 2):
            result = client.post(
                "/graphql",
                params={"account": account_id},
                json={"query": "{runs{name}}"},
                headers={"x-cyclotron-preflight": "forged"},
            )
            assert result.json()["data"]["runs"][0]["name"] == "Private account run"
            assert len(lookups) == expected


@pytest.mark.parametrize("change", ["revoked", "outage", "membership"])
def test_revocation_or_outage_between_http_preflight_and_execution_denies_before_store(hosted, change):
    client, gateway, state, account_id = hosted
    _, person, _ = google_account_mode(gateway, state)
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        headers = {
            "Origin": "https://console.example.test",
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Boundary fixture"}, headers=headers).status_code == 201
        lookups = []

        def me(_):
            lookups.append("fresh")
            if len(lookups) == 1:
                if change == "revoked":
                    return None
                if change == "outage":
                    raise ProspectusUnavailable("authority unavailable")
            return person

        if change == "membership":
            state["accounts"] = []

        gateway.google_identity.me = me
        gateway.access.workspace_for_account = lambda _: pytest.fail("denied operation reached workspace state")
        response = client.post("/graphql", params={"account": account_id}, json={"query": "{runs{name}}"})
        assert response.status_code == {"revoked": 401, "outage": 503, "membership": 200}[change]
        assert response.json().get("data") is None
        assert len(lookups) == 1


def test_cross_origin_or_initial_preflight_failure_never_reaches_execution(hosted):
    client, gateway, state, account_id = hosted
    google_account_mode(gateway, state)
    with client:
        sign_in(client, state)
        gateway.google_identity.me = lambda _: pytest.fail("cross-origin request reached identity lookup")
        result = client.post(
            "/graphql",
            params={"account": account_id},
            json={"query": "{runs{name}}"},
            headers={"Origin": "https://evil.example.test", "x-cyclotron-preflight": "forged"},
        )
        assert result.status_code == 403
        gateway.access.workspace_for_account = lambda _: pytest.fail("failed preflight reached workspace")
        gateway.google_identity.me = lambda _: None
        assert (
            client.post(
                "/graphql",
                params={"account": account_id},
                json={"query": "{runs{name}}"},
                headers={"x-cyclotron-preflight": "forged"},
            ).status_code
            == 401
        )


def test_websocket_context_authenticates_without_an_http_preflight_marker(hosted):
    client, gateway, state, account_id = hosted
    _, person, _ = google_account_mode(gateway, state)
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        headers = {
            "Origin": "https://console.example.test",
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Websocket fixture"}, headers=headers).status_code == 201
        lookups = []
        gateway.google_identity.me = lambda _: lookups.append("fresh") or person
        with client.websocket_connect(
            "wss://console.example.test/graphql?account=" + account_id, subprotocols=["graphql-transport-ws"]
        ) as socket:
            socket.send_json({"type": "connection_init"})
            assert socket.receive_json()["type"] == "connection_ack"
            socket.send_json({"id": "read", "type": "subscribe", "payload": {"query": "{runs{name}}"}})
            message = socket.receive_json()
            assert message["type"] == "next"
            assert message["payload"]["data"]["runs"][0]["name"] == "Private account run"
            assert len(lookups) == 2


@pytest.mark.parametrize("marker", ["absent", "invalid"])
def test_context_without_its_private_marker_performs_full_authentication(hosted, marker):
    client, gateway, state, account_id = hosted
    _, person, _ = google_account_mode(gateway, state)
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        headers = {
            "Origin": "https://console.example.test",
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Marker fixture"}, headers=headers).status_code == 201
        original = client.app.router.middleware_stack

        async def change_marker(scope, receive, send):
            if scope.get("type") == "http" and scope.get("path") == "/graphql":
                if marker == "absent":
                    scope["state"].pop("_cyclotron_preflight", None)
                else:
                    scope["state"]["_cyclotron_preflight"] = object()
            await original(scope, receive, send)

        client.app.router.middleware_stack = change_marker
        lookups = []
        gateway.google_identity.me = lambda _: lookups.append("fresh") or person
        result = client.post("/graphql", params={"account": account_id}, json={"query": "{runs{name}}"})
        assert result.json()["data"]["runs"][0]["name"] == "Private account run"
        assert len(lookups) == 1


def test_one_identities_me_and_one_authorize_per_graphql_events_and_timeline_request(hosted):
    client, gateway, state, account_id = hosted
    _, person, _ = google_account_mode(gateway, state)
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        headers = {
            "Origin": "https://console.example.test",
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Counted"}, headers=headers).status_code == 201
        run = state["stores"][account_id].runs()[0]
        identity_lookups = []
        authorizations = []
        gateway.google_identity.me = lambda _: identity_lookups.append("me") or person
        central = gateway.accounts.transport

        def counting(method, path, access_token, body=None, key=None):
            if path.endswith("/authorize"):
                authorizations.append(body["operation"])
            return central(method, path, access_token, body, key)

        gateway.accounts.transport = counting
        requests = {
            "graphql": lambda: client.post("/graphql", params={"account": account_id}, json={"query": "{runs{name}}"}),
            "events": lambda: client.get(f"/api/runs/{run['id']}/events", params={"account": account_id}),
            "timeline": lambda: client.get(f"/runs/{run['id']}/timeline", params={"account": account_id}),
        }
        for name, send in requests.items():
            identity_lookups.clear()
            authorizations.clear()
            assert send().status_code == 200, name
            assert identity_lookups == ["me"], name
            assert authorizations == ["console:read"], name


def test_membership_revoked_between_requests_is_refused_on_the_next_request(hosted):
    client, gateway, state, account_id = hosted
    google_account_mode(gateway, state)
    with client:
        sign_in(client, state)
        session = client.get("/api/session").json()
        headers = {
            "Origin": "https://console.example.test",
            "X-CSRF-Token": session["csrfToken"],
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post("/api/accounts", json={"name": "Revocable"}, headers=headers).status_code == 201
        query = {"query": "{runs{name}}"}
        first = client.post("/graphql", params={"account": account_id}, json=query)
        assert first.json()["data"]["runs"][0]["name"] == "Private account run"
        state["accounts"] = []
        second = client.post("/graphql", params={"account": account_id}, json=query)
        assert second.json().get("data") is None and second.json().get("errors")


def test_each_hosted_request_logs_its_route_template_and_duration_without_secrets(hosted, capsys):
    import json

    client, gateway, state, account_id = hosted
    google_account_mode(gateway, state)
    with client:
        sign_in(client, state)
        cookie = client.cookies.get(gateway.config.cookie_name)
        token = gateway.sessions.get(cookie).access_token
        capsys.readouterr()
        client.get("/api/runs/some-run/events", params={"account": account_id})
        output = capsys.readouterr().out
        entries = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        requests = [entry for entry in entries if entry.get("phase") == "request"]
        assert [entry["route"] for entry in requests] == ["/api/runs/{run_id}/events"]
        assert requests[0]["event"] == "console_phase" and isinstance(requests[0]["durationMs"], int)
        assert cookie not in output and token not in output and account_id not in output
