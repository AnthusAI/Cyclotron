"""Re-verify a calling site's forwarded session credential with Prospectus.

The site forwards the signed-in person's access token. The lab asks Prospectus,
with its own temporary runtime role, which person the token belongs to and what
role that person holds on the pinned account. The lab keeps no membership data.
"""

import asyncio

from .prospectus_accounts import AccountAccessDenied
from .publication_lab import PublicationError
from .site_credentials import VerifiedSiteCredential


def prospectus_site_credential_verifier(accounts):
    """accounts is a prospectus_accounts.AccountsClient on the lab's runtime credentials.

    A refusal returns None. An unavailable account service propagates so the host
    answers 503 and no lab is selected.
    """

    def verify_blocking(token, pinned_account):
        try:
            person = accounts.resolve_identity(token).get("person")
            if not isinstance(person, dict) or not isinstance(person.get("id"), str):
                return None
            authorization = accounts.authorize(token, pinned_account, person["id"], "console:read")
        except AccountAccessDenied:
            return None
        return VerifiedSiteCredential(authorization["accountId"], authorization["personId"], authorization["role"])

    async def verify_credential(token, pinned_account):
        return await asyncio.to_thread(verify_blocking, token, pinned_account)

    return verify_credential


def prospectus_bearer_authority(accounts):
    """Authority for the curation routes: distinguishes an unknown token from a refused role.

    Returns async authorize(token, pinned_account, operation) where operation is
    console:read or console:write. An unrecognized token raises a 401 PublicationError;
    a recognized person without that role on the pinned account raises 403. An
    unavailable account service propagates so the route answers 503.
    """

    def authorize_blocking(token, pinned_account, operation):
        try:
            person = accounts.resolve_identity(token).get("person")
        except AccountAccessDenied:
            raise PublicationError("bearer credential refused", 401, code="bearer_invalid") from None
        if not isinstance(person, dict) or not isinstance(person.get("id"), str):
            raise PublicationError("bearer credential refused", 401, code="bearer_invalid")
        try:
            authorization = accounts.authorize(token, pinned_account, person["id"], operation)
        except AccountAccessDenied:
            raise PublicationError("curation access refused", 403, code="access_refused") from None
        return VerifiedSiteCredential(authorization["accountId"], authorization["personId"], authorization["role"])

    async def authorize(token, pinned_account, operation):
        return await asyncio.to_thread(authorize_blocking, token, pinned_account, operation)

    return authorize
