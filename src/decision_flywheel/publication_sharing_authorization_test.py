"""Fresh synthetic account authorization; no network, tokens, or live grants."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from .account_sessions import SessionRejected
from .prospectus_accounts import AccountAccessDenied, AccountsClient, ProspectusUnavailable, VerifiedAccountSession
from .publication_sharing import GrantSpec, SharingDenied, SharingGrant, SharingUnavailable, VerifiedPrincipal
from .publication_sharing_authorization import ProspectusSharingAuthorization


class Host:
    def __init__(self, account="consumer", role="viewer"):
        self.connection = SimpleNamespace(
            session=VerifiedAccountSession("synthetic-access", f"{account}-person", account, f"{account}-session")
        )
        self.role = role
        self.allowed = True
        self.revision = 1
        self.error = None
        self.after_accounts = None
        self.calls = []
        self.scope_calls = []
        self.scope_effect = None
        self.scope_result = None
        self.spec = GrantSpec(
            grant_id="grant-one",
            owner_account_id="owner",
            consumer_account_id="consumer",
            publication_id="consumer-publication",
            engine_id="shared-central",
            binding_revision="binding-one",
            execution_outputs=("consumer-output", "private-sibling"),
            visible_outputs=("consumer-output",),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            max_requests=3,
            billing_account_id="owner",
            allowed_operations=("infer", "deliver"),
        )
        self.grant = SharingGrant(self.spec, "owner-person")
        self.accounts = AccountsClient(self.transport)
        self.adapter = ProspectusSharingAuthorization(
            self.accounts,
            resolve_session=self.resolve_session,
            authorize_owner_scope=self.owner_scope,
            authorize_consumer_scope=self.consumer_scope,
        )

    async def resolve_session(self, connection):
        return connection.session

    def transport(self, method, path, token, body):
        self.calls.append((method, path, token, body))
        if self.error:
            raise self.error
        session = self.connection.session
        # The token belongs only to the selected account. No membership in owner
        # is supplied for the consumer, even when the engine is owned there.
        result = {
            "allowed": self.allowed,
            "accountId": session.account_id,
            "personId": session.person_id,
            "product": "cyclotron",
            "operation": body["operation"],
            "revision": self.revision,
            "role": self.role,
        }
        if self.after_accounts:
            self.after_accounts()
        return result

    async def owner_scope(self, principal, spec, operation):
        self.scope_calls.append(("owner", principal, spec, operation))
        if spec != self.spec:
            raise SharingDenied("the exact engine and specification are not approved")
        if self.scope_effect:
            await self.scope_effect()
        return self.scope_result

    async def consumer_scope(self, principal, grant, operation):
        self.scope_calls.append(("consumer", principal, grant, operation))
        if grant.spec != self.spec or operation != "deliver":
            raise SharingDenied("the exact publication and operation are not approved")
        if self.scope_effect:
            await self.scope_effect()
        return self.scope_result

    async def request(self):
        request = await self.adapter.for_request(self.connection)
        return request, await request.resolve_principal()


def test_a_consumer_viewer_uses_only_its_own_account_read_and_exact_publication_operation_scope():
    async def scenario():
        host = Host()
        request, principal = await host.request()
        assert principal == VerifiedPrincipal("consumer", "consumer-person")
        assert await request.authorize_use(principal, host.grant, "deliver") is None
        assert host.scope_calls == [("consumer", principal, host.grant, "deliver")]
        assert len(host.calls) >= 4
        assert all(call[:2] == ("POST", "/v1/accounts/consumer/authorize") for call in host.calls)
        assert all(call[3] == {"operation": "console:read"} for call in host.calls)
        with pytest.raises(SharingDenied):
            await request.authorize_use(principal, host.grant, "infer")

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["issue", "revoke"])
def test_an_owner_needs_exact_current_role_and_exact_specification_permission(operation):
    async def scenario():
        host = Host("owner", "owner")
        request, principal = await host.request()
        assert await request.authorize_owner(principal, host.spec, operation) is None
        assert host.scope_calls == [("owner", principal, host.spec, operation)]
        assert [call[3]["operation"] for call in host.calls][-2:] == ["console:write", "console:write"]
        assert all(call[1] == "/v1/accounts/owner/authorize" for call in host.calls)
        for changed in (replace(host.spec, engine_id="another-engine"), replace(host.spec, visible_outputs=("private-sibling",))):
            with pytest.raises(SharingDenied):
                await request.authorize_owner(principal, changed, operation)

    asyncio.run(scenario())


@pytest.mark.parametrize("role", ["admin", "member", "viewer"])
def test_owner_account_membership_cannot_substitute_for_the_owner_role(role):
    async def scenario():
        host = Host("owner", role)
        request, principal = await host.request()
        with pytest.raises(SharingDenied):
            await request.authorize_owner(principal, host.spec, "issue")
        assert host.scope_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["owner", "consumer"])
@pytest.mark.parametrize("result", [True, False, {}, "approved"])
def test_scope_callbacks_cannot_return_body_shaped_or_boolean_authority(boundary, result):
    async def scenario():
        host = Host("owner", "owner") if boundary == "owner" else Host()
        host.scope_result = result
        request, principal = await host.request()
        with pytest.raises(SharingDenied):
            if boundary == "owner":
                await request.authorize_owner(principal, host.spec, "issue")
            else:
                await request.authorize_use(principal, host.grant, "deliver")

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["resolve_session", "authorize_owner_scope", "authorize_consumer_scope"])
def test_all_host_authority_callbacks_are_mandatory(missing):
    host = Host()
    callbacks = {
        "resolve_session": host.resolve_session,
        "authorize_owner_scope": host.owner_scope,
        "authorize_consumer_scope": host.consumer_scope,
    }
    callbacks[missing] = None
    with pytest.raises(TypeError):
        ProspectusSharingAuthorization(host.accounts, **callbacks)


def test_the_membership_ceiling_requires_the_real_accounts_client():
    host = Host()
    with pytest.raises(TypeError):
        ProspectusSharingAuthorization(
            SimpleNamespace(authorize=lambda *args: {"role": "owner"}),
            resolve_session=host.resolve_session,
            authorize_owner_scope=host.owner_scope,
            authorize_consumer_scope=host.consumer_scope,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"access_token": "a-different-synthetic-token"},
        {"account_id": "other"},
        {"person_id": "other-person"},
        {"session_ref": "other-session"},
    ],
)
def test_a_request_cannot_follow_a_switched_session_account_person_or_token(change):
    async def scenario():
        host = Host()
        request, _ = await host.request()
        calls = len(host.calls)
        host.connection.session = replace(host.connection.session, **change)
        with pytest.raises(SharingDenied):
            await request.resolve_principal()
        assert len(host.calls) == calls

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["owner", "consumer"])
@pytest.mark.parametrize("change", ["membership", "session", "account", "person", "token", "outage"])
def test_changes_during_a_scope_callback_are_rechecked_before_return(boundary, change):
    async def scenario():
        host = Host("owner", "owner") if boundary == "owner" else Host()
        request, principal = await host.request()

        async def change_authority():
            await asyncio.sleep(0)
            if change == "membership":
                host.allowed = False
            elif change == "outage":
                host.error = ProspectusUnavailable("synthetic-private-detail")
            else:
                field = {"session": "session_ref", "account": "account_id", "person": "person_id", "token": "access_token"}[
                    change
                ]
                host.connection.session = replace(host.connection.session, **{field: "changed"})

        host.scope_effect = change_authority
        expected = SharingUnavailable if change == "outage" else SharingDenied
        with pytest.raises(expected):
            if boundary == "owner":
                await request.authorize_owner(principal, host.spec, "issue")
            else:
                await request.authorize_use(principal, host.grant, "deliver")
        assert len(host.scope_calls) == 1

    asyncio.run(scenario())


def test_owner_role_is_rechecked_after_the_exact_scope_callback_returns():
    async def scenario():
        host = Host("owner", "owner")
        request, principal = await host.request()

        async def lose_ownership():
            host.role = "admin"
            host.revision += 1

        host.scope_effect = lose_ownership
        with pytest.raises(SharingDenied):
            await request.authorize_owner(principal, host.spec, "revoke")

    asyncio.run(scenario())


def test_session_changes_while_accounts_authorization_is_in_flight_are_rejected():
    async def scenario():
        host = Host()
        request, _ = await host.request()
        host.after_accounts = lambda: setattr(
            host.connection, "session", replace(host.connection.session, session_ref="switched-during-check")
        )
        with pytest.raises(SharingDenied):
            await request.resolve_principal()

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["owner", "consumer"])
@pytest.mark.parametrize("wrong", ["principal", "account", "operation"])
def test_wrong_principal_account_or_operation_never_reaches_scope_permission(boundary, wrong):
    async def scenario():
        host = Host("owner", "owner") if boundary == "owner" else Host()
        request, principal = await host.request()
        if wrong == "principal":
            principal = VerifiedPrincipal(principal.account_id, "another-person")
        if boundary == "owner":
            spec = (
                replace(host.spec, owner_account_id="other-owner", billing_account_id="other-owner")
                if wrong == "account"
                else host.spec
            )
            invoke = lambda: request.authorize_owner(principal, spec, "admin" if wrong == "operation" else "issue")
        else:
            grant = (
                replace(host.grant, spec=replace(host.spec, consumer_account_id="other-consumer"))
                if wrong == "account"
                else host.grant
            )
            invoke = lambda: request.authorize_use(principal, grant, "read-source" if wrong == "operation" else "deliver")
        with pytest.raises(SharingDenied):
            await invoke()
        assert host.scope_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["revoked", "operation"])
def test_inactive_or_operation_restricted_grants_never_reach_consumer_scope_permission(change):
    async def scenario():
        host = Host()
        request, principal = await host.request()
        grant = (
            replace(host.grant, status="revoked")
            if change == "revoked"
            else replace(host.grant, spec=replace(host.spec, allowed_operations=("infer",)))
        )
        with pytest.raises(SharingDenied):
            await request.authorize_use(principal, grant, "deliver")
        assert host.scope_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", [SessionRejected, AccountAccessDenied, SharingDenied, PermissionError, ProspectusUnavailable, OSError]
)
@pytest.mark.parametrize("boundary", ["session", "accounts", "scope"])
def test_authority_failures_are_closed_and_exception_details_are_not_exposed(failure, boundary):
    async def scenario():
        host = Host()
        request, principal = await host.request()
        detail = "synthetic-private-error-with-access-token"
        if boundary == "accounts":
            host.error = failure(detail)
        elif boundary == "session":

            async def reject(_):
                raise failure(detail)

            host.adapter = ProspectusSharingAuthorization(
                host.accounts,
                resolve_session=reject,
                authorize_owner_scope=host.owner_scope,
                authorize_consumer_scope=host.consumer_scope,
            )
        else:

            async def reject():
                raise failure(detail)

            host.scope_effect = reject
        expected = SharingUnavailable if failure in (ProspectusUnavailable, OSError) else SharingDenied
        with pytest.raises(expected) as error:
            if boundary == "session":
                await host.adapter.for_request(host.connection)
            else:
                await request.authorize_use(principal, host.grant, "deliver")
        assert detail not in str(error.value)
        assert error.value.__suppress_context__

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "session",
    [
        None,
        {"account_id": "consumer", "person_id": "consumer-person"},
        VerifiedAccountSession("", "person", "consumer", "session"),
        VerifiedAccountSession("synthetic", "", "consumer", "session"),
        VerifiedAccountSession("synthetic", "person", "", "session"),
        VerifiedAccountSession("synthetic", "person", "consumer"),
        VerifiedAccountSession("synthetic\r\nheader", "person", "consumer", "session"),
    ],
)
def test_only_complete_host_verified_sessions_can_create_a_request_context(session):
    async def scenario():
        host = Host()
        host.connection.session = session
        with pytest.raises(SharingDenied):
            await host.adapter.for_request(host.connection)
        assert host.calls == []

    asyncio.run(scenario())


def test_concurrent_request_contexts_do_not_share_or_replace_their_principals():
    async def scenario():
        sessions = {
            name: VerifiedAccountSession(f"synthetic-{name}", f"{name}-person", name, f"{name}-session")
            for name in ("consumer", "another")
        }
        calls = []

        def transport(method, path, token, body):
            account = path.split("/")[3]
            assert token == sessions[account].access_token
            calls.append((account, body["operation"]))
            return dict(
                allowed=True,
                accountId=account,
                personId=sessions[account].person_id,
                product="cyclotron",
                operation=body["operation"],
                revision=1,
                role="viewer",
            )

        async def resolve(connection):
            await asyncio.sleep(0)
            return sessions[connection]

        adapter = ProspectusSharingAuthorization(
            AccountsClient(transport),
            resolve_session=resolve,
            authorize_owner_scope=lambda *args: None,
            authorize_consumer_scope=lambda *args: None,
        )
        first, second = await asyncio.gather(adapter.for_request("consumer"), adapter.for_request("another"))
        principals = await asyncio.gather(first.resolve_principal(), second.resolve_principal())
        assert principals == [VerifiedPrincipal("consumer", "consumer-person"), VerifiedPrincipal("another", "another-person")]
        sessions["another"] = replace(sessions["another"], person_id="changed")
        assert await first.resolve_principal() == principals[0]
        with pytest.raises(SharingDenied):
            await second.resolve_principal()
        assert all(operation == "console:read" for _, operation in calls)
        assert "synthetic-" not in repr(first)

    asyncio.run(scenario())
