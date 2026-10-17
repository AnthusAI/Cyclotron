"""Durable encrypted sessions retain one-use OAuth and revocation across workers."""
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .account_sessions import SessionRejected
from .s3_journal_test import ConditionalS3, journal


class Cipher:
    def __init__(self):self.key=AESGCM.generate_key(256)
    def encrypt(self,kind,reference,content):
        import base64
        nonce=os.urandom(12)
        return {'nonce':base64.b64encode(nonce).decode(),'body':base64.b64encode(AESGCM(self.key).encrypt(nonce,content,(kind+reference).encode())).decode()}
    def decrypt(self,kind,reference,envelope):
        import base64
        return AESGCM(self.key).decrypt(base64.b64decode(envelope['nonce']),base64.b64decode(envelope['body']),(kind+reference).encode())


def sessions(client=None,cipher=None,clock=None):
    from .s3_sessions import S3OAuthSessions
    client=client or ConditionalS3();cipher=cipher or Cipher()
    return S3OAuthSessions(None,journal_for=lambda kind,ref:journal(client,name=kind+'-'+ref),cipher=cipher,
        clock=clock or (lambda:1000),capacity=5,max_age=100),client,cipher


def test_oauth_pending_is_encrypted_and_consumed_once_across_processes():
    s,client,cipher=sessions();pending=s.begin()
    fresh,_,_=sessions(client,cipher)
    assert fresh.consume(pending.state,pending.browser_cookie)==pending
    with pytest.raises(SessionRejected):s.consume(pending.state,pending.browser_cookie)
    raw=b''.join(value[0] for value in client.objects.values())
    assert pending.state.encode() not in raw and pending.code_verifier.encode() not in raw and pending.browser_cookie.encode() not in raw


def test_session_person_selection_persist_and_stale_writer_cannot_resurrect_logout():
    s,client,cipher=sessions();identifier,session=s.create('synthetic-access-token',{'sub':'subject','exp':1100})
    session.person_id='person';session.account_id='account';s.save(session)
    fresh,_,_=sessions(client,cipher);restored=fresh.get(identifier)
    assert restored.person_id=='person' and restored.account_id=='account'
    assert fresh.get_ref(restored.session_ref).subject=='subject'
    assert b'synthetic-access-token' not in b''.join(value[0] for value in client.objects.values())
    s.revoke(identifier);restored.account_id='other'
    with pytest.raises(SessionRejected):fresh.save(restored)
    assert fresh.get(identifier) is None


def test_session_expiry_quota_and_wrong_browser_proof_fail_closed():
    clock=[1000];s,client,cipher=sessions(clock=lambda:clock[0])
    pending=s.begin()
    with pytest.raises(SessionRejected):s.consume(pending.state,'wrong')
    with pytest.raises(SessionRejected):s.consume(pending.state,pending.browser_cookie)
    identifier,_=s.create('access',{'sub':'subject','exp':1010})
    clock[0]=1011
    assert s.get(identifier) is None
    clock[0]=1400
    for _ in range(5):s.begin()
    with pytest.raises(SessionRejected):s.begin()


def test_uncertain_consume_never_grants_oauth_exchange_permission():
    s,client,cipher=sessions();pending=s.begin()
    target=s._journal('pending',s.reference(pending.state)).head_key
    client.lose=lambda key:key==target
    with pytest.raises(SessionRejected):s.consume(pending.state,pending.browser_cookie)
    with pytest.raises(SessionRejected):sessions(client,cipher)[0].consume(pending.state,pending.browser_cookie)


def test_session_updates_cannot_replace_person_or_original_token_and_old_selection_conflicts():
    s,client,cipher=sessions();identifier,current=s.create('access',{'sub':'subject','exp':1100})
    current.person_id='person';s.save(current)
    stale=s.get(identifier);new=s.get(identifier)
    new.account_id='beta';s.save(new)
    stale.account_id='alpha'
    with pytest.raises(SessionRejected):s.save(stale)
    new.person_id='different'
    with pytest.raises(SessionRejected):s.save(new)
    new=s.get(identifier);new.access_token='replacement'
    with pytest.raises(SessionRejected):s.save(new)
    assert s.get(identifier).account_id=='beta'


def test_kms_envelope_binds_exact_key_application_kind_and_session_reference():
    import base64

    from .s3_sessions import KMSSessionCipher
    key_arn='arn:aws:kms:us-east-1:111111111111:key/11111111-1111-4111-8111-111111111111'
    class KMS:
        def __init__(self):self.calls=[];self.context=None;self.key=AESGCM.generate_key(256)
        def generate_data_key(self,**kwargs):
            self.calls.append(kwargs);self.context=kwargs['EncryptionContext']
            return {'KeyId':key_arn,'Plaintext':self.key,'CiphertextBlob':b'synthetic-wrapped-key'}
        def decrypt(self,**kwargs):
            self.calls.append(kwargs)
            assert kwargs['EncryptionContext']==self.context
            assert kwargs['KeyId']==key_arn
            return {'KeyId':key_arn,'Plaintext':self.key}
    kms=KMS();cipher=KMSSessionCipher(kms,key_arn,application='cyclotron-console')
    ref='a'*64;envelope=cipher.encrypt('session',ref,b'synthetic-jwt')
    assert cipher.decrypt('session',ref,envelope)==b'synthetic-jwt'
    assert b'synthetic-jwt' not in base64.b64decode(envelope['body'])
    for kind,reference in [('pending',ref),('session','b'*64)]:
        with pytest.raises(Exception):cipher.decrypt(kind,reference,envelope)
    damaged={**envelope,'body':base64.b64encode(b'bad').decode()}
    with pytest.raises(Exception):cipher.decrypt('session',ref,damaged)
    assert kms.calls[0]['EncryptionContext']['purpose']=='oauth-session'


def test_missing_session_is_signed_out_but_corrupt_or_unavailable_canonical_state_is_not():
    from .s3_sessions import SessionStoreUnavailable
    s,client,_=sessions()
    assert s.get('absent') is None
    cookie,current=s.create('access',{'sub':'subject','exp':1100})
    key=s._journal('session',current.session_ref).head_key
    client.objects.pop(key)
    with pytest.raises(SessionStoreUnavailable):s.get(cookie)
