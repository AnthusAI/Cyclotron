"""Actual Python SigV4/queued guard -> pinned central role-pair harness, offline.

Run with accounts extras and pytest fixtures installed:
PYTHONPATH=src python scripts/verify_execution_delegation_contract.py CENTRAL_CHECKOUT
Synthetic RSA keys, credentials, S3 boundary and native disposable central files
only. The harness models Gateway caller context; this does NOT prove deployed
IAM/S3/Google or activate any role/policy. No central files are edited.
"""
import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
from uuid import uuid4
import httpx
import jwt
from botocore.credentials import Credentials
from cryptography.hazmat.primitives.asymmetric import rsa
from verify_google_identity_contract import Harness
from decision_flywheel.account_sessions import CognitoTokenVerifier
from decision_flywheel.google_identity import GoogleIdentityClient
from decision_flywheel.prospectus_accounts import AccountsClient,SigV4Transport,AccountAccessDenied
from decision_flywheel.queued_account_authorization import QueuedAccountAuthorization
from decision_flywheel.s3_sessions import S3OAuthSessions
from decision_flywheel.s3_sessions_test import Cipher
from decision_flywheel.s3_journal_test import ConditionalS3,journal


def verify(repository):
    now=int(time.time());issuer='https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Synthetic0';client='synthetic0'
    keys={purpose:rsa.generate_private_key(public_exponent=65537,key_size=2048) for purpose in ('access','id')}
    jwks={purpose:json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))|{'kid':purpose,'alg':'RS256','use':'sig'} for purpose,key in keys.items()}
    registration=dict(product='cyclotron',roleArn='arn:aws:iam::111111111111:role/Synthetic0',issuer=issuer,
        clientIds=[client],capabilities=['console:read','console:write'],enabled=True,
        googleFederation=dict(policy='google-only-unlinked-v1',reviewedPolicyId='synthetic-review-v1',clientId=client),
        executionDelegation=dict(roleArn='arn:aws:iam::111111111111:role/SyntheticWorker0',
            enrollmentRoleArn='arn:aws:iam::111111111111:role/Synthetic0',issuer=issuer,clientId=client,
            enrollmentPolicyRevision='synthetic-review-v1',reviewedPolicyId='synthetic-execution-review-v1'))
    calls=[];checks=0
    harness=Harness(repository)
    with tempfile.TemporaryDirectory(prefix='cyclotron-role-pair-') as root:
        configuration=dict(op='configure',nowSeconds=now,products=[registration],keys={issuer:jwks},filesystemRoot=root,durableAccounts=True)
        try:
            assert harness.call(configuration)=={'configured':True}
            def transport(role):
                def central(request):
                    assert 'x-prospectus-human-token' in request.headers.get('authorization','').lower() or request.url.path.endswith('/challenge')
                    assert request.headers.get('x-amz-security-token')=='synthetic-temporary'
                    calls.append((role,request.method,request.url.path))
                    event=dict(httpMethod=request.method,path=request.url.path,headers=dict(request.headers),body=request.content.decode(),
                        requestContext=dict(identity=dict(userArn=f'arn:aws:sts::111111111111:assumed-role/{role}/test',accountId='111111111111')))
                    result=harness.call(dict(op='request',event=event))
                    return httpx.Response(result['statusCode'],headers=result['headers'],content=result['body'])
                return SigV4Transport('https://synthetic-central.example.invalid','us-east-1',
                    session=SimpleNamespace(get_credentials=lambda:Credentials('synthetic','synthetic','synthetic-temporary')),
                    client=httpx.Client(transport=httpx.MockTransport(central)))
            bff=transport('Synthetic0');worker=transport('SyntheticWorker0')
            identity=GoogleIdentityClient(bff);challenge=identity.challenge()
            def token(purpose,**changes):
                claims=dict(iss=issuer,sub='synthetic-pool-sub',iat=now,exp=now+1800,auth_time=now,origin_jti='synthetic-family',jti=str(uuid4()),token_use=purpose)
                claims.update({'client_id':client} if purpose=='access' else {'aud':client,'nonce':challenge.nonce,
                    'identities':[dict(providerName='Google',providerType='Google',userId='synthetic-google-sub')]})
                return jwt.encode(claims|changes,keys[purpose],algorithm='RS256',headers={'kid':purpose})
            access=token('access');id_token=token('id')
            person=identity.resolve(access,id_token,challenge.challenge_id)
            account=AccountsClient(bff).create_account(access,'Synthetic role-pair account',str(uuid4()))
            verifier=CognitoTokenVerifier(issuer,client,jwks={'keys':list(jwks.values())})
            client_s3=ConditionalS3();cipher=Cipher()
            def store():return S3OAuthSessions(verifier,journal_for=lambda kind,ref:journal(client_s3,name=kind+'-'+ref),cipher=cipher)
            sessions=store();cookie,session=sessions.create(access,verifier.access(access));session.person_id=person['id'];sessions.save(session)
            binding=lambda account_id,run_id:'synthetic-fixed-binding'
            enrolling=QueuedAccountAuthorization(sessions,verifier,identity,AccountsClient(bff),binding)
            actor=asyncio.run(enrolling.capture(session,account['id'],str(uuid4()),binding(None,None)))
            executing=QueuedAccountAuthorization(store(),verifier,GoogleIdentityClient(worker),AccountsClient(worker),binding)
            asyncio.run(executing.guard(actor)());checks+=1
            assert all(role=='SyntheticWorker0' for role,method,path in calls[-2:])
            harness.restart(configuration)
            asyncio.run(executing.guard(actor)());checks+=1
            assert identity.me(access)['id']==person['id'];checks+=1
            # Forbidden worker enrollment/account management stays denied.
            for method,path,body in [('POST','/v1/identities/google/challenge',{}),('POST','/v1/identities/google/resolve',{'challengeId':challenge.challenge_id}),
                    ('GET','/v1/accounts',None),('POST','/v1/accounts',{'name':'forbidden'}),('GET',f"/v1/accounts/{account['id']}",None)]:
                try:worker(method,path,None if path.endswith('/challenge') else access,body,identity_token=id_token if path.endswith('/resolve') else None)
                except AccountAccessDenied:checks+=1
                else:raise AssertionError('worker route unexpectedly allowed')
            for evidence in (token('access',origin_jti='new-family'),token('access',client_id='wrong'),token('access',exp=now-1)):
                try:GoogleIdentityClient(worker).me(evidence)
                except AccountAccessDenied:checks+=1
                else:raise AssertionError('changed worker evidence unexpectedly allowed')
            unknown=QueuedAccountAuthorization(store(),verifier,GoogleIdentityClient(transport('SyntheticUnknown')),AccountsClient(worker),binding)
            try:asyncio.run(unknown.guard(actor)())
            except AccountAccessDenied:checks+=1
            else:raise AssertionError('unlisted execution role allowed')
            without_pair={**configuration,'products':[{key:value for key,value in registration.items() if key!='executionDelegation'}]}
            harness.restart(without_pair)
            try:asyncio.run(executing.guard(actor)())
            except AccountAccessDenied:checks+=1
            else:raise AssertionError('removed execution pair still authorized')
            harness.restart(configuration)
            asyncio.run(executing.guard(actor)());checks+=1
            before=len(calls);sessions.revoke(cookie)
            try:asyncio.run(executing.guard(actor)())
            except AccountAccessDenied:checks+=1
            else:raise AssertionError('logged out session continued')
            assert len(calls)==before
            print(json.dumps(dict(passed=checks,centralSource='e2bb8ffb2f4288989bab6153d0394afecf8439a9',
                nativeCentralStorage=True,pythonSigV4=True,separateRuntimePrincipals=True,
                deployedIamVerified=False,liveGoogleVerified=False)))
        finally:harness.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('prospectus',type=Path)
    verify(parser.parse_args().prospectus.resolve())
