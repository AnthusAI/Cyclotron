"""Bounded canonical S3 transactions; DynamoDB is never involved in authority.

A small conditional head publishes one immutable value+parent record. No SQL,
provider call or caller callback runs inside the CAS primitive. Uncertain writes
always raise; observing a claim later never grants dispatch permission.
"""
from dataclasses import dataclass
import json
import re
from uuid import uuid4,UUID
from .storage_payloads import S3Payloads,PayloadRef

class JournalConflict(RuntimeError): pass
class JournalUnavailable(RuntimeError): pass
class JournalUncertain(RuntimeError): pass
class JournalLimit(ValueError): pass

def encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def decode(content):
    def invalid(value):raise ValueError('non-JSON value')
    return json.loads(content,parse_constant=invalid)
def uuid(value):
    if not isinstance(value,str) or str(UUID(value))!=value or UUID(value).version!=4:
        raise ValueError('UUIDv4 required')
    return value

@dataclass(frozen=True)
class JournalRecord:
    namespace: str
    anchor: str
    revision: int
    commit_id: str
    etag: str
    reference: PayloadRef
    value: object

class S3Journal:
    def __init__(self,payloads,domain,name,*,max_bytes,max_commits):
        if not isinstance(payloads,S3Payloads):raise ValueError('injected scoped S3 payload adapter required')
        if any(not isinstance(v,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',v) for v in (domain,name)):
            raise ValueError('fixed domain and run namespace required')
        if type(max_bytes) is not int or not 1<=max_bytes<=payloads.max_bytes or type(max_commits) is not int or not 1<=max_commits<2**63:
            raise ValueError('explicit positive journal byte and commit budgets required')
        # Hidden SDK retries can turn a lost successful PUT into a later 412.
        # Pure mutation replay would then duplicate append-only records. Real
        # botocore clients must perform exactly one attempt; the domain retries
        # only explicit service conflicts. Test clients implement this contract.
        meta=getattr(payloads.client,'meta',None)
        if meta is not None and getattr(meta,'config',None) is not None:
            retries=meta.config.retries or {}
            if retries.get('total_max_attempts')!=1:
                raise ValueError('conditional S3 client requires total_max_attempts=1')
        self.payloads=payloads;self.max_bytes=max_bytes;self.max_commits=max_commits
        self.namespace=f'{payloads.scope.key}/{domain}/{name}'
        prefix=f'{payloads.scope.key}/coordination/{domain}/{name}'
        self.head_key=prefix+'/head.json';self.anchor_key=prefix+'/anchor.json'
    def _get(self,key,*,missing=False):
        stream=None
        try:
            response=self.payloads.client.get_object(Bucket=self.payloads.bucket,Key=key,ExpectedBucketOwner=self.payloads.owner)
            stream=response['Body'];length=response['ContentLength'];etag=response['ETag']
            if type(length) is not int or not 0<length<=8192 or not isinstance(etag,str) or not etag or len(etag)>1024:
                raise ValueError('invalid coordination object')
            content=stream.read(length+1)
            if len(content)!=length:raise ValueError('incomplete coordination object')
            return decode(content),etag
        except Exception as error:
            if missing and getattr(error,'response',{}).get('Error',{}).get('Code')=='NoSuchKey':return None
            raise JournalUnavailable('canonical S3 coordination unavailable; do not initialize or dispatch') from None
        finally:
            if stream is not None:stream.close()
    def _put(self,key,value,etag=None):
        try:
            response=self.payloads.client.put_object(Bucket=self.payloads.bucket,Key=key,Body=encode(value),
                ExpectedBucketOwner=self.payloads.owner,ContentType='application/json',
                **({'IfNoneMatch':'*'} if etag is None else {'IfMatch':etag}))
            result=response.get('ETag')
            if not isinstance(result,str) or not result:raise ValueError('missing committed ETag')
            return result
        except Exception as error:
            if getattr(error,'response',{}).get('Error',{}).get('Code') in ('PreconditionFailed','ConditionalRequestConflict','412','409'):
                raise JournalConflict('S3 head changed; read current authority before retry') from None
            raise JournalUncertain('S3 write outcome uncertain; no dispatch or automatic reset') from None
    def _anchor(self):
        result=self._get(self.anchor_key)
        value=result[0]
        try:
            if set(value)!={'schema','namespace','anchor'} or value['schema']!='cyclotron-s3-anchor/v1' or value['namespace']!=self.namespace:
                raise ValueError()
            return uuid(value['anchor'])
        except Exception:raise JournalUnavailable('invalid S3 namespace anchor') from None
    def _head(self,anchor,revision,commit_id,reference):
        return {'schema':'cyclotron-s3-head/v1','namespace':self.namespace,'anchor':anchor,
                'revision':revision,'commit':commit_id,'state':{'digest':reference.digest,'length':reference.length}}
    def _publish(self,anchor,revision,value,parent):
        commit_id=str(uuid4())
        transaction={'schema':'cyclotron-s3-transaction/v1','namespace':self.namespace,'anchor':anchor,
            'revision':revision,'commit':commit_id,'parent':parent,'value':value}
        content=encode(transaction)
        if len(content)>self.max_bytes or revision>self.max_commits:raise JournalLimit('canonical journal budget exceeded; no truncation')
        try:reference=self.payloads.put(content)
        except Exception:raise JournalUncertain('immutable S3 transaction publication uncertain; head not advanced') from None
        return self._head(anchor,revision,commit_id,reference),reference,commit_id
    def initialize(self,value):
        # Explicit new namespace only. The immutable anchor makes a missing head
        # after a crash/deletion unavailable rather than silently resetting money.
        existing=self._get(self.anchor_key,missing=True)
        if existing is not None:return self.read()
        if self._get(self.head_key,missing=True) is not None:
            raise JournalUnavailable('head exists without namespace anchor; operator reconciliation required')
        # Enforce size before creating a namespace that would need reconciliation.
        if len(encode(value))+1024>self.max_bytes:raise JournalLimit('canonical journal byte budget exceeded')
        anchor=str(uuid4())
        try:self._put(self.anchor_key,{'schema':'cyclotron-s3-anchor/v1','namespace':self.namespace,'anchor':anchor})
        except JournalConflict:return self.read()
        head,reference,commit_id=self._publish(anchor,1,value,None)
        etag=self._put(self.head_key,head)
        return JournalRecord(self.namespace,anchor,1,commit_id,etag,reference,decode(encode(value)))
    def read(self):
        anchor=self._anchor();head,etag=self._get(self.head_key)
        try:
            if (set(head)!={'schema','namespace','anchor','revision','commit','state'}
                    or head['schema']!='cyclotron-s3-head/v1' or head['namespace']!=self.namespace or head['anchor']!=anchor
                    or type(head['revision']) is not int or not 1<=head['revision']<=self.max_commits):raise ValueError()
            uuid(head['commit'])
            if set(head['state'])!={'digest','length'}:raise ValueError()
            ref=PayloadRef(self.payloads.scope,head['state']['digest'],head['state']['length'])
            if ref.length>self.max_bytes:raise ValueError()
            transaction=decode(self.payloads.get(ref))
            if (set(transaction)!={'schema','namespace','anchor','revision','commit','parent','value'}
                    or transaction['schema']!='cyclotron-s3-transaction/v1'
                    or any(transaction[k]!=head[k] for k in ('namespace','anchor','revision','commit'))):raise ValueError()
            parent=transaction['parent']
            if head['revision']==1:
                if parent is not None:raise ValueError()
            else:
                if not isinstance(parent,dict) or set(parent)!={'digest','length'}:raise ValueError()
                PayloadRef(self.payloads.scope,parent['digest'],parent['length'])
            return JournalRecord(self.namespace,anchor,head['revision'],head['commit'],etag,ref,transaction['value'])
        except Exception:raise JournalUnavailable('invalid or missing canonical S3 transaction; never use an index as fallback') from None
    def commit(self,expected,value):
        if not isinstance(expected,JournalRecord) or expected.namespace!=self.namespace or expected.reference.scope!=self.payloads.scope:
            raise PermissionError('S3 transaction scope refused')
        if self._anchor()!=expected.anchor:raise JournalConflict('namespace anchor changed')
        head,reference,commit_id=self._publish(expected.anchor,expected.revision+1,value,
            {'digest':expected.reference.digest,'length':expected.reference.length})
        etag=self._put(self.head_key,head,expected.etag)
        return JournalRecord(self.namespace,expected.anchor,expected.revision+1,commit_id,etag,reference,decode(encode(value)))
