"""Canonical S3 evaluation clock/trace and explicit whole-plan ownership."""
from copy import deepcopy
import json
from .s3_journal import S3Journal,JournalConflict,JournalUncertain,uuid,encode,decode
from .s3_runtime_storage import RuntimeOwnershipError


def empty():return {'schema':'cyclotron-s3-evaluation/v1','owner':None,'fence':0,'clocks':[],'trace':[],'watermark':0}

class S3EvaluationStorage:
    def __init__(self,journal,*,max_records):
        if not isinstance(journal,S3Journal) or type(max_records) is not int or max_records<1:raise ValueError('canonical evaluation journal and record budget required')
        self.journal=journal;self.max_records=max_records;self.authority=None;self.poisoned=False
    def _validate(self,value):
        if not isinstance(value,dict) or set(value)!=set(empty()) or value['schema']!='cyclotron-s3-evaluation/v1':raise ValueError('invalid S3 evaluation')
        if type(value['fence']) is not int or value['fence']<0 or type(value['watermark']) is not int or not 0<=value['watermark']<2**63:raise ValueError('invalid evaluation watermark/fence')
        if value['owner'] is not None:uuid(value['owner'])
        if not isinstance(value['clocks'],list) or not isinstance(value['trace'],list) or len(value['clocks'])+len(value['trace'])>self.max_records:raise ValueError('evaluation record budget exceeded')
        for key in ('clocks','trace'):
            ids,identities=set(),set()
            for row in value[key]:
                if not isinstance(row,list) or len(row)!=3 or type(row[0]) is not int or not 1<=row[0]<2**63 or row[0] in ids or any(not isinstance(x,str) for x in row[1:]) or row[1] in identities:raise ValueError('invalid evaluation records')
                ids.add(row[0]);identities.add(row[1])
                if key=='trace':
                    decode(row[2])
                    if row[0]>value['watermark']:raise ValueError('trace exceeds watermark')
            if [r[0] for r in value[key]]!=sorted(ids):raise ValueError('evaluation ordinals must be ordered')
    def initialize(self):self._validate(self.journal.initialize(empty()).value)
    def _record(self,*,owned=True,recovery=False):
        if self.poisoned and not recovery:raise RuntimeOwnershipError('uncertain evaluation write; reopen/reconcile before continuing')
        record=self.journal.read();self._validate(record.value)
        if owned and (self.authority is None or (record.value['owner'],record.value['fence'])!=self.authority):raise RuntimeOwnershipError('evaluation ownership unavailable')
        return record
    def _change(self,fn,*,owned=True,recovery=False):
        for _ in range(8):
            record=self._record(owned=owned,recovery=recovery);value=deepcopy(record.value);result=fn(value);self._validate(value)
            if value==record.value:return result
            try:self.journal.commit(record,value)
            except JournalConflict:continue
            except JournalUncertain:self.poisoned=True;raise
            return result
        raise RuntimeOwnershipError('evaluation contention limit reached')
    def acquire(self,owner):
        uuid(owner)
        def apply(value):
            if self.authority is not None or value['owner'] is not None:raise RuntimeOwnershipError('evaluation already owned; no timeout takeover')
            value['owner']=owner;value['fence']+=1;return owner,value['fence']
        self.authority=self._change(apply,owned=False)
    def close(self):
        if self.authority is None:return
        try:self._change(lambda value:value.__setitem__('owner',None),recovery=True)
        finally:self.authority=None
    def bind_clock(self,plan,proposed_clock):
        if not isinstance(plan,str) or not isinstance(proposed_clock,str):raise ValueError('plan and clock text required')
        def apply(value):
            row=next((r for r in value['clocks'] if r[1]==plan),None)
            if row:return row[2]
            value['clocks'].append([max((r[0] for r in value['clocks']),default=0)+1,plan,proposed_clock]);return proposed_clock
        return self._change(apply)
    def append_trace(self,identity,payload):
        if not isinstance(identity,str) or not isinstance(payload,str):raise ValueError('trace identity and raw payload text required')
        decode(payload)
        def apply(value):
            value['watermark']+=1
            old=next((r for r in value['trace'] if r[1]==identity),None)
            if old:return old[0],old[2]
            identifier=value['watermark'];value['trace'].append([identifier,identity,payload]);return identifier,payload
        return self._change(apply)
    def events(self,*,after=0,limit=100):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=1000:raise ValueError('bounded trace cursor required')
        return [(i,p) for i,key,p in self._record().value['trace'] if i>after][:limit]
    def export_storage(self,*,max_records=10000,max_bytes=64000000):
        value=self._record().value
        result={'schema':'cyclotron-evaluation/v1','tables':{
            'execution':[{'_rowid':i,'plan':key,'clock':clock} for i,key,clock in value['clocks']],
            'trace':[{'_rowid':i,'id':i,'key':key,'payload':payload} for i,key,payload in value['trace']]},'metadata':{'sequences':{'trace':value['watermark']}}}
        if type(max_records) is not int or max_records<1 or len(value['clocks'])+len(value['trace'])>max_records or type(max_bytes) is not int or max_bytes<1 or len(encode(result))>max_bytes:raise ValueError('evaluation export budget exceeded')
        return result
    def restore_storage(self,snapshot,*,max_records=10000,max_bytes=64000000):
        if type(max_records) is not int or max_records<1 or type(max_bytes) is not int or max_bytes<1 or len(encode(snapshot))>max_bytes:raise ValueError('evaluation import budget exceeded')
        snapshot=deepcopy(snapshot)
        if (not isinstance(snapshot,dict) or set(snapshot)!={'schema','tables','metadata'} or snapshot['schema']!='cyclotron-evaluation/v1'
                or not isinstance(snapshot['tables'],dict) or set(snapshot['tables'])!={'execution','trace'}
                or not isinstance(snapshot['metadata'],dict) or set(snapshot['metadata'])!={'sequences'}
                or not isinstance(snapshot['metadata']['sequences'],dict) or set(snapshot['metadata']['sequences'])!={'trace'}):raise ValueError('invalid evaluation snapshot')
        for domain,columns in [('execution',{'_rowid','plan','clock'}),('trace',{'_rowid','id','key','payload'})]:
            if not isinstance(snapshot['tables'][domain],list) or any(not isinstance(r,dict) or set(r)!=columns for r in snapshot['tables'][domain]):raise ValueError('invalid evaluation import records')
        if any(r['id']!=r['_rowid'] for r in snapshot['tables']['trace']):raise ValueError('invalid trace ordinal')
        proposed=empty();proposed['clocks']=[[r['_rowid'],r['plan'],r['clock']] for r in snapshot['tables']['execution']]
        proposed['trace']=[[r['id'],r['key'],r['payload']] for r in snapshot['tables']['trace']]
        proposed['watermark']=snapshot['metadata']['sequences']['trace'];self._validate(proposed)
        if len(proposed['clocks'])+len(proposed['trace'])>max_records:raise ValueError('evaluation import record budget exceeded')
        def apply(value):
            if value['clocks'] or value['trace'] or value['watermark']:raise ValueError('evaluation restore requires unused empty destination')
            for field in ('clocks','trace','watermark'):value[field]=proposed[field]
        self._change(apply)
