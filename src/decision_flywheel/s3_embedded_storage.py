"""S3 embedded root and complete core composition, with no local-file fallback.

Root source-cursor/derived-record transactions publish one CAS. A failed/unknown
commit poisons that handle rather than continuing with potentially stale memory.
All execution ownership is explicit, fenced, and has no time-based takeover.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4
from .journal import Journal,commit_with_contract,uuid
from .storage_errors import StorageMissing,StorageLimit
from .s3_runtime_storage import RuntimeOwnershipError,S3RuntimeStores
from .s3_batch_storage import S3BatchStorage


def empty():
    return {'schema':'cyclotron-s3-embedded/v1','owner':None,'fence':0,'data':{
        'meta':{},'items':{},'decisions':[],'reviews':[],'events':[],'learning':[],'usage':[],'shares':[]}}

class S3EmbeddedStorage:
    def __init__(self,journal,*,max_records,cache_owned=False):
        if not isinstance(journal,Journal) or type(max_records) is not int or max_records<1:raise ValueError('scoped journal and record budget required')
        self.journal=journal;self.max_records=max_records;self.authority=None;self.poisoned=False;self.cache_owned=cache_owned;self._cached=None
        self._transaction=ContextVar('s3_embedded_transaction',default=None)
    def _validate(self,value):
        if not isinstance(value,dict) or set(value)!=set(empty()) or value['schema']!='cyclotron-s3-embedded/v1':raise ValueError('invalid canonical embedded root')
        if type(value['fence']) is not int or value['fence']<0:raise ValueError('invalid root execution fence')
        if value['owner'] is not None:uuid(value['owner'])
        data=value['data']
        if not isinstance(data,dict) or set(data)!=set(empty()['data']):raise ValueError('complete embedded domains required')
        if any(not isinstance(data[k],dict if k in ('meta','items') else list) for k in data):raise ValueError('invalid embedded records')
        if sum(map(len,data.values()))>self.max_records:raise StorageLimit('embedded record budget exceeded; no truncation')
    def initialize(self):self._validate(self.journal.initialize(empty()).value)
    def _owned(self,value):
        if self.authority is None or (value['owner'],value['fence'])!=self.authority:raise RuntimeOwnershipError('embedded execution ownership unavailable')
    def acquire(self,owner):
        uuid(owner)
        record=self.journal.read();self._validate(record.value)
        if self.authority is not None or record.value['owner'] is not None:raise RuntimeOwnershipError('embedded root already owned; no timeout takeover')
        value=deepcopy(record.value);value['owner']=owner;value['fence']+=1
        self._cached=self.journal.commit(record,value);self.authority=(owner,value['fence'])
    def release(self):
        if self.authority is None:return
        try:
            record=self.journal.read();self._validate(record.value);self._owned(record.value)
            value=deepcopy(record.value);value['owner']=None
            self.journal.commit(record,value)
        finally:self.authority=None;self._cached=None
    def close(self):pass  # The enclosing execution lease releases ownership.
    def _record(self):
        if self.poisoned:raise RuntimeOwnershipError('uncertain root transaction; reopen/reconcile before continuing')
        record=self._cached if self.cache_owned and self._cached is not None else self.journal.read()
        self._validate(record.value);self._owned(record.value)
        return record
    def refresh(self):
        self._cached=None
        if self.poisoned:raise RuntimeOwnershipError('uncertain root transaction; reopen/reconcile before continuing')
        record=self.journal.read();self._validate(record.value);self._owned(record.value);self._cached=record
    def _poison(self):self.poisoned=True;self._cached=None
    def _data(self):
        value=self._transaction.get()
        return value['data'] if value is not None else self._record().value['data']
    @contextmanager
    def transaction(self):
        if self._transaction.get() is not None:raise RuntimeError('nested embedded transaction')
        record=self._record();value=deepcopy(record.value);token=self._transaction.set(value)
        try:
            yield self
            self._validate(value)
            if value!=record.value:
                self._cached=commit_with_contract(self.journal,record,value,invalidate=self._poison,invalidate_on_conflict=True)
        finally:self._transaction.reset(token)
    def _write(self,fn):
        if self._transaction.get() is not None:return fn(self._data())
        with self.transaction():return fn(self._data())
    def meta(self,key):
        data=self._data()['meta'];return (data[key],) if key in data else None
    def insert_meta(self,key,payload,*,ignore=False):
        def apply(data):
            if key in data['meta']:
                if ignore:return
                raise ValueError('duplicate root metadata')
            data['meta'][key]=payload
        self._write(apply)
    def put_meta(self,key,payload):self._write(lambda data:data['meta'].__setitem__(key,payload))
    def put_item(self,identifier,payload,fingerprint):self._write(lambda data:data['items'].__setitem__(identifier,[payload,fingerprint]))
    def items(self):return [(key,row[0]) for key,row in self._data()['items'].items()]
    def item_values(self,identifier):
        row=self._data()['items'].get(identifier);return row[0] if row else None
    def insert_decision(self,row):
        def apply(data):
            if any(r[0]==row[0] for r in data['decisions']):raise ValueError('duplicate decision identity')
            data['decisions'].append(list(row))
        self._write(apply)
    def set_decision_state(self,identifier,state,*,unless_superseded=False):
        def apply(data):
            for row in data['decisions']:
                if row[0]==identifier and (not unless_superseded or row[3]!='superseded'):row[3]=state
        self._write(apply)
    def decision_count(self):return len(self._data()['decisions'])
    def decision_rows(self):return [(r[0],r[3],r[4]) for r in self._data()['decisions']]
    def decision_ids(self):return [(r[0],) for r in self._data()['decisions']]
    def decision_items(self):return [(r[0],r[1]) for r in self._data()['decisions']]
    def decision_payloads(self):return [(r[4],) for r in self._data()['decisions']]
    def window_decisions(self,limit,offset):return [(r[0],r[4]) for r in self._data()['decisions'][offset:offset+limit]]
    def decision(self,identifier):return next(((r[4],) for r in self._data()['decisions'] if r[0]==identifier),None)
    def decision_fingerprint(self,identifier):
        row=next((r for r in self._data()['decisions'] if r[0]==identifier),None)
        if row is None:raise StorageMissing(f'unknown decision {identifier}')
        return row[2]
    def latest_decision(self,item_id):return next(((r[3],r[4]) for r in reversed(self._data()['decisions']) if r[1]==item_id and r[3]!='superseded'),None)
    def latest_id(self,item_id=None):return next(((r[0],) for r in reversed(self._data()['decisions']) if item_id is None or r[1]==item_id),None)
    def insert_review(self,identifier,decision_id,classifier,payload):
        def apply(data):
            if any(r[1]==identifier for r in data['reviews']):raise ValueError('duplicate review identity')
            data['reviews'].append([len(data['reviews'])+1,identifier,decision_id,classifier,payload])
        self._write(apply)
    def review(self,identifier):return next(((r[4],) for r in self._data()['reviews'] if r[1]==identifier),None)
    def reviews(self,decision_id,classifier):return [(r[4],) for r in self._data()['reviews'] if (r[2],r[3])==(decision_id,classifier)]
    def last_review(self,decision_id,classifier):return next(((r[4],) for r in reversed(self._data()['reviews']) if (r[2],r[3])==(decision_id,classifier)),None)
    def append_event(self,payload):
        def apply(data):
            identifier=len(data['events'])+1;data['events'].append([identifier,payload]);return identifier
        return self._write(apply)
    def events(self,after,limit):return [tuple(r) for r in self._data()['events'] if r[0]>after][:limit]
    def insert_usage(self,decision_id,model,prompt,cached,output,source):
        def apply(data):
            identifier=len(data['usage'])+1;data['usage'].append([identifier,decision_id,model,prompt,cached,output,source]);return identifier
        return self._write(apply)
    def insert_usage_share(self,usage_id,classifier,share):
        def apply(data):
            if any((r[0],r[1])==(usage_id,classifier) for r in data['shares']):raise ValueError('duplicate usage share')
            data['shares'].append([usage_id,classifier,share])
        self._write(apply)
    def usage_counts(self):return [tuple(r[1:6]) for r in self._data()['usage']]
    def usage_shares(self):return [tuple(r) for r in self._data()['shares']]
    def usage_by_output(self):return [(r[0],r[2],r[3],r[4],r[5],r[6]) for r in self._data()['usage']]
    def enqueue_learning(self,classifier,since_count,at):
        def apply(data):
            if any(r[1]==classifier for r in data['learning']):return
            data['learning'].append([max((r[0] for r in data['learning']),default=0)+1,classifier,since_count,at,0])
        self._write(apply)
    def pending_learning(self):return [r[1] for r in self._data()['learning']]
    def next_learning(self):return next((tuple(r[:3]) for r in self._data()['learning']),None)
    def learning_rows(self):return [(r[0],r[1],r[2],r[4]) for r in self._data()['learning']]
    def supersede_learning(self,seq):
        def apply(data):
            for row in data['learning']:
                if row[0]==seq:row[4]+=1
        self._write(apply)
    def requeue_learning(self,seq,covered):
        def apply(data):
            for row in data['learning']:
                if row[0]==seq:row[2]=covered;row[4]=0
        self._write(apply)
    def review_count(self,classifier):return sum(r[3]==classifier for r in self._data()['reviews'])
    def delete_learning(self,seq):self._write(lambda data:data.__setitem__('learning',[r for r in data['learning'] if r[0]!=seq]))

class S3CyclotronStores:
    """Explicit composition seam, not hosted deployment or an account directory.

    Host binds all journals to an already-authorized scope and absolute shared
    request ceiling. No FileLease, local directory, SQLite or DDB fallback exists.
    """
    requires_durable_optimizer=True
    def __init__(self,root_journal,*,journal_for,max_records):
        if not callable(journal_for):raise ValueError('fixed scoped journal factory required')
        self.root_storage=S3EmbeddedStorage(root_journal,max_records=max_records,cache_owned=True)
        self.journal_for=journal_for;self.max_records=max_records;self.owner=str(uuid4())
        self.lease=self;self.batch_storage=None;self.runtimes={}
    def _journal(self,domain,identifier):
        root=self.root_storage.journal
        name=sha256((root.namespace+'\0'+identifier).encode()).hexdigest()
        result=self.journal_for(domain,name)
        if not isinstance(result,Journal) or result.boundary!=root.boundary:
            raise PermissionError('cloud composition journal scope mismatch')
        return result
    def acquire(self):
        self.root_storage.initialize();self.root_storage.acquire(self.owner)
        try:
            self.batch_storage=S3BatchStorage(self._journal('shared','ledger'),max_records=self.max_records)
            self.batch_storage.initialize()
        except BaseException:
            self.root_storage.release();raise
    def runtime_for_classifier(self,identifier):
        if identifier in self.runtimes:raise ValueError('classifier runtime already opened')
        runtime=S3RuntimeStores(self._journal('runtime',identifier),max_records=self.max_records,cache_owned=True)
        runtime.initialize();runtime.acquire(self.owner);self.runtimes[identifier]=runtime
        return runtime
    def execution_boundary(self):
        # Called only after the public per-call human/tenant guard succeeds.
        # Native ownership excludes other writers; CAS still fences every write.
        self.root_storage.refresh()
        for runtime in self.runtimes.values():runtime.refresh()
    def release(self):
        errors=[]
        runtimes=list(self.runtimes.values());self.runtimes={}
        for runtime in runtimes:
            try:runtime.close()
            except BaseException as error:errors.append(error)
        try:self.root_storage.release()
        except BaseException as error:errors.append(error)
        if errors:raise errors[0]
