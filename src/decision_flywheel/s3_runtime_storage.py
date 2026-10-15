"""Named core runtime repositories over a bounded canonical S3 transaction.

An explicit owner holds this classifier until release. There is no timeout-based
steal: an uncertain/crashed holder needs reconciliation. Every read and CAS
mutation checks the same owner/fence stored with its data, never a DDB lease.
"""
from copy import deepcopy
import json
from .s3_journal import S3Journal,JournalConflict,JournalUncertain,uuid
from .learning_storage import ARTIFACTS,artifact_table

class RuntimeOwnershipError(RuntimeError):pass

DICT_FIELDS=('state','answers','rounds','features','controls','causes')
LIST_FIELDS=('events','answer_history','evaluations','verdicts','elements','shadow_set','shadow_scored','shadow_looks','shadow_incumbents')

def empty():
    return {'schema':'cyclotron-s3-runtime/v1','owner':None,'fence':0,'ordinal':0,
            'data':{**{k:{} for k in DICT_FIELDS},**{k:[] for k in LIST_FIELDS},'artifacts':{k:{} for k in ARTIFACTS}}}

def json_text(value):
    if not isinstance(value,str):raise ValueError('raw JSON text required')
    json.loads(value)
    return value

class S3RuntimeStores:
    def __init__(self,journal,*,max_records,max_conflicts=8):
        if not isinstance(journal,S3Journal) or type(max_records) is not int or max_records<1 or type(max_conflicts) is not int or not 1<=max_conflicts<=100:
            raise ValueError('canonical journal and bounded records/conflicts required')
        self.journal=journal;self.max_records=max_records;self.max_conflicts=max_conflicts;self.authority=None;self.poisoned=False
        self.runtime_storage=_State(self);self.runtime_cache=_Cache(self);self.learning_storage=_Learning(self)
        self.feature_storage=_Payloads(self,'features');self.control_storage=_Payloads(self,'controls')
        self.hypothesis_storage=_Hypothesis(self);self.shadow_storage=_Shadow(self);self.pending_storage=_Pending(self)
    def _validate(self,value):
        if not isinstance(value,dict) or set(value)!=set(empty()) or value['schema']!='cyclotron-s3-runtime/v1':raise ValueError('invalid canonical runtime')
        if type(value['fence']) is not int or value['fence']<0 or type(value['ordinal']) is not int or value['ordinal']<0:raise ValueError('invalid runtime epoch')
        if value['owner'] is not None:uuid(value['owner'])
        data=value['data']
        if not isinstance(data,dict) or set(data)!=set(empty()['data']):raise ValueError('invalid runtime domains')
        if any(not isinstance(data[k],dict) for k in DICT_FIELDS) or any(not isinstance(data[k],list) for k in LIST_FIELDS):raise ValueError('invalid runtime records')
        if not isinstance(data['artifacts'],dict) or set(data['artifacts'])!=set(ARTIFACTS) or any(not isinstance(v,dict) for v in data['artifacts'].values()):raise ValueError('invalid learning kinds')
        count=sum(len(data[k]) for k in (*DICT_FIELDS,*LIST_FIELDS))+sum(map(len,data['artifacts'].values()))
        if count>self.max_records:raise ValueError('canonical runtime record budget exceeded; no truncation')
    def initialize(self):self._validate(self.journal.initialize(empty()).value)
    def _owned(self,value):
        if self.authority is None or (value['owner'],value['fence'])!=self.authority:
            raise RuntimeOwnershipError('runtime execution ownership unavailable; no timeout takeover')
    def _read(self):
        if self.poisoned:raise RuntimeOwnershipError('uncertain runtime write; reopen/reconcile before continuing')
        current=self.journal.read();self._validate(current.value);self._owned(current.value)
        return current.value['data']
    def _change(self,fn,*,ownership=True,recovery=False):
        if self.poisoned and not recovery:raise RuntimeOwnershipError('uncertain runtime write; reopen/reconcile before continuing')
        for _ in range(self.max_conflicts):
            record=self.journal.read();self._validate(record.value)
            if ownership:self._owned(record.value)
            value=deepcopy(record.value);result=fn(value)
            self._validate(value)
            if value==record.value:return result
            try:self.journal.commit(record,value)
            except JournalConflict:continue
            except JournalUncertain:self.poisoned=True;raise
            return result
        raise RuntimeOwnershipError('runtime contention limit reached')
    def change(self,fn):return self._change(lambda value:fn(value['data'],value))
    def acquire(self,owner):
        uuid(owner)
        if self.authority is not None:raise RuntimeOwnershipError('handle already owns execution')
        def apply(value):
            if value['owner'] is not None:raise RuntimeOwnershipError('runtime already owned; reconcile crashed owner explicitly')
            value['owner']=owner;value['fence']+=1
            return owner,value['fence']
        # Set local permission ONLY after confirmed CAS success.
        self.authority=self._change(apply,ownership=False)
    def close(self):
        if self.authority is None:return
        def release(value):value['owner']=None
        try:self._change(release,recovery=True)
        finally:self.authority=None
    @staticmethod
    def ordinal(value):value['ordinal']+=1;return value['ordinal']

class _View:
    def __init__(self,store):self.store=store
    def read(self):return self.store._read()
    def change(self,fn):return self.store.change(fn)
    def ensure(self):self.read()

class _State(_View):
    def close(self):self.store.close()
    def state(self,key):return self.read()['state'].get(key)
    def bind_contract(self,expected):
        json_text(expected)
        def apply(data,value):
            old=data['state'].get('contract')
            if old is not None and old!=expected:raise ValueError('runtime task/model contract does not match the saved study')
            data['state'].setdefault('contract',expected)
        self.change(apply)
    def put_state(self,key,payload):
        json_text(payload);self.change(lambda data,value:data['state'].__setitem__(key,payload))
    def append_event(self,payload):
        json_text(payload)
        def apply(data,value):
            identifier=(data['events'][-1][0] if data['events'] else 0)+1
            data['events'].append([identifier,payload]);return identifier
        return self.change(apply)
    def history(self,limit=100):
        rows=self.read()['events'];rows=rows if limit<0 else rows[-limit:] if limit else []
        return tuple({**json.loads(payload),'event_id':identifier} for identifier,payload in rows)
    def trace_events(self,*,after_event_id=0,limit=100):
        if type(after_event_id) is not int or after_event_id<0 or type(limit) is not int or not 1<=limit<=1000:raise ValueError('bounded trace page required')
        rows=[r for r in self.read()['events'] if r[0]>after_event_id][:limit]
        return {'events':[{**json.loads(payload),'event_id':identifier} for identifier,payload in rows],
                'cursor':rows[-1][0] if rows else after_event_id}
    def event_payloads(self,kinds):
        if not isinstance(kinds,tuple) or not 1<=len(kinds)<=100 or any(not isinstance(k,str) for k in kinds):raise ValueError('bounded event kinds required')
        return iter([payload for _,payload in self.read()['events'] if json.loads(payload).get('kind') in kinds])
    def events_of(self,kinds):
        if not isinstance(kinds,tuple) or not 1<=len(kinds)<=100:raise ValueError('bounded event kinds required')
        return tuple({**json.loads(p),'event_id':i} for i,p in self.read()['events'] if json.loads(p).get('kind') in kinds)
    def next_cycle_number(self):return max((json.loads(p).get('cycle_number',0) or 0 for _,p in self.read()['events']),default=0)+1

class _Cache(_View):
    def answer(self,key):
        row=self.read()['answers'].get(key);return tuple(row[:2]) if row else None
    def begin_answer(self,key,previous):
        def apply(data,value):
            if previous:data['answer_history'].append([len(data['answer_history'])+1,key,*previous])
            data['answers'][key]=['pending',None,self.store.ordinal(value)]
        self.change(apply)
    def fail_answer(self,key):
        def apply(data,value):
            if key in data['answers']:data['answers'][key][0]='failed'
        self.change(apply)
    def complete_answer(self,key,payload,*,previous,current_key=None):
        json_text(payload)
        def apply(data,value):
            target=current_key or key
            if target!=key:
                if previous and key in data['answers']:data['answers'][key][:2]=previous
                else:data['answers'].pop(key,None)
                data['answers'][target]=['pending',None,self.store.ordinal(value)]
            if target in data['answers']:data['answers'][target][:2]=['complete',payload]
        self.change(apply)
    def round(self,key):
        row=self.read()['rounds'].get(key);return tuple(row[:2]) if row else None
    def reset_pending_round(self,key):
        def apply(data,value):
            row=data['rounds'].get(key)
            if row and row[0]=='pending':del data['rounds'][key]
        self.change(apply)
    def begin_round(self,key):
        def apply(data,value):
            if key in data['rounds']:raise ValueError('round already exists')
            data['rounds'][key]=['pending',None,self.store.ordinal(value)]
        self.change(apply)
    def complete_round(self,key,payload):
        json_text(payload)
        def apply(data,value):
            if key in data['rounds']:data['rounds'][key][:2]=['complete',payload]
        self.change(apply)
    def save_completed_round(self,key,payload):
        json_text(payload)
        self.change(lambda data,value:data['rounds'].__setitem__(key,['complete',payload,self.store.ordinal(value)]))

class _Learning(_View):
    def ensure(self,kind):artifact_table(kind);self.read()
    def artifact(self,kind,key):
        artifact_table(kind);row=self.read()['artifacts'][kind].get(key);return tuple(row[:2]) if row else None
    def completed(self,kind):
        artifact_table(kind);rows=self.read()['artifacts'][kind]
        return [(k,r[1]) for k,r in sorted(rows.items(),key=lambda p:p[1][2],reverse=True) if r[0]=='complete']
    def count(self,kind):artifact_table(kind);return len(self.read()['artifacts'][kind])
    def begin(self,kind,key,payload):
        artifact_table(kind);json_text(payload)
        self.change(lambda data,value:data['artifacts'][kind].__setitem__(key,['pending',payload,self.store.ordinal(value)]))
    def update_payload(self,kind,key,payload):
        artifact_table(kind);json_text(payload)
        def apply(data,value):
            if key in data['artifacts'][kind]:data['artifacts'][kind][key][1]=payload
        self.change(apply)
    def complete(self,kind,key,payload,*,aliases=(),clear_pending_key=None):
        artifact_table(kind);json_text(payload)
        def apply(data,value):
            rows=data['artifacts'][kind]
            if clear_pending_key:
                old=json.loads(rows[clear_pending_key][1]);old['pending_evaluation']=False
                rows[clear_pending_key][1]=json.dumps(old,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
            if key in rows:rows[key][:2]=['complete',payload]
            for alias in aliases:rows[alias]=['complete',payload,self.store.ordinal(value)]
        self.change(apply)

class _Payloads(_View):
    def __init__(self,store,domain):super().__init__(store);self.domain=domain
    def payloads(self):return tuple(value for key,value in sorted(self.read()[self.domain].items()))
    def payload(self,key):return self.read()[self.domain].get(key)
    def save(self,key,payload):json_text(payload);self.change(lambda data,value:data[self.domain].__setitem__(key,payload))

class _Pending(_View):
    DOMAINS={'runtime_rounds':'rounds','runtime_answers':'answers',**{v[0]:k for k,v in ARTIFACTS.items()}}
    def _rows(self,data,kind):
        domain=self.DOMAINS[kind]
        return data[domain] if domain in ('rounds','answers') else data['artifacts'][domain]
    def marks(self):
        data=self.read()
        return {kind:max((r[2] for r in self._rows(data,kind).values()),default=0) for kind in self.DOMAINS}
    def pending(self):
        data=self.read()
        return [(kind,row[2],key) for kind in self.DOMAINS for key,row in self._rows(data,kind).items() if row[0]=='pending']
    def discard(self,kind,ordinal,key):
        if kind not in self.DOMAINS:raise ValueError('unknown pending artifact')
        def apply(data,value):
            rows=self._rows(data,kind);row=rows.get(key)
            if row and row[0]=='pending' and row[2]==ordinal:del rows[key];return True
            return False
        return self.change(apply)

class _Hypothesis(_View):
    def ensure_diagnosis(self):self.read()
    def record_evaluations(self,rows):
        def apply(data,value):
            for row in rows:
                data['evaluations']=[r for r in data['evaluations'] if (r[1],r[2],r[7])!=(row[0],row[1],row[6])]
                data['evaluations'].append([self.store.ordinal(value),*row])
        self.change(apply)
    def append_verdict(self,row):self.change(lambda data,value:data['verdicts'].append(list(row)))
    def verdicts(self,idea_id):return [tuple(r[1:]) for r in self.read()['verdicts'] if r[0]==idea_id]
    def _evaluations(self,idea_id,context_version=None):
        return [r for r in self.read()['evaluations'] if r[1]==idea_id and (context_version is None or r[7]==context_version)]
    def latest(self,idea_id,context_version=None):return [(r[2],r[3]) for r in sorted(self._evaluations(idea_id,context_version),key=lambda r:(r[8],r[0]))]
    def marks(self,idea_id,since=None,*,strict=False):return [r[3] for r in self._evaluations(idea_id) if since is None or (r[8]>since if strict else r[8]>=since)]
    def verdict_idea_ids(self):return sorted({r[0] for r in self.read()['verdicts']})
    def idea_ids(self,context_version=None):return sorted({r[1] for r in self.read()['evaluations'] if context_version is None or r[7]==context_version})
    def idea_payload(self,idea_id):return self.store.control_storage.payload(idea_id)
    def idea_payloads(self):return self.store.control_storage.payloads()
    def save_cause(self,row):self.change(lambda data,value:data['causes'].__setitem__(row[0],list(row[1:])))
    def causes(self,round_number=None):return [r[1] for key,r in sorted(self.read()['causes'].items(),key=lambda p:(p[1][0],p[0])) if round_number is None or r[0]==round_number]
    def save_element(self,row):
        def apply(data,value):
            data['elements']=[r for r in data['elements'] if (r[1],r[2])!=(row[0],row[1])]
            data['elements'].append([self.store.ordinal(value),*row])
        self.change(apply)
    def elements(self):return [tuple(r[1:13]) for r in sorted(self.read()['elements'],key=lambda r:(r[2],r[0]))]
    def wrong(self,idea_id,context_version=None,*,recent=None):
        rows=sorted((r for r in self._evaluations(idea_id,context_version) if r[3]==0),key=lambda r:(r[8],r[0]),reverse=recent is not None)
        return [(r[2],r[4],r[5]) for r in (rows if recent is None else rows[:recent])]

class _Shadow(_View):
    def calls(self):return len(self.read()['shadow_scored'])
    def active(self):return [tuple(r[:3]) for r in sorted(self.read()['shadow_set'],key=lambda r:(r[2],r[0])) if r[3]=='active']
    def known(self):return {r[0] for r in self.read()['shadow_set']}
    def statuses(self):return {r[0]:r[3] for r in self.read()['shadow_set']}
    def set_status(self,idea_id,status):
        def apply(data,value):
            for row in data['shadow_set']:
                if row[0]==idea_id:row[3]=status
        self.change(apply)
    def _append(self,domain,row,identity,ignore=False):
        def apply(data,value):
            if any(tuple(r[:identity])==tuple(row[:identity]) for r in data[domain]):
                if ignore:return
                raise ValueError('duplicate forward-evaluation identity')
            data[domain].append(list(row))
        self.change(apply)
    def join(self,idea_id,control,seq):self._append('shadow_set',(idea_id,control,seq,'active'),1)
    def scored(self,idea_id,item_id,seq):self._append('shadow_scored',(idea_id,item_id,seq),2,True)
    def incumbent(self,incumbent_id,rubric,seq):self._append('shadow_incumbents',(incumbent_id,rubric,seq),1,True)
    def incumbent_ids(self):return {r[0] for r in self.read()['shadow_incumbents']}
    def looks_taken(self,idea_id,incumbent):return sum(r[0]==idea_id and r[1]==incumbent for r in self.read()['shadow_looks'])
    def look(self,row):self._append('shadow_looks',row,3)
