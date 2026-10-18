"""Lab-side authentication of a calling site by a credential its account service issued.

A site calls the central lab with a short-lived bearer credential issued for the
selected account and role. The lab re-verifies that credential with the account
service on every operation through an injected verifier, so a revoked membership
stops working at once. This module owns no account-service client, credential
issuer, shared lab key, role mapping or lab storage.
"""

import inspect
from dataclasses import dataclass

from .publication_lab import PublicationAccess, PublicationActor, PublicationError, text


@dataclass(frozen=True)
class VerifiedSiteCredential:
    account_id: str
    subject_id: str
    role: str

    def __post_init__(self):
        text(self.account_id, "account identity")
        text(self.subject_id, "subject identity")
        text(self.role, "role")


async def _resolved(value):
    return await value if inspect.isawaitable(value) else value


def bearer_token(request):
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise PublicationError("site credential required", 401)
    return token.strip()


def site_credential_access(verify_credential, lab_for_account, grants_for):
    """Build the resolver for create_publication_host from verified site credentials.

    verify_credential(token, pinned_account_id) asks the account service and returns
    a VerifiedSiteCredential, or None when the credential is unknown, expired or revoked.
    lab_for_account(account_id) returns that account's open PublicationLab or None.
    grants_for(credential, publication_ids) returns (publication_id, capability) pairs;
    there is deliberately no default role mapping.
    """

    async def resolve_access(request, publication, capability):
        token = bearer_token(request)
        pinned_account = request.headers.get("x-cyclotron-account")
        credential = await _resolved(verify_credential(token, pinned_account))
        if not isinstance(credential, VerifiedSiteCredential):
            raise PublicationError("site credential refused", 403)
        if credential.account_id != pinned_account:
            raise PublicationError("site credential is for another account", 403)
        lab = await _resolved(lab_for_account(credential.account_id))
        if lab is None or lab.account_id != credential.account_id:
            raise PublicationError("account has no lab", 403)
        grants = frozenset(
            (publication_id, granted)
            for publication_id, granted in grants_for(credential, tuple(lab.bindings))
            if publication_id in lab.bindings
        )
        if (publication, capability) not in grants:
            raise PublicationError("publication capability refused", 403)
        return PublicationAccess(lab, PublicationActor(credential.account_id, credential.subject_id, grants))

    return resolve_access
