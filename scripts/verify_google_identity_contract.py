"""Exercise the real Python BFF against Prospectus's isolated identity factory.

Usage: PYTHONPATH=src python scripts/verify_google_identity_contract.py PATH_TO_PROSPECTUS
Requires the optional accounts/web Python dependencies, Node and that checkout's
locked tsx installation. Uses synthetic RSA keys, memory storage, mock HTTP and a
JSON-lines child only. Gateway IAM context is a fixture, not deployed IAM proof.
"""
import json
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from botocore.credentials import Credentials
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import jwt

from decision_flywheel.account_routes import HostedAccounts
from decision_flywheel.account_sessions import CognitoTokenVerifier, OAuthConfig
from decision_flywheel.google_identity import GoogleIdentityClient
from decision_flywheel.prospectus_accounts import AccountAccessDenied, AccountsClient, AccountWorkspace, SigV4Transport
from decision_flywheel.web_api import create_app
from decision_flywheel.web_store import WebStore


class Harness:
    def __init__(self, repository):
        self.child = subprocess.Popen([shutil.which('node') or 'node', '--import', 'tsx',
            'accounts/testing/google-harness.ts'], cwd=repository,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def call(self, message):
        self.child.stdin.write(json.dumps(message) + '\n')
        self.child.stdin.flush()
        if not select.select([self.child.stdout], [], [], 15)[0]:
            raise RuntimeError('Synthetic identity harness did not respond')
        response = json.loads(self.child.stdout.readline())
        if 'harnessError' in response:
            raise RuntimeError('Synthetic identity harness rejected input')
        return response

    def close(self):
        self.child.terminate()
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()


def verify(repository):
    now = int(time.time())
    products, private_keys, public_keys = [], {}, {}
    for index, product in enumerate(('cyclotron', 'papyrus')):
        issuer = f'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Synthetic{index}'
        client = f'synthetic{index}'
        products.append(dict(product=product, roleArn=f'arn:aws:iam::111111111111:role/Synthetic{index}',
            issuer=issuer, clientIds=[client], capabilities=['console:read', 'console:write'], enabled=True,
            googleFederation=dict(policy='google-only-unlinked-v1', reviewedPolicyId='synthetic-review-v1', clientId=client)))
        public_keys[issuer] = {}
        for purpose in ('access', 'id'):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            private_keys[index, purpose] = key
            public_keys[issuer][purpose] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {
                'kid': purpose, 'alg': 'RS256', 'use': 'sig'}

    def pair(index, nonce, *, access_changes=None, id_changes=None):
        product = products[index]
        common = dict(iss=product['issuer'], sub=f'different-pool-subject-{index}', auth_time=now,
            origin_jti=f'synthetic-family-{index}', iat=now, exp=now + 1800)
        access = common | dict(token_use='access', client_id=product['clientIds'][0], jti=str(uuid4())) | (access_changes or {})
        identity = common | dict(token_use='id', aud=product['clientIds'][0], jti=str(uuid4()), nonce=nonce,
            email='irrelevant@example.invalid', identities=[dict(providerName='Google', providerType='Google',
            userId='same-global-google-subject')]) | (id_changes or {})
        return {purpose: jwt.encode(claims, private_keys[index, purpose], algorithm='RS256', headers={'kid': purpose})
            for purpose, claims in (('access', access), ('id', identity))}

    def event(index, method, path, headers, body):
        return dict(httpMethod=method, path=path, headers=dict(headers), body=body,
            requestContext=dict(identity=dict(userArn=f'arn:aws:sts::111111111111:assumed-role/Synthetic{index}/test',
                accountId='111111111111')))

    harness = Harness(repository)
    try:
        assert harness.call(dict(op='configure', nowSeconds=now, products=products, keys=public_keys)) == {'configured': True}
        with tempfile.TemporaryDirectory(prefix='cyclotron-google-contract-') as directory:
            accounts, stores, resolutions = {}, {}, []
            def bff(index):
                product = products[index]
                state = {}
                def central(request):
                    request_event = event(index, request.method, request.url.path, request.headers, request.content.decode())
                    if request.url.path.endswith('/resolve'):
                        resolutions.append(request_event)
                    result = harness.call(dict(op='request', event=request_event))
                    return httpx.Response(result['statusCode'], headers=result['headers'], content=result['body'])
                identity = GoogleIdentityClient(SigV4Transport('https://synthetic-identity.example.invalid', 'us-east-1',
                    session=SimpleNamespace(get_credentials=lambda: Credentials('synthetic', 'fake', 'temporary')),
                    client=httpx.Client(transport=httpx.MockTransport(central))))
                def account_transport(method, path, access_token, body=None, key=None):
                    person = identity.me(access_token)
                    if not person:
                        raise AccountAccessDenied('synthetic account membership missing')
                    summary = accounts.get(person['id'])
                    if method == 'GET':
                        return {'person': person, 'accounts': [summary | {'entitlement': summary['entitlement'] | {
                            'product': product['product']}}] if summary else []}
                    if path == '/v1/accounts':
                        account_id = str(uuid4())
                        summary = dict(id=account_id, name=body['name'], role='owner', entitlement=dict(accountId=account_id,
                            product=product['product'], status='active', capabilities=['console:read', 'console:write']))
                        accounts[person['id']] = summary
                        stores[account_id] = WebStore(Path(directory) / (account_id + '.sqlite'))
                        stores[account_id].create_run('Shared synthetic workspace', 'recorded', {})
                        return {'account': summary}
                    if not summary or path != f'/v1/accounts/{summary["id"]}/authorize':
                        raise AccountAccessDenied('synthetic account membership missing')
                    return dict(allowed=True, accountId=summary['id'], personId=person['id'], product=product['product'],
                        role='owner', operation=body['operation'], revision=1)
                def exchange(request):
                    proof = pair(index, state['nonce'], access_changes=state.get('access_changes'), id_changes=state.get('id_changes'))
                    return httpx.Response(200, json={'access_token': proof['access'], 'id_token': proof['id']})
                verifier = CognitoTokenVerifier(product['issuer'], product['clientIds'][0], jwks={'keys': list(public_keys[product['issuer']].values())})
                config = OAuthConfig(product['issuer'], product['clientIds'][0], 'https://synthetic.auth.example.invalid',
                    f'https://synthetic{index}.example.invalid')
                gateway = HostedAccounts(config, AccountsClient(account_transport, product=product['product']),
                    lambda identifier: AccountWorkspace(stores[identifier]), verifier=verifier, google_identity=identity,
                    token_client=httpx.AsyncClient(transport=httpx.MockTransport(exchange)))
                app = create_app(None, account_access=gateway.access)
                gateway.install(app)
                return TestClient(app, base_url=config.public_origin), gateway, state

            def login(client, state, *, expected=303):
                redirect = client.get('/auth/login', follow_redirects=False)
                assert redirect.status_code == 303
                query = parse_qs(urlsplit(redirect.headers['location']).query)
                state['nonce'] = query['nonce'][0]
                callback = dict(state=query['state'][0], code='synthetic-code')
                assert client.get('/auth/callback', params=callback, follow_redirects=False).status_code == expected
                return callback

            first, first_gateway, first_state = bff(0)
            second, second_gateway, second_state = bff(1)
            with first, second:
                callback = login(first, first_state)
                first_session = first.get('/api/session').json()
                assert first_session['personId'] and first_session['accounts'] == []
                headers = {'Origin': first_gateway.config.public_origin, 'X-CSRF-Token': first_session['csrfToken'],
                    'Idempotency-Key': str(uuid4())}
                created = first.post('/api/accounts', json={'name': 'Synthetic shared team'}, headers=headers)
                assert created.status_code == 201
                account_id = created.json()['selectedAccountId']
                login(second, second_state)
                second_session = second.get('/api/session').json()
                assert first_session['personId'] == second_session['personId']
                assert second_session['accounts'][0]['id'] == account_id
                headers = {'Origin': second_gateway.config.public_origin, 'X-CSRF-Token': second_session['csrfToken']}
                assert second.post('/api/accounts/select', json={'accountId': account_id}, headers=headers).status_code == 200
                assert second.post('/graphql', params={'account': account_id}, json={'query': '{runs{name}}'}).json()['data']['runs'][0]['name'] == 'Shared synthetic workspace'
                assert first.get('/auth/callback', params=callback).status_code == 400
                assert len(resolutions) == 2  # state replay did not reach central
                assert harness.call(dict(op='request', event=resolutions[0]))['statusCode'] == 200
                changed = dict(resolutions[0], headers=dict(resolutions[0]['headers']))
                changed['headers']['x-prospectus-identity-token'] = pair(0, first_state['nonce'])['id']
                assert harness.call(dict(op='request', event=changed))['statusCode'] == 409
                session = second_gateway.sessions.get(second.cookies.get(second_gateway.config.cookie_name))
                session.access_token = pair(1, second_state['nonce'])['access']
                assert second.get('/api/session').json()['personId'] == first_session['personId']
                session.access_token = pair(1, second_state['nonce'], access_changes={'origin_jti': 'new-login-family'})['access']
                assert second.get('/api/session').json()['authenticated'] is False
                assert len(resolutions) == 2  # continuation never enrolls a new family

            # New browser sessions cannot bypass proof validation or gain a
            # central association from a malformed/ambiguous provider profile.
            rejected = 0
            for changes in (dict(aud='wrong-client'), dict(iss=products[1]['issuer']), dict(origin_jti='other-family'),
                dict(nonce='wrong-nonce'), dict(identities=[dict(providerName='Google', providerType='Google', userId='a'),
                    dict(providerName='Google', providerType='Google', userId='b')])):
                client, _, state = bff(0)
                state['id_changes'] = changes
                with client:
                    login(client, state, expected=400)
                    assert client.get('/api/session').json()['authenticated'] is False
                rejected += 1
            print(json.dumps(dict(result='PASS', pools=2, canonicalPeople=1, syntheticAccountContinuity=True,
                invalidProofCases=rejected, localStateReplay='denied', centralExactRetry='idempotent',
                centralChangedProof='409', continuationFamily='enforced', deployedIamVerified=False, liveGoogleVerified=False)))
    finally:
        harness.close()


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Pass the Prospectus checkout path; this script makes no cloud calls.')
    verify(Path(sys.argv[1]).resolve())
