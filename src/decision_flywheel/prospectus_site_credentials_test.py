"""The lab re-verifies a forwarded person token with the account service on every call."""

import asyncio

import pytest

from .prospectus_accounts import AccountAccessDenied, ProspectusUnavailable
from .prospectus_site_credentials import prospectus_bearer_authority, prospectus_site_credential_verifier
from .publication_lab import PublicationError
from .site_credentials import VerifiedSiteCredential


class FakeAccounts:
    def __init__(self):
        self.members = {("person-1", "account-a"): "editor"}
        self.tokens = {"token-1": "person-1"}
        self.available = True
        self.calls = []

    def resolve_identity(self, token):
        self.calls.append(("resolve", token))
        if not self.available:
            raise ProspectusUnavailable("down")
        person = self.tokens.get(token)
        if person is None:
            raise AccountAccessDenied("unknown token")
        return {"person": {"id": person}}

    def authorize(self, token, account_id, person_id, operation):
        self.calls.append(("authorize", account_id, operation))
        role = self.members.get((person_id, account_id))
        if role is None:
            raise AccountAccessDenied("account access refused")
        return {"accountId": account_id, "personId": person_id, "role": role}


def test_verified_person_and_role_become_a_site_credential_and_every_failure_closes():
    accounts = FakeAccounts()
    verify = prospectus_site_credential_verifier(accounts)

    async def scenario():
        assert await verify("token-1", "account-a") == VerifiedSiteCredential("account-a", "person-1", "editor")
        assert accounts.calls == [("resolve", "token-1"), ("authorize", "account-a", "console:read")]
        assert await verify("token-1", "account-b") is None
        assert await verify("unknown", "account-a") is None
        del accounts.members[("person-1", "account-a")]
        assert await verify("token-1", "account-a") is None
        accounts.available = False
        with pytest.raises(ProspectusUnavailable):
            await verify("token-1", "account-a")

    asyncio.run(scenario())


def test_bearer_authority_tells_an_unknown_token_from_a_refused_role_and_passes_the_requested_operation():
    accounts = FakeAccounts()
    authorize = prospectus_bearer_authority(accounts)

    async def refusal(token, account):
        with pytest.raises(PublicationError) as caught:
            await authorize(token, account, "console:write")
        return caught.value.status, caught.value.code

    async def scenario():
        credential = await authorize("token-1", "account-a", "console:write")
        assert credential == VerifiedSiteCredential("account-a", "person-1", "editor")
        assert accounts.calls == [("resolve", "token-1"), ("authorize", "account-a", "console:write")]
        assert await refusal("unknown", "account-a") == (401, "bearer_invalid")
        assert await refusal("token-1", "account-b") == (403, "access_refused")
        accounts.available = False
        with pytest.raises(ProspectusUnavailable):
            await authorize("token-1", "account-a", "console:read")

    asyncio.run(scenario())
