"""Central Google proof transport uses synthetic clients only."""
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from botocore.credentials import Credentials

from .google_identity import GoogleIdentityClient
from .prospectus_accounts import AccountAccessDenied, ProspectusUnavailable, SigV4Transport


def test_challenge_is_machine_signed_and_resolution_signs_both_independent_proofs():
    seen = []
    challenge_id = str(uuid4())
    person = {'id': str(uuid4()), 'createdAt': '2026-10-14T00:00:00Z'}
    def respond(request):
        seen.append(request)
        result = ({'challengeId': challenge_id, 'nonce': 'n' * 43, 'expiresAt': 1300}
                  if request.url.path.endswith('/challenge') else {'person': person})
        return httpx.Response(200, json=result)
    transport = SigV4Transport('https://identity.example.test/local', 'us-east-1',
        session=SimpleNamespace(get_credentials=lambda: Credentials('fake', 'fake', 'temporary')),
        client=httpx.Client(transport=httpx.MockTransport(respond)))
    identity = GoogleIdentityClient(transport, clock=lambda: 1000)
    challenge = identity.challenge()
    assert challenge.challenge_id == challenge_id
    assert 'x-prospectus-human-token' not in seen[0].headers
    assert seen[0].headers['authorization'].startswith('AWS4-HMAC-SHA256 ')
    assert identity.resolve('access-proof', 'id-proof', challenge_id) == person
    signed = seen[1].headers['authorization']
    assert 'x-prospectus-human-token' in signed and 'x-prospectus-identity-token' in signed
    assert seen[1].headers['x-prospectus-human-token'] == 'access-proof'
    assert seen[1].headers['x-prospectus-identity-token'] == 'id-proof'
    assert seen[1].read() == ('{"challengeId":"' + challenge_id + '"}').encode()
    assert identity.me('continued-access-proof') == person
    assert 'x-prospectus-identity-token' not in seen[2].headers
    assert 'n' * 43 not in repr(challenge)


@pytest.mark.parametrize('change', [dict(expiresAt=1000), dict(expiresAt=1301),
    dict(expiresAt=True), dict(nonce='short'), dict(nonce='bad nonce' * 8), dict(challengeId='bad')])
def test_malformed_or_expired_challenges_fail_before_authorize_redirect(change):
    result = dict(challengeId=str(uuid4()), nonce='n' * 43, expiresAt=1300) | change
    with pytest.raises(ProspectusUnavailable):
        GoogleIdentityClient(lambda *args: result, clock=lambda: 1000).challenge()


def test_unenrolled_me_is_not_enrollment_and_malformed_people_fail_closed():
    calls = []
    client = GoogleIdentityClient(lambda *args, **kwargs: calls.append(args) or {'person': None})
    assert client.me('access') is None
    assert calls == [('GET', '/v1/identities/me', 'access')]
    with pytest.raises(ProspectusUnavailable):
        client.resolve('access', 'id', str(uuid4()))


def test_proof_headers_are_restricted_to_the_resolution_endpoint():
    transport = SigV4Transport('https://identity.example.test', 'us-east-1', session=object())
    for path in ('/v1/accounts', '/v1/identities/me'):
        with pytest.raises(AccountAccessDenied):
            transport('GET', path, 'access', identity_token='id-proof')
    with pytest.raises(AccountAccessDenied):
        transport('POST', '/v1/identities/google/resolve', 'access')
    with pytest.raises(AccountAccessDenied):
        transport('POST', '/v1/identities/google/resolve', 'access', identity_token='id\r\ninjected')
