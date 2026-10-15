"""Bounded approved AWS storage trial; synthetic data, no providers or tenant authority.

Uses normal Endymion temporary credentials. Retains a tiny isolated proof namespace
instead of deleting history. This is storage/KMS evidence, not runtime IAM proof.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4
import botocore.session
from botocore.config import Config
from botocore.exceptions import ClientError
from decision_flywheel.batch_storage import StorageScope
from decision_flywheel.storage_payloads import S3Payloads
from decision_flywheel.s3_journal import S3Journal,JournalConflict,JournalUncertain
from decision_flywheel.s3_sessions import KMSSessionCipher


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('outputs',help='describe-stacks JSON file for the approved stack')
    args=parser.parse_args()
    document=json.loads(Path(args.outputs).read_text());stack=document['Stacks'][0]
    assert stack['StackName']=='cyclotron-console'
    outputs={row['OutputKey']:row['OutputValue'] for row in stack['Outputs']}
    session=botocore.session.get_session();session.set_config_variable('profile','cyclotron-production')
    options=Config(retries={'total_max_attempts':1},connect_timeout=5,read_timeout=15,signature_version='s3v4')
    sts=session.create_client('sts',region_name='us-east-1')
    assert sts.get_caller_identity()['Account']=='682849160744'
    raw=session.create_client('s3',region_name='us-east-1',config=options)
    calls={'get':0,'put':0}
    class Observed:
        meta=raw.meta;lose=False;head=None
        def get_object(self,**request):calls['get']+=1;return raw.get_object(**request)
        def put_object(self,**request):
            calls['put']+=1;result=raw.put_object(**request)
            if self.lose and request['Key']==self.head:
                self.lose=False;raise TimeoutError('synthetic response loss after actual AWS receipt')
            return result
    client=Observed();account=str(uuid4());scope=StorageScope(account,'console')
    payloads=S3Payloads(client,outputs['BooksBucketName'],'682849160744',scope,max_bytes=65536)
    def journal():return S3Journal(payloads,'console','cloud-proof',max_bytes=65536,max_commits=20,create_only_initialization=True)
    j=journal();first=j.initialize({'synthetic':True,'charged':0})
    second=j.commit(first,{'synthetic':True,'charged':1})
    try:j.commit(first,{'synthetic':True,'charged':0})
    except JournalConflict:pass
    else:raise AssertionError('stale ETag accepted')
    assert journal().read().value==second.value
    try:raw.put_object(Bucket=payloads.bucket,Key=j.head_key,Body=b'{}',ExpectedBucketOwner=payloads.owner)
    except ClientError as error:assert error.response['Error']['Code']=='AccessDenied'
    else:raise AssertionError('bucket policy allowed unconditional head replacement')
    client.head=j.head_key;client.lose=True
    try:j.commit(second,{'synthetic':True,'charged':1,'claimed':True})
    except JournalUncertain:pass
    else:raise AssertionError('lost receipt granted success')
    assert journal().read().value['claimed'] is True
    kms=session.create_client('kms',region_name='us-east-1',config=Config(retries={'total_max_attempts':1},connect_timeout=5,read_timeout=15))
    cipher=KMSSessionCipher(kms,outputs['SessionKeyArn'],application='cyclotron-console')
    reference=sha256(account.encode()).hexdigest();body=b'synthetic-session-proof-no-human-token'
    envelope=cipher.encrypt('session',reference,body)
    assert cipher.decrypt('session',reference,envelope)==body
    try:cipher.decrypt('session',sha256(b'wrong-proof-context').hexdigest(),envelope)
    except ClientError as error:assert error.response['Error']['Code']=='InvalidCiphertextException'
    else:raise AssertionError('KMS accepted wrong record context')
    print(json.dumps({'actualAwsStorage':'PASS','account':'682849160744','region':'us-east-1',
        'syntheticProofAccount':account,'staleCasDenied':True,'unconditionalHeadDenied':True,
        'lostReceiptUncertain':True,'kmsContextBound':True,'storageCalls':calls,
        'runtimeIamVerified':False,'providerCalls':0,'objectsDeleted':0}))

if __name__=='__main__':main()
