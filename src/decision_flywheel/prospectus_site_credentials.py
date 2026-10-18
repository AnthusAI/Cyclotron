"""Re-verify a calling site's forwarded session credential with Prospectus.

The site forwards the signed-in person's access token. The lab asks Prospectus,
with its own temporary runtime role, which person the token belongs to and what
role that person holds on the pinned account. The lab keeps no membership data.
"""

import asyncio

from .prospectus_accounts import AccountAccessDenied
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
