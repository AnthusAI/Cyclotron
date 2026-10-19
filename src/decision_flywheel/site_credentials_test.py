"""A site reaches the central lab only with a credential the account service still accepts."""

import asyncio

import httpx
import pytest

from .embedded_cyclotron import Cyclotron
from .publication_lab import HTTPPublicationClient, PublicationError, PublicationLab, create_publication_host
from .publication_lab_test import BINDINGS, DEFINITION, ITEM, OfflineModel
from .site_credentials import VerifiedSiteCredential, site_credential_access


def decide_payload():
    return {"item": ITEM, "bindingRevision": "binding-1"}


def test_site_credentials_are_reverified_per_operation_and_bound_to_the_pinned_account(tmp_path):
    async def scenario():
        with Cyclotron.open(tmp_path / "a", DEFINITION, OfflineModel()) as engine:
            lab = PublicationLab(engine, account_id="account-a", bindings=BINDINGS)
            accepted = {
                "token-a": VerifiedSiteCredential("account-a", "person-1", "editor"),
                "token-b": VerifiedSiteCredential("account-b", "person-2", "editor"),
            }
            verifications = []

            def verify(token, pinned_account):
                verifications.append((token, pinned_account))
                return accepted.get(token)

            def grants_for(credential, publication_ids):
                actions = ("read", "decide") if credential.role == "editor" else ("read",)
                return [(publication, action) for publication in publication_ids for action in actions]

            app = create_publication_host(
                site_credential_access(verify, lambda account: lab if account == "account-a" else None, grants_for),
                require_execution_guard=False,
            )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lab.test") as http:

                def client(account, token):
                    http.headers["authorization"] = f"Bearer {token}" if token else ""
                    return HTTPPublicationClient(http, account_id=account)

                publication = "threat-intelligence"
                decided = await client("account-a", "token-a").command(publication, "decide", decide_payload())
                assert decided["accountId"] == "account-a"
                with pytest.raises(PublicationError) as missing:
                    await client("account-a", None).command(publication, "status", {})
                assert missing.value.status == 401
                with pytest.raises(PublicationError) as other_account:
                    await client("account-a", "token-b").command(publication, "status", {})
                assert other_account.value.status == 403
                with pytest.raises(PublicationError) as no_lab:
                    await client("account-b", "token-b").command(publication, "status", {})
                assert no_lab.value.status == 403
                accepted.pop("token-a")
                with pytest.raises(PublicationError) as revoked:
                    await client("account-a", "token-a").command(publication, "status", {})
                assert revoked.value.status == 403
                assert len(verifications) == 4

    asyncio.run(scenario())


def test_role_without_the_capability_is_refused_before_the_engine_runs(tmp_path):
    async def scenario():
        model = OfflineModel()
        with Cyclotron.open(tmp_path / "a", DEFINITION, model) as engine:
            lab = PublicationLab(engine, account_id="account-a", bindings=BINDINGS)
            viewer = VerifiedSiteCredential("account-a", "person-3", "viewer")
            app = create_publication_host(
                site_credential_access(
                    lambda token, account: viewer,
                    lambda account: lab,
                    lambda credential, ids: [(publication, "read") for publication in ids],
                ),
                require_execution_guard=False,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://lab.test", headers={"authorization": "Bearer t"}
            ) as http:
                with pytest.raises(PublicationError) as refused:
                    await HTTPPublicationClient(http, account_id="account-a").command(
                        "threat-intelligence", "decide", decide_payload()
                    )
                assert refused.value.status == 403 and model.calls == 0

    asyncio.run(scenario())
