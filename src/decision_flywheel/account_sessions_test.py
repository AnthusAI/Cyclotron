"""RSA-signed synthetic OAuth specifications; no provider or credential access."""
import base64
import json
import time
from hashlib import sha256

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from .account_sessions import CognitoTokenVerifier, OAuthConfig, OAuthSessions, SessionRejected

ISSUER = 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_synthetic'
CLIENT = 'synthetic-console-client'


@pytest.fixture
def identity():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {'kid': 'synthetic', 'alg': 'RS256', 'use': 'sig'}
    verifier = CognitoTokenVerifier(ISSUER, CLIENT, jwks={'keys': [jwk]})
    now = int(time.time())
    def token(kind='access', **changes):
        claims = dict(iss=ISSUER, sub='provider-subject', exp=now + 1800,
            iat=now, token_use=kind, auth_time=now, origin_jti='synthetic-lineage')
        claims.update({'client_id': CLIENT} if kind == 'access' else {'aud': CLIENT, 'nonce': 'expected-nonce'})
        claims.update(changes)
        return jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'synthetic'})
    return verifier, token


def test_verifier_accepts_only_signed_access_tokens_for_the_pinned_issuer_and_client(identity):
    verifier, token = identity
    claims = verifier.access(token())
    assert claims['sub'] == 'provider-subject'
    for invalid in [token(iss='https://untrusted.example.test'), token(client_id='other'),
                    token(exp=int(time.time()) - 5), token(iat=int(time.time()) + 600), token('id'),
                    jwt.encode({'iss': ISSUER, 'sub': 'forged'}, key='', algorithm='none')]:
        with pytest.raises(SessionRejected):
            verifier.access(invalid)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({'iss': ISSUER, 'sub': 'forged', 'exp': int(time.time()) + 600,
        'iat': int(time.time()), 'client_id': CLIENT, 'token_use': 'access'}, other_key,
        algorithm='RS256', headers={'kid': 'synthetic'})
    with pytest.raises(SessionRejected):
        verifier.access(forged)


def test_equal_email_claims_do_not_make_different_subjects_the_same_identity(identity):
    verifier, token = identity
    first = verifier.access(token(sub='first-subject', email='same@example.test'))
    second = verifier.access(token(sub='second-subject', email='same@example.test'))
    sessions = OAuthSessions(verifier)
    _, a = sessions.create(token(sub='first-subject'), first)
    _, b = sessions.create(token(sub='second-subject'), second)
    assert a.subject != b.subject
    assert a.person_id is None and b.person_id is None


def test_access_and_id_tokens_must_agree_and_the_id_token_must_match_nonce(identity):
    verifier, token = identity
    assert verifier.token_pair(token(), token('id'), 'expected-nonce')['sub'] == 'provider-subject'
    for id_token in [token('id', nonce='wrong'), token('id', sub='another-person'), token('id', aud='other')]:
        with pytest.raises(SessionRejected):
            verifier.token_pair(token(), id_token, 'expected-nonce')


def test_enrollment_requires_independently_verified_matching_fresh_session_evidence(identity):
    verifier, token = identity
    assert verifier.token_pair(token(), token('id'), 'expected-nonce', require_session_binding=True)
    for changes in (dict(origin_jti='other'), dict(origin_jti=''), dict(origin_jti=None),
                    dict(auth_time=int(time.time()) - 301), dict(auth_time=True), dict(aud=[CLIENT])):
        with pytest.raises(SessionRejected):
            verifier.token_pair(token(), token('id', **changes), 'expected-nonce', require_session_binding=True)
    old = dict(auth_time=int(time.time()) - 301)
    with pytest.raises(SessionRejected):
        verifier.token_pair(token(**old), token('id', **old), 'expected-nonce', require_session_binding=True)


def test_central_nonce_and_expiry_are_bound_to_the_local_single_use_login(identity):
    from .google_identity import GoogleChallenge
    verifier, token = identity
    clock = [1000]
    sessions = OAuthSessions(verifier, clock=lambda: clock[0])
    challenge = GoogleChallenge('central-challenge', 'central-nonce', 1010)
    pending = sessions.begin(challenge)
    assert pending.nonce == 'central-nonce' and pending.challenge_id == 'central-challenge'
    assert pending.expires_at == 1010
    assert pending.nonce not in repr(pending) and pending.code_verifier not in repr(pending)
    _, session = sessions.create(token(), {'sub': 'subject', 'exp': 3000})
    assert session.access_token not in repr(session) and session.csrf_token not in repr(session)
    clock[0] = 1011
    with pytest.raises(SessionRejected):
        sessions.consume(pending.state, pending.browser_cookie)


def test_oauth_state_is_browser_bound_single_use_and_carries_pkce_and_nonce(identity):
    verifier, _ = identity
    sessions = OAuthSessions(verifier)
    pending = sessions.begin()
    assert len(pending.code_verifier) >= 43
    assert pending.code_challenge == base64.urlsafe_b64encode(sha256(pending.code_verifier.encode()).digest()).decode().rstrip('=')
    with pytest.raises(SessionRejected):
        sessions.consume(pending.state, 'another-browser')
    pending = sessions.begin()
    assert sessions.consume(pending.state, pending.browser_cookie) == pending
    with pytest.raises(SessionRejected):
        sessions.consume(pending.state, pending.browser_cookie)


def test_server_sessions_are_opaque_expiring_and_revocable(identity):
    verifier, token = identity
    clock = [1000]
    sessions = OAuthSessions(verifier, clock=lambda: clock[0], max_age=10)
    identifier, session = sessions.create(token(), {'sub': 'provider-subject', 'exp': 3000})
    assert token() not in identifier
    assert sessions.get(identifier) is session
    clock[0] = 1011
    assert sessions.get(identifier) is None
    identifier, _ = sessions.create(token(), {'sub': 'provider-subject', 'exp': 3000})
    sessions.revoke(identifier)
    assert sessions.get(identifier) is None


def test_pending_logins_expire_and_memory_is_bounded(identity):
    verifier, _ = identity
    clock = [1000]
    sessions = OAuthSessions(verifier, clock=lambda: clock[0], capacity=1)
    first = sessions.begin()
    with pytest.raises(SessionRejected):
        sessions.begin()
    clock[0] += 301
    with pytest.raises(SessionRejected):
        sessions.consume(first.state, first.browser_cookie)
    assert sessions.begin()


def test_production_oauth_configuration_requires_https_and_fixed_callback_origin():
    with pytest.raises(ValueError):
        OAuthConfig(ISSUER, CLIENT, 'https://synthetic.auth.us-east-1.amazoncognito.com', 'http://lab.example.test')
    config = OAuthConfig(ISSUER, CLIENT, 'https://synthetic.auth.us-east-1.amazoncognito.com', 'https://lab.example.test')
    assert config.callback_url == 'https://lab.example.test/auth/callback'
    assert config.cookie_name.startswith('__Host-')
