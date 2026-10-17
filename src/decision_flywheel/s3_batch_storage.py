"""S3-authoritative shared batches, atomic paid budget and fenced dispatch claims.

One explicitly bounded run ledger; immutable transaction values retain exact text
and all charged history. No SQLite connection or DynamoDB authority is used.
"""
from copy import deepcopy
import json
from .batch_storage import (BatchRecord,BatchConflict,BatchInProgress,BatchUncertain,
                            validate_request,validate_snapshot)
from .decision_cache import CacheMiss
from .journal import Journal,JournalConflict,JournalUncertain

EMPTY={'schema':'cyclotron-shared-batches/v1','batches':[],'attempts':[],'history':[]}

class S3BatchStorage:
    def __init__(self,journal,*,max_records,max_conflicts=8):
        if not isinstance(journal,Journal):raise ValueError('canonical journal required')
        if type(max_records) is not int or max_records<1 or type(max_conflicts) is not int or not 1<=max_conflicts<=100:
            raise ValueError('explicit record budget and bounded contention retries required')
        self.journal=journal;self.max_records=max_records;self.max_conflicts=max_conflicts
    def _check(self,value,max_records=None):
        if not isinstance(value,dict) or set(value)!=set(EMPTY):raise ValueError('complete shared batch state required')
        validate_snapshot(value)
        if sum(len(value[k]) for k in ('batches','attempts','history'))>(self.max_records if max_records is None else min(max_records,self.max_records)):
            raise ValueError('shared batch record budget exceeded; no history truncation')
        return value
    def initialize(self):
        self._check(self.journal.initialize(deepcopy(EMPTY)).value)
    def _read(self):
        record=self.journal.read();self._check(record.value)
        return record
    def _mutate(self,change):
        # Only pure domain changes are retried after an explicit CAS conflict.
        # Transport ambiguity propagates; a successful dispatch claim is NEVER
        # inferred from rereading a claimed row after an uncertain PUT.
        for _ in range(self.max_conflicts):
            expected=self._read();state=deepcopy(expected.value)
            changed,result=change(state)
            if not changed:return result
            self._check(state)
            try:self.journal.commit(expected,state)
            except JournalConflict:continue
            except JournalUncertain:raise BatchUncertain('S3 transaction uncertain; reconcile before provider dispatch') from None
            return result
        raise BatchConflict('S3 run contention limit reached; retry operation with its same owner')
    @staticmethod
    def _row(state,fingerprint):return next((r for r in state['batches'] if r[0]==fingerprint),None)
    @staticmethod
    def _record(row):return BatchRecord(row[0],row[1],row[2],row[3],row[4],row[5]) if row else None
    def get(self,fingerprint):return self._record(self._row(self._read().value,fingerprint))
    def requests(self):return len(self._read().value['attempts'])
    def reserve(self,fingerprint,maximum,owner,*,policy='reuse',retry_failed=False):
        validate_request(fingerprint,owner)
        if type(maximum) is not int or maximum<0 or policy not in ('reuse','cache_only','refresh'):
            raise ValueError('valid request ceiling and policy required')
        def change(state):
            row=self._row(state,fingerprint)
            prior=next((r for r in state['attempts'] if r[2]==owner),None)
            if prior and prior[1]!=fingerprint:raise BatchConflict('operation belongs to a different request')
            if row and row[4]==owner:return False,self._record(row)
            if prior:
                old=next((r for r in state['history'] if r[1]==fingerprint and r[5]==owner),None)
                if old:return False,BatchRecord(fingerprint,old[3],old[4],old[2],owner,True)
                raise BatchConflict('operation result requires reconciliation')
            if row and row[1]=='pending':raise BatchInProgress('shared request in progress; explicit reconciliation required')
            if policy=='cache_only' and (not row or row[1]!='complete'):raise CacheMiss('complete batch not cached')
            if row and row[1]=='complete' and policy!='refresh':return False,self._record(row)
            if row and row[1]!='complete' and not retry_failed and not (row[1]=='interrupted' and row[2]=='{"learning_abandoned":true}'):
                raise RuntimeError('explicit retry required for interrupted batch')
            if len(state['attempts'])>=maximum:raise RuntimeError('run decision request ceiling reached')
            if row:
                state['history'].append([len(state['history'])+1,fingerprint,row[3],row[1],row[2],row[4]])
                generation,revision=row[3]+1,row[6]+1
                state['batches'].remove(row)
            else:generation,revision=1,1
            state['attempts'].append([len(state['attempts'])+1,fingerprint,owner])
            row=[fingerprint,'pending',None,generation,owner,False,revision]
            state['batches'].append(row);state['batches'].sort(key=lambda r:r[0])
            return True,self._record(row)
        return self._mutate(change)
    def claim(self,fingerprint,owner):
        validate_request(fingerprint,owner)
        def change(state):
            row=self._row(state,fingerprint)
            if not row or row[4]!=owner or row[1]!='pending' or row[5]:return False,False
            row[5]=True;row[6]+=1
            return True,True
        return self._mutate(change)
    def settle(self,fingerprint,owner,status,payload=None):
        validate_request(fingerprint,owner)
        if status not in ('complete','failed','interrupted') or status=='complete' and payload is None:
            raise ValueError('valid settlement required')
        if payload is not None:json.loads(payload)
        def change(state):
            row=self._row(state,fingerprint)
            if row and row[4]==owner and row[1]==status and row[2]==payload:return False,self._record(row)
            if not row or row[4]!=owner or row[1]!='pending':raise BatchConflict('stale shared request owner')
            if status=='complete' and not row[5]:raise BatchConflict('completion requires dispatch claim')
            row[1]=status;row[2]=payload;row[6]+=1
            return True,self._record(row)
        return self._mutate(change)
    def operation_mark(self):return len(self._read().value['attempts'])
    def pending_operations(self):
        state=self._read().value;rows={r[0]:r for r in state['batches']}
        return [(ordinal,key,owner) for ordinal,key,owner in state['attempts'] if rows[key][1]=='pending' and rows[key][4]==owner]
    def abandon_learning(self,fingerprint,owner):
        validate_request(fingerprint,owner)
        def change(state):
            row=self._row(state,fingerprint)
            if not row or row[4]!=owner or row[1]!='pending':raise BatchConflict('stale shared request owner')
            if row[5]:
                raise BatchUncertain('claimed cloud request requires explicit provider reconciliation; never automatically repay')
            row[1]='interrupted';row[2]='{"learning_abandoned":true}';row[6]+=1
            return True,self._record(row)
        return self._mutate(change)
    def page(self,*,after=None,limit=100):
        if type(limit) is not int or not 1<=limit<=1000 or after is not None and not isinstance(after,str):
            raise ValueError('bounded page required')
        rows=[r for r in self._read().value['batches'] if r[0]>(after or '')][:limit+1]
        return [self._record(r) for r in rows[:limit]],rows[limit-1][0] if len(rows)>limit else None
    def export(self,*,max_records=10000):
        if type(max_records) is not int or max_records<1:raise ValueError('bounded export required')
        return deepcopy(self._check(self._read().value,max_records))
    def restore(self,snapshot):
        snapshot=deepcopy(snapshot);self._check(snapshot)
        def change(state):
            if state['attempts'] or state['batches']:raise BatchConflict('restore requires empty destination')
            state.update(deepcopy(snapshot));return True,None
        return self._mutate(change)
    def close(self):pass  # Injected SDK clients belong to the host.
