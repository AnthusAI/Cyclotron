"""Curation authority that verifies an editor's Papyrus token as the lab verifier role.

The BFF assumes the dedicated cyclotron-lab-verifier role (a credential that can reach
only GET /v1/identities/me and POST /v1/accounts/*/authorize of the central accounts
API), caches the temporary credentials and refreshes them before they expire, and signs
every accounts call with them. The editor's token is forwarded in
X-Prospectus-Human-Token; Prospectus verifies it. With the role ARN or the accounts URL
unset the authority is unavailable and the curation routes answer 503, never open.
"""

import re
import threading
from datetime import datetime, timedelta, timezone

from botocore.credentials import ReadOnlyCredentials

from .prospectus_accounts import AccountsClient, ProspectusUnavailable, SigV4Transport
from .prospectus_site_credentials import prospectus_bearer_authority

VERIFIER_ROLE_ARN = re.compile(r"arn:aws:iam::[0-9]{12}:role/cyclotron-lab-verifier")
SESSION_NAME = "cyclotron-lab-curation"
SESSION_SECONDS = 3600
REFRESH_MARGIN = timedelta(minutes=5)


def valid_verifier_role_arn(value):
    return isinstance(value, str) and VERIFIER_ROLE_ARN.fullmatch(value) is not None


class AssumedRoleSession:
    """botocore-session-shaped source of verifier credentials: get_credentials().get_frozen_credentials().

    sts is any client with assume_role(RoleArn=, RoleSessionName=, DurationSeconds=); it is
    created lazily by sts_factory so nothing touches the network until the first call.
    Credentials are cached and renewed REFRESH_MARGIN before expiry. If a renewal fails
    while the cached credentials are still valid they are used; otherwise the call fails
    closed with ProspectusUnavailable. Failures are never cached and carry no detail.
    """

    def __init__(self, role_arn, *, sts=None, sts_factory=None, now=None):
        if not valid_verifier_role_arn(role_arn):
            raise ValueError("exact cyclotron-lab-verifier role ARN required")
        if sts is None and sts_factory is None:
            raise ValueError("an STS client or factory is required")
        self.role_arn = role_arn
        self._sts, self._sts_factory = sts, sts_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._credentials = None
        self._expires = None

    def get_credentials(self):
        return self

    def get_frozen_credentials(self):
        with self._lock:
            now = self._now()
            if self._credentials is None or now >= self._expires - REFRESH_MARGIN:
                try:
                    self._renew()
                except Exception:
                    if self._credentials is None or now >= self._expires:
                        raise ProspectusUnavailable("verifier credentials unavailable") from None
            return self._credentials

    def _renew(self):
        if self._sts is None:
            self._sts = self._sts_factory()
        granted = self._sts.assume_role(RoleArn=self.role_arn, RoleSessionName=SESSION_NAME, DurationSeconds=SESSION_SECONDS)[
            "Credentials"
        ]
        expires = granted["Expiration"]
        if not isinstance(expires, datetime) or expires.tzinfo is None:
            raise ValueError("invalid expiration")
        parts = (granted["AccessKeyId"], granted["SecretAccessKey"], granted["SessionToken"])
        if not all(isinstance(value, str) and value for value in parts):
            raise ValueError("incomplete credentials")
        credentials = ReadOnlyCredentials(*parts)
        self._credentials, self._expires = credentials, expires


def unavailable_authority(reason="verifier role or accounts API URL not configured"):
    """Fail-closed authority: every call raises, so the curation routes answer 503 authority_unavailable."""

    async def authorize(token, pinned_account, operation):
        raise ProspectusUnavailable(reason)

    return authorize


def lab_verifier_authority(role_arn, accounts_url, region, *, sts=None, sts_factory=None, client=None, now=None):
    """Build the curation authority, or the fail-closed one when either setting is empty or invalid."""
    if not role_arn or not accounts_url:
        return unavailable_authority()
    try:
        session = AssumedRoleSession(role_arn, sts=sts, sts_factory=sts_factory or default_sts_factory(region), now=now)
        transport = SigV4Transport(accounts_url, region, session=session, client=client)
    except ValueError:
        return unavailable_authority("verifier configuration invalid")
    return prospectus_bearer_authority(AccountsClient(transport))


def default_sts_factory(region):
    def create():
        from botocore.session import get_session

        return get_session().create_client("sts", region_name=region)

    return create
