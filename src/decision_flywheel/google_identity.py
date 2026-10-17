"""Optional central Google enrollment client, separate from membership storage."""
from dataclasses import dataclass, field
import re
import time
from uuid import UUID

from .prospectus_accounts import ProspectusUnavailable


@dataclass(frozen=True)
class GoogleChallenge:
    challenge_id: str
    nonce: str = field(repr=False)
    expires_at: int


def uuid4_string(value):
    try:
        parsed = UUID(value)
        return isinstance(value, str) and parsed.version == 4 and str(parsed) == value
    except (ValueError, TypeError, AttributeError):
        return False


class GoogleIdentityClient:
    """The host explicitly injects this isolated service; it grants no membership.

    Proofs live only in the BFF callback until central resolution finishes. Later
    requests use access-only lookup, which must never create an identity alias.
    """

    def __init__(self, transport, *, clock=time.time):
        self.transport = transport
        self.clock = clock

    def challenge(self):
        result = self.transport('POST', '/v1/identities/google/challenge', None, {})
        nonce, expiry = result.get('nonce'), result.get('expiresAt')
        if (not uuid4_string(result.get('challengeId')) or not isinstance(nonce, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', nonce)
                or type(expiry) is not int or not self.clock() < expiry <= self.clock() + 300):
            raise ProspectusUnavailable('invalid identity challenge')
        return GoogleChallenge(result['challengeId'], nonce, expiry)

    @staticmethod
    def _person(result, *, optional=False):
        person = result.get('person')
        if optional and 'person' in result and person is None:
            return None
        if (not isinstance(person, dict) or not uuid4_string(person.get('id'))
                or not isinstance(person.get('createdAt'), str) or not person['createdAt']):
            raise ProspectusUnavailable('invalid identity service response')
        return {'id': person['id'], 'createdAt': person['createdAt']}

    def resolve(self, access_token, id_token, challenge_id):
        if not uuid4_string(challenge_id):
            raise ValueError('identity resolution requires a challenge UUIDv4')
        result = self.transport('POST', '/v1/identities/google/resolve', access_token,
            {'challengeId': challenge_id}, identity_token=id_token)
        return self._person(result)

    def me(self, access_token):
        return self._person(self.transport('GET', '/v1/identities/me', access_token), optional=True)
