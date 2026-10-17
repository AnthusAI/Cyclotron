"""Bounded console domain transactions with S3 authority and no SQL fallback.

Fixed portable record fields are shared with native migration. Named domain
operations, not a SQL parser, own revisions, pins, feedback and command claims.
"""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from .catalog_storage import encoded, timestamp
from .console_snapshot import SCHEMA, SEQUENCES, TABLES
from .console_snapshot import validate as validate_console
from .cyclotrons import load_config
from .journal import Journal, commit_with_contract, uuid
from .models import DecisionTask
from .run_storage import _UNSET
from .storage_errors import StorageConflict


def now():return datetime.now(timezone.utc).isoformat()
def initial():
    return {'schema':'cyclotron-s3-console/v2','snapshot':{'schema':SCHEMA,'tables':{k:[] for k in TABLES},
            'sequences':{k:0 for k in SEQUENCES}},'claims':{},'actors':{}}
def rows(value,domain,**fields):return [r for r in value['snapshot']['tables'][domain] if all(r[k]==v for k,v in fields.items())]
def one(value,domain,**fields):return next(iter(rows(value,domain,**fields)),None)
def clean(row):return {k:deepcopy(v) for k,v in row.items() if k!='_rowid'}
def decoded(row,*fields):
    value=clean(row)
    for field in fields:value[field]=(load_config(value[field]) if field=='config' and 'mode' in value and 'status' in value else json.loads(value[field])) if value[field] is not None else None
    return value
def insert(value,domain,fields):
    records=value['snapshot']['tables'][domain]
    record={'_rowid':max((r['_rowid'] for r in records),default=0)+1,**fields}
    if domain in SEQUENCES:
        value['snapshot']['sequences'][domain]+=1
        record[SEQUENCES[domain]]=value['snapshot']['sequences'][domain];record['_rowid']=record[SEQUENCES[domain]]
    if set(record)!=set(TABLES[domain])|{'_rowid'}:raise ValueError('complete known domain record required')
    records.append(record);return record

class S3ConsoleStorage:
    def __init__(self,journal,*,max_records,max_conflicts=8):
        if not isinstance(journal,Journal) or type(max_records) is not int or max_records<1 or type(max_conflicts) is not int or not 1<=max_conflicts<=100:
            raise ValueError('scoped canonical console journal and explicit limits required')
        self.journal=journal;self.max_records=max_records;self.max_conflicts=max_conflicts
        self._tx=ContextVar('s3_console_transaction',default=None)
        self._claim=ContextVar('s3_console_claim',default=None)
        self._granted=set()
    def _validate(self,value):
        if not isinstance(value,dict) or set(value)!=set(initial()) or value['schema']!='cyclotron-s3-console/v2' or not isinstance(value['claims'],dict) or not isinstance(value['actors'],dict):raise ValueError('invalid canonical console transaction')
        validate_console(value['snapshot'],max_records=self.max_records,max_bytes=self.journal.max_bytes)
        if sum(map(len,value['snapshot']['tables'].values()))+len(value['claims'])+len(value['actors'])>self.max_records:raise ValueError('console record budget exceeded')
        for job_id,claim in value['claims'].items():
            job=one(value,'web_jobs',id=job_id)
            if job is None or set(claim)!={'owner','fence'} or type(claim['fence']) is not int or claim['fence']<1:raise ValueError('invalid command claim')
            uuid(claim['owner'])
        from .queued_account_authorization import QueuedActor
        for job_id, actor in value['actors'].items():
            job=one(value,'web_jobs',id=job_id)
            if not isinstance(actor,dict):raise ValueError('invalid queued actor')
            parsed=QueuedActor(**actor)
            if job is None or parsed.run_id!=job['run_id'] or parsed.operation!='console:write':raise ValueError('queued actor run boundary refused')
    @staticmethod
    def _upgrade(value):
        if isinstance(value,dict) and set(value)=={'schema','snapshot','claims'} and value['schema']=='cyclotron-s3-console/v1':
            return {**value,'schema':'cyclotron-s3-console/v2','actors':{}}
        return value
    def initialize(self):self._validate(self._upgrade(self.journal.initialize(initial()).value))
    def _record(self):
        record=self.journal.read();record=replace(record,value=self._upgrade(record.value));self._validate(record.value);return record
    def _check_claim(self,value):
        claim=self._claim.get()
        if claim is None:return
        job_id,owner,fence=claim;job=one(value,'web_jobs',id=job_id)
        if job is None or job['status']!='running' or value['claims'].get(job_id)!={'owner':owner,'fence':fence}:
            raise ValueError('stale command execution boundary')
    def _worker_write(self,value,run_id):
        if self._claim.get() is not None or any(one(value,'web_jobs',id=key)['run_id']==run_id for key in value['claims']):
            self._check_claim(value)
            claim=self._claim.get()
            if claim is None or one(value,'web_jobs',id=claim[0])['run_id']!=run_id:
                raise ValueError('worker mutation requires command-bound cloud store')
    def _read(self):
        value=self._tx.get() if self._tx.get() is not None else self._record().value
        self._check_claim(value);return value
    def for_command(self,job_id,owner,fence):
        uuid(owner)
        if type(fence) is not int or fence<1:raise ValueError('valid command fence required')
        if (job_id,owner,fence) not in self._granted:raise ValueError('command claim receipt not confirmed in this worker')
        value=self._read();job=one(value,'web_jobs',id=job_id)
        if job is None or job['status']!='running' or value['claims'].get(job_id)!={'owner':owner,'fence':fence}:raise ValueError('stale command capability')
        return _CommandView(self,(job_id,owner,fence),job['run_id'])
    def _mutate(self,fn):
        if self._tx.get() is not None:return fn(self._tx.get())
        for _ in range(self.max_conflicts):
            record=self._record();self._check_claim(record.value);value=deepcopy(record.value);result=fn(value);self._validate(value)
            if value==record.value:return result
            try:commit_with_contract(self.journal,record,value)
            except StorageConflict:continue
            return result
        raise ValueError('console contention limit reached; retry with same operation identity')
    @contextmanager
    def transaction(self):
        if self._tx.get() is not None:raise ValueError('nested console transaction')
        record=self._record();self._check_claim(record.value);value=deepcopy(record.value);token=self._tx.set(value)
        try:
            yield self
            self._validate(value)
            if value!=record.value:commit_with_contract(self.journal,record,value)
        finally:self._tx.reset(token)
    def close(self):pass
    def export_storage(self,*,max_records=10000,max_bytes=64000000):
        value=self._read()
        if any(one(value,'web_jobs',id=key)['status']=='running' for key in value['claims']):raise ValueError('quiesce cloud command claims before portable export')
        return validate_console(value['snapshot'],max_records=min(max_records,self.max_records),max_bytes=min(max_bytes,self.journal.max_bytes))
    def restore_storage(self,snapshot,*,max_records=10000,max_bytes=64000000):
        snapshot=validate_console(snapshot,max_records=min(max_records,self.max_records),max_bytes=min(max_bytes,self.journal.max_bytes))
        if any(r['status']=='running' for r in snapshot['tables']['web_jobs']):raise ValueError('reconcile native running jobs before cloud import')
        def apply(value):
            if any(value['snapshot']['tables'].values()) or value['claims']:raise ValueError('console restore requires empty destination')
            value['snapshot']=snapshot
        self._mutate(apply)
    def export(self,**limits):
        from .catalog_snapshot import COLUMNS
        from .catalog_snapshot import SCHEMA as catalog_schema
        from .catalog_snapshot import validate as catalog_validate
        snapshot=self.export_storage(**limits)
        return catalog_validate({'schema':catalog_schema,'tables':{t:[clean(r) for r in snapshot['tables'][t]] for t in COLUMNS},
            'feedback_sequence':snapshot['sequences']['item_feedback']},**limits)

    # Catalog revisions and definition activation share this same transaction.
    @staticmethod
    def _classifier(value,identifier,revision=None):
        candidates=rows(value,'classifiers',id=identifier)
        row=next((r for r in candidates if r['revision']==revision),None) if revision is not None else max(candidates,key=lambda r:r['revision'],default=None)
        if row is None:raise ValueError('unknown classifier revision')
        return row
    def classifier(self,identifier,revision=None):return decoded(self._classifier(self._read(),identifier,revision),'config')
    def classifiers(self):
        value=self._read();ids={r['id'] for r in rows(value,'classifiers')}
        return sorted((decoded(self._classifier(value,i),'config') for i in ids),key=lambda r:(r['name'],r['id']))
    def classifier_versions(self,identifier):
        result=[decoded(r,'config') for r in sorted(rows(self._read(),'classifiers',id=identifier),key=lambda r:r['revision'])]
        if not result:raise ValueError('unknown classifier')
        return result
    @staticmethod
    def _definition_insert(value,identifier,name,refs,settings,created):
        revision=max((r['revision'] for r in rows(value,'cyclotron_definitions',id=identifier)),default=0)+1
        return insert(value,'cyclotron_definitions',dict(id=identifier,revision=revision,name=name,classifiers=encoded(refs),settings=encoded(settings),
            fingerprint=sha256(encoded({'name':name,'classifiers':refs,'settings':settings}).encode()).hexdigest(),created_at=created))
    @staticmethod
    def _activate(value,identifier,revision):
        active=one(value,'cyclotron_catalog',id=identifier)
        if active:active['active_revision']=revision
        else:insert(value,'cyclotron_catalog',dict(id=identifier,active_revision=revision))
    def save_classifier(self,identifier,name,config):
        labels=[r.get('label') for r in config.get('classes',[])]
        if not identifier or not name.strip() or not isinstance(config.get('question'),str) or not config['question'].strip() or len(labels)<2 or len(set(labels))!=len(labels) or any(not isinstance(v,str) or not v.strip() for v in labels) or any(r.get('role') not in (None,'positive','negative') for r in config['classes']):raise ValueError('classifier requires a question and distinct ordered classes')
        DecisionTask(identifier,tuple(labels),config['question']);payload=encoded(config);created=now()
        def apply(value):
            previous=max(rows(value,'classifiers',id=identifier),key=lambda r:r['revision'],default=None)
            if previous and (previous['name'],previous['config'])==(name,payload):return decoded(previous,'config')
            revision=previous['revision']+1 if previous else 1
            row=insert(value,'classifiers',dict(id=identifier,revision=revision,name=name,config=payload,created_at=created))
            for active in rows(value,'cyclotron_catalog'):
                old=one(value,'cyclotron_definitions',id=active['id'],revision=active['active_revision'])
                refs=json.loads(old['classifiers'])
                if any(ref['id']==identifier for ref in refs):
                    for ref in refs:
                        if ref['id']==identifier:ref['revision']=revision
                    new=self._definition_insert(value,old['id'],old['name'],refs,json.loads(old['settings']),created)
                    active['active_revision']=new['revision']
            return decoded(row,'config')
        return self._mutate(apply)
    def save_item_list(self,identifier,name):
        if not identifier or not name.strip():raise ValueError('item list identity and name required')
        def apply(value):
            row=one(value,'item_lists',id=identifier)
            if row:row['name']=name
            else:insert(value,'item_lists',dict(id=identifier,name=name))
            return {'id':identifier,'name':name}
        return self._mutate(apply)
    def item_lists(self):
        value=self._read()
        return [{**clean(r),'count':len({i['id'] for i in rows(value,'list_item_revisions',list_id=r['id'])})} for r in sorted(rows(value,'item_lists'),key=lambda r:(r['name'],r['id']))]
    def upsert_list_items(self,list_id,items):
        if not 1<=len(items)<=200:raise ValueError('upsert between one and 200 items')
        created=now()
        def apply(value):
            if not one(value,'item_lists',id=list_id):raise ValueError('unknown item list')
            counts={'inserted':0,'updated':0,'unchanged':0}
            for item in items:
                if not isinstance(item.get('id'),str) or not item['id'] or not isinstance(item.get('values'),dict):raise ValueError('item identity and structured values required')
                occurred=timestamp(item['occurred_at']);payload=encoded({**item,'occurred_at':occurred})
                if len(payload.encode())>4000000:raise ValueError('item exceeds size limit')
                fingerprint=sha256(payload.encode()).hexdigest()
                old=max(rows(value,'list_item_revisions',list_id=list_id,id=item['id']),key=lambda r:r['revision'],default=None)
                if old and old['fingerprint']==fingerprint:counts['unchanged']+=1;continue
                insert(value,'list_item_revisions',dict(list_id=list_id,id=item['id'],revision=old['revision']+1 if old else 1,occurred_at=occurred,fingerprint=fingerprint,payload=payload,imported_at=created))
                counts['updated' if old else 'inserted']+=1
            return counts
        return self._mutate(apply)
    @staticmethod
    def _items(value,list_id):
        if not one(value,'item_lists',id=list_id):raise ValueError('unknown item list')
        latest={}
        for row in rows(value,'list_item_revisions',list_id=list_id):
            if row['id'] not in latest or row['revision']>latest[row['id']]['revision']:latest[row['id']]=row
        return sorted(latest.values(),key=lambda r:(r['occurred_at'],r['id']))
    def snapshot_items(self,list_id):return [{**json.loads(r['payload']),'revision':r['revision'],'fingerprint':r['fingerprint']} for r in self._items(self._read(),list_id)]
    def list_items(self,list_id,*,after=0,limit=200):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=200:raise ValueError('item page must be bounded')
        return self.snapshot_items(list_id)[after:after+limit]
    def item_revision(self,list_id,identifier,revision):
        row=one(self._read(),'list_item_revisions',list_id=list_id,id=identifier,revision=revision)
        if row is None:raise ValueError('unknown item revision')
        return json.loads(row['payload'])
    @staticmethod
    def _latest_votes(value,list_id,item_id,item_revision):
        result={}
        for row in rows(value,'item_feedback',list_id=list_id,item_id=item_id,item_revision=item_revision):
            key=(row['classifier_id'],row['classifier_revision'])
            if key not in result or row['sequence']>result[key]['sequence']:result[key]=row
        return [r for _,r in sorted(result.items()) if not one(value,'item_feedback_retractions',feedback_request_id=r['request_id'])]
    def item_labels(self,list_id,item_id,item_revision):return [clean(r) for r in self._latest_votes(self._read(),list_id,item_id,item_revision)]
    def label_item(self,classifier_id,classifier_revision,list_id,item_id,item_revision,label,comment,request_id):
        if not request_id or not isinstance(comment,str):raise ValueError('feedback identity and comment required')
        created=now()
        def apply(value):
            classifier=self._classifier(value,classifier_id,classifier_revision);config=json.loads(classifier['config'])
            DecisionTask(classifier_id,tuple(r['label'] for r in config['classes']),config['question']).validate_label(label)
            if not one(value,'list_item_revisions',list_id=list_id,id=item_id,revision=item_revision):raise ValueError('unknown item revision')
            fields=dict(classifier_id=classifier_id,classifier_revision=classifier_revision,list_id=list_id,item_id=item_id,item_revision=item_revision,label=label,comment=comment)
            old=one(value,'item_feedback',request_id=request_id)
            if old:
                if any(old[k]!=v for k,v in fields.items()):raise ValueError('feedback identity has different content')
                return clean(old)
            return clean(insert(value,'item_feedback',dict(sequence=0,request_id=request_id,**fields,created_at=created)))
        return self._mutate(apply)
    def retract_item_label(self,feedback_request_id,request_id):
        if not isinstance(request_id,str) or not request_id:raise ValueError('retraction identity required')
        created=now()
        def apply(value):
            existing=one(value,'item_feedback_retractions',request_id=request_id)
            if existing:
                if existing['feedback_request_id']!=feedback_request_id:raise ValueError('retraction identity has different content')
                return clean(existing)
            vote=one(value,'item_feedback',request_id=feedback_request_id)
            if vote is None:raise ValueError('unknown feedback identity')
            latest=max(rows(value,'item_feedback',**{k:vote[k] for k in ('classifier_id','classifier_revision','list_id','item_id','item_revision')}),key=lambda r:r['sequence'])
            if vote!=latest:raise ValueError('only the current label can be retracted')
            if one(value,'item_feedback_retractions',feedback_request_id=feedback_request_id):raise ValueError('label already retracted by another command')
            return clean(insert(value,'item_feedback_retractions',dict(request_id=request_id,feedback_request_id=feedback_request_id,created_at=created)))
        return self._mutate(apply)
    def record_item_prediction(self,run_id,list_id,item_id,item_revision,prediction):
        payload=encoded(prediction)
        def apply(value):
            self._worker_write(value,run_id)
            if not one(value,'list_item_revisions',list_id=list_id,id=item_id,revision=item_revision):raise ValueError('unknown item revision')
            fields=dict(run_id=run_id,list_id=list_id,item_id=item_id,item_revision=item_revision)
            old=one(value,'item_predictions',**fields)
            if old:
                if old['payload']!=payload:raise ValueError('displayed prediction is immutable')
            else:insert(value,'item_predictions',dict(**fields,payload=payload))
        self._mutate(apply)
    def item_results(self,list_id,item_id,item_revision):
        value=self._read();result=[]
        for r in rows(value,'item_predictions',list_id=list_id,item_id=item_id,item_revision=item_revision):
            run=one(value,'web_runs',id=r['run_id'])
            if run:result.append((run['created_at'],{**decoded(r,'payload'),'run_name':run['name']}))
        return [r for _,r in sorted(result,key=lambda p:p[0],reverse=True)]

    def save_cyclotron_definition(self,identifier,name,classifiers,settings):
        from .cyclotron_settings import validate_shared_settings
        if not isinstance(identifier,str) or not identifier.strip() or not isinstance(name,str) or not name.strip() or not isinstance(settings,dict):raise ValueError('cyclotron identity, name and structured settings required')
        settings=validate_shared_settings(settings);refs=[{'id':r['id'],'revision':r['revision']} for r in classifiers]
        if not refs or len({r['id'] for r in refs})!=len(refs):raise ValueError('cyclotron requires distinct ordered classifiers')
        created=now()
        def apply(value):
            for ref in refs:
                if type(ref['revision']) is not int:raise ValueError('unknown classifier revision')
                self._classifier(value,ref['id'],ref['revision'])
            active=one(value,'cyclotron_catalog',id=identifier)
            old=one(value,'cyclotron_definitions',id=identifier,revision=active['active_revision']) if active else None
            if old and (old['name'],old['classifiers'],old['settings'])==(name,encoded(refs),encoded(settings)):return decoded(old,'classifiers','settings')
            row=self._definition_insert(value,identifier,name,refs,settings,created)
            self._activate(value,identifier,row['revision']);return decoded(row,'classifiers','settings')
        return self._mutate(apply)
    @staticmethod
    def _definition(value,identifier,revision=None):
        if revision is None:
            active=one(value,'cyclotron_catalog',id=identifier)
            revision=active['active_revision'] if active else None
        row=one(value,'cyclotron_definitions',id=identifier,revision=revision)
        if row is None:raise ValueError('unknown cyclotron definition')
        return row
    def cyclotron_definition(self,identifier,revision=None):return decoded(self._definition(self._read(),identifier,revision),'classifiers','settings')
    def cyclotron_definitions(self):
        value=self._read()
        return sorted((decoded(self._definition(value,r['id']),'classifiers','settings') for r in rows(value,'cyclotron_catalog')),key=lambda r:(r['name'],r['id']))
    def cyclotron_definition_versions(self,identifier):return [decoded(r,'classifiers','settings') for r in sorted(rows(self._read(),'cyclotron_definitions',id=identifier),key=lambda r:r['revision'])]
    def activate_cyclotron_definition(self,identifier,revision):
        def apply(value):
            row=self._definition(value,identifier,revision);self._activate(value,identifier,revision)
            return decoded(row,'classifiers','settings')
        return self._mutate(apply)
    def run_cyclotron_definition(self,run_id):
        value=self._read();pin=one(value,'run_cyclotron_definitions',run_id=run_id)
        return decoded(self._definition(value,pin['cyclotron_id'],pin['definition_revision']),'classifiers','settings') if pin else None
    def cyclotron_definition_comparison(self,identifier,before_revision,after_revision):
        from .definition_diff import compare_definitions
        value=self._read();before=decoded(self._definition(value,identifier,before_revision),'classifiers','settings')
        after=decoded(self._definition(value,identifier,after_revision),'classifiers','settings');changes=compare_definitions(before,after)
        return {'before':before,'after':after,'changes':changes,'classifiers':[{'id':c['id'],**{side:decoded(self._classifier(value,c['id'],c[side]['revision']),'config') if c[side] is not None else None for side in ('before','after')}} for c in changes['members']]}
    # Run manifests, items and initial events commit with their definition pin.
    @staticmethod
    def _run(value,identifier):
        row=one(value,'web_runs',id=identifier)
        if row is None:raise ValueError('unknown run')
        return row
    def create_run(self,name,mode,config,*,items=(),frozen_feedback=(),initial_events=()):
        if not name.strip() or mode not in ('live','recorded'):raise ValueError('name and supported run mode required')
        identifier=str(uuid4());created=now();payload=encoded(config)
        def apply(value):
            row=insert(value,'web_runs',dict(id=identifier,name=name,mode=mode,status='ready',created_at=created,config=payload))
            for item in items:
                if one(value,'web_items',run_id=identifier,id=item['id']):raise ValueError('duplicate run item')
                insert(value,'web_items',dict(run_id=identifier,id=item['id'],payload=encoded(item),prediction=None,reviewed=0))
            for item_id,classifier_id,feedback in frozen_feedback:insert(value,'replay_feedback',dict(run_id=identifier,item_id=item_id,classifier_id=classifier_id,payload=encoded(feedback)))
            for source_id,event in initial_events:self._event_insert(value,identifier,source_id,event)
            if config.get('cyclotron_definition_revision'):
                definition=self._definition(value,config['cyclotron_id'],config['cyclotron_definition_revision'])
                refs=[{'id':c['id'],'revision':c['revision']} for c in config['classifiers']]
                if definition['classifiers']!=encoded(refs):raise ValueError('run classifiers do not match pinned definition')
                insert(value,'run_cyclotron_definitions',dict(run_id=identifier,cyclotron_id=config['cyclotron_id'],definition_revision=config['cyclotron_definition_revision']))
            return decoded(row,'config')
        return self._mutate(apply)
    def run(self,run_id):
        row=clean(self._run(self._read(),run_id));row['config']=load_config(row['config']);return row
    def runs(self):return [decoded(r,'config') for r in reversed(rows(self._read(),'web_runs'))]
    def run_cyclotron_id(self,run_id):
        value=self._read();run=self._run(value,run_id);edition=one(value,'cyclotron_versions',run_id=run_id)
        return edition['cyclotron_id'] if edition else load_config(run['config']).get('cyclotron_id')
    @staticmethod
    def _labeling(value,run_id):
        run=S3ConsoleStorage._run(value,run_id)
        if run['mode']!='live':return {'allowed':False,'reason':'Recorded runs are read-only.'}
        edition=one(value,'cyclotron_versions',run_id=run_id)
        if edition:
            family=one(value,'cyclotrons',id=edition['cyclotron_id'])
            if edition['revision']!=family['active_revision']:return {'allowed':False,'reason':'Activate this cyclotron version before continuing labeling.'}
        return {'allowed':True,'reason':None}
    def labeling_access(self,run_id):return self._labeling(self._read(),run_id)
    def counts(self,run_id):
        value=self._read();run=self._run(value,run_id);events=[json.loads(r['payload']) for r in rows(value,'web_events',run_id=run_id)]
        result={name:sum(e.get('kind')==kind for e in events) for name,kind in [('cycles','cycle-started'),('predictions','prediction'),('labels','human-feedback'),('optimizations','optimizer-request')]}
        if load_config(run['config']).get('classifiers'):result['cycles']=len({e.get('cycle_number') for e in events if e.get('kind')=='cycle-started' and e.get('cycle_number') is not None})
        return result
    def set_status(self,run_id,status):
        def apply(value):self._worker_write(value,run_id);self._run(value,run_id)['status']=status
        self._mutate(apply)
    def update_run_limits(self,run_id,max_requests,max_optimizer_calls):
        if any(type(v) is not int or v<1 for v in (max_requests,max_optimizer_calls)):raise ValueError('operating limits must be positive integers')
        def apply(value):
            run=self._run(value,run_id)
            if run['mode']!='live':raise ValueError('only live runs accept operating limits')
            if any(r['status'] in ('pending','running') for r in rows(value,'web_jobs',run_id=run_id)):raise ValueError('wait for current work before changing limits')
            run['config']=encoded({**load_config(run['config']),'max_requests':max_requests,'max_optimizer_calls':max_optimizer_calls})
            return decoded(run,'config')
        return self._mutate(apply)
    def summary(self,run_id):
        value=self._read();self._run(value,run_id);row=one(value,'web_summaries',run_id=run_id)
        return json.loads(row['payload']) if row else None
    @staticmethod
    def _event_insert(value,run_id,source_id,payload):
        if not source_id or not isinstance(payload,dict) or not payload.get('kind'):raise ValueError('event identity and kind required')
        raw=encoded(payload)
        if len(raw.encode())>4000000:raise ValueError('event exceeds size limit')
        existing=one(value,'web_events',run_id=run_id,source_id=source_id)
        if existing:
            if existing['payload']!=raw:raise ValueError('event identity already has different content')
            return existing
        return insert(value,'web_events',dict(sequence=0,run_id=run_id,source_id=source_id,payload=raw))
    def complete_run(self,run_id,summary):
        created=now();payload=encoded(summary)
        def apply(value):
            self._worker_write(value,run_id)
            run=self._run(value,run_id);old=one(value,'web_summaries',run_id=run_id)
            if old and old['payload']!=payload:raise ValueError('completed run already has different summary')
            if not old:
                insert(value,'web_summaries',dict(run_id=run_id,payload=payload))
                self._event_insert(value,run_id,'run:completed',{'kind':'run-completed','summary':summary,'created_at':created})
            run['status']='completed';return decoded(run,'config')
        return self._mutate(apply)
    def append_batch(self,run_id,events):
        if not 1<=len(events)<=200:raise ValueError('ingest between one and 200 events')
        def apply(value):
            self._worker_write(value,run_id);self._run(value,run_id)
            return [decoded(self._event_insert(value,run_id,key,event),'payload') for key,event in events]
        return self._mutate(apply)
    def append_event(self,run_id,source_id,payload):return self.append_batch(run_id,[(source_id,payload)])[0]
    def events(self,run_id,*,after=0,limit=1000):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=1000:raise ValueError('event page must be bounded')
        value=self._read();self._run(value,run_id)
        return [decoded(r,'payload') for r in rows(value,'web_events',run_id=run_id) if r['sequence']>after][:limit]
    def all_events(self,run_id):
        value=self._read();self._run(value,run_id);return [decoded(r,'payload') for r in rows(value,'web_events',run_id=run_id)]
    def event_cursor(self,run_id):
        value=self._read();self._run(value,run_id);return max((r['sequence'] for r in rows(value,'web_events',run_id=run_id)),default=0)
    def command(self,run_id,request_id,kind,payload):
        identifier=str(uuid4());raw=encoded(payload)
        if not request_id or kind not in ('prepare','label','correct','skip','undo','optimize','replay-next'):raise ValueError('only live runs accept supported labeling commands')
        def apply(value):
            access=self._labeling(value,run_id)
            if not access['allowed']:raise ValueError('activate this cyclotron version before continuing its labeling' if self._run(value,run_id)['mode']=='live' else 'only live runs accept supported labeling commands')
            old=one(value,'web_jobs',run_id=run_id,request_id=request_id)
            if old:
                if (old['kind'],old['payload'])!=(kind,raw):raise ValueError('command identity has different content')
                return decoded(old,'payload','result')
            if any(r['status'] in ('pending','running') for r in rows(value,'web_jobs',run_id=run_id)):raise ValueError('run already has work in progress')
            return decoded(insert(value,'web_jobs',dict(id=identifier,run_id=run_id,request_id=request_id,kind=kind,payload=raw,status='pending',result=None)),'payload','result')
        return self._mutate(apply)
    def enqueue_authorized_command(self,run_id,request_id,kind,payload,actor):
        from .queued_account_authorization import QueuedActor
        if not isinstance(actor,QueuedActor) or actor.run_id!=run_id or actor.operation!='console:write':
            raise ValueError('trusted run-bound queued actor required')
        # Host captures/proves the actor before this transaction. Worker must
        # verify it again; neither a queue delivery nor this record is a lease.
        with self.transaction():
            value=self._tx.get();existing=one(value,'web_jobs',run_id=run_id,request_id=request_id)
            if existing is not None and existing['id'] not in value['actors']:raise ValueError('legacy command has no actor; cannot adopt it')
            job=self.command(run_id,request_id,kind,payload)
            previous=value['actors'].get(job['id']);provenance=asdict(actor)
            if previous is not None and previous!=provenance:raise ValueError('command already bound to another actor')
            value['actors'][job['id']]=provenance
        return job
    def command_envelope(self,job_id):
        from .queued_account_authorization import QueuedActor
        value=self._read();job=one(value,'web_jobs',id=job_id)
        if job is None:raise ValueError('unknown command')
        actor=value['actors'].get(job_id)
        if actor is None:raise ValueError('queued actor absent; legacy job cannot execute')
        return decoded(job,'payload','result'),QueuedActor(**actor)
    def command_actor(self,job_id,owner,fence):
        from .queued_account_authorization import QueuedActor
        self.for_command(job_id,owner,fence)
        actor=self._read()['actors'].get(job_id)
        if actor is None:raise ValueError('queued actor absent; legacy job cannot execute')
        return QueuedActor(**actor)
    def command_recovery(self,job_id):
        # Operator diagnostic only: never a grant, claim reset or takeover.
        value=self._read();job=one(value,'web_jobs',id=job_id)
        if job is None:raise ValueError('unknown command')
        claim=value['claims'].get(job_id,{})
        return {'jobId':job_id,'runId':job['run_id'],'status':job['status'],
                'owner':claim.get('owner'),'fence':claim.get('fence'),
                'hasActor':job_id in value['actors'],'automaticTakeover':False}
    def jobs(self,run_id):
        value=self._read();self._run(value,run_id);return [decoded(r,'payload','result') for r in reversed(rows(value,'web_jobs',run_id=run_id))][:20]
    def claim_command(self,*,owner=None,job_id=None):
        if owner is None:raise ValueError('cloud command claim requires an explicit worker operation owner')
        uuid(owner)
        def apply(value):
            if any(c['owner']==owner for c in value['claims'].values()):raise ValueError('worker operation owner already claimed; reconcile its result')
            job=next((r for r in rows(value,'web_jobs') if r['status']=='pending' and (job_id is None or r['id']==job_id)),None)
            if job is None:return None
            previous=value['claims'].get(job['id']);fence=previous['fence']+1 if previous else 1
            value['claims'][job['id']]={'owner':owner,'fence':fence};job['status']='running'
            return {**decoded(job,'payload','result'),'claim_owner':owner,'claim_fence':fence}
        result=self._mutate(apply)
        if result is not None:self._granted.add((result['id'],result['claim_owner'],result['claim_fence']))
        return result
    def finish_command(self,job_id,status,result,*,owner=None,fence=None):
        if owner is None or type(fence) is not int:raise ValueError('cloud completion requires its worker owner and fence')
        uuid(owner);raw=encoded(result)
        if (job_id,owner,fence) not in self._granted:raise ValueError('command claim receipt not confirmed in this worker')
        def apply(value):
            job=one(value,'web_jobs',id=job_id);claim=value['claims'].get(job_id)
            if job is None or claim!={'owner':owner,'fence':fence}:raise ValueError('stale cloud command completion')
            if job['status']==status and job['result']==raw:return
            if job['status']!='running':raise ValueError('command is not running')
            if status not in ('completed','failed','interrupted'):raise ValueError('terminal command status required')
            job['status']=status;job['result']=raw
            latest=rows(value,'web_jobs',run_id=job['run_id'])[-1]
            run=self._run(value,job['run_id'])
            if latest['id']==job_id and status=='completed' and isinstance(result,dict) and result.get('finished') is True and run['mode']=='live':run['status']='completed'
        self._mutate(apply)
    def recover_interrupted(self):raise ValueError('cloud command recovery requires explicit scoped owner/provider reconciliation; no global timeout takeover')
    def resume_feedback_command(self,run_id,job_id):raise ValueError('cloud feedback recovery requires reviewed fenced worker composition')
    def current_item(self,run_id):
        value=self._read();self._run(value,run_id)
        row=next((r for r in rows(value,'web_items',run_id=run_id) if not r['reviewed'] and r['prediction'] is not None),None)
        return {'item':json.loads(row['payload']),'prediction':json.loads(row['prediction'])} if row else None
    def update_item(self,run_id,item_id,*,prediction=_UNSET,reviewed=_UNSET):
        if reviewed is not _UNSET and type(reviewed) is not bool:raise ValueError('reviewed must be a boolean')
        def apply(value):
            self._worker_write(value,run_id)
            row=one(value,'web_items',run_id=run_id,id=item_id)
            if row is None:raise ValueError('unknown item')
            if prediction is not _UNSET:row['prediction']=encoded(prediction) if prediction is not None else None
            if reviewed is not _UNSET:row['reviewed']=int(reviewed)
        if prediction is not _UNSET or reviewed is not _UNSET:self._mutate(apply)
    def items(self,run_id):
        value=self._read();self._run(value,run_id);return [json.loads(r['payload']) for r in rows(value,'web_items',run_id=run_id)]
    def unreviewed_items(self,run_id):
        value=self._read();self._run(value,run_id);return [json.loads(r['payload']) for r in rows(value,'web_items',run_id=run_id) if not r['reviewed']]

    # PrimingService uses this same domain transaction, including acceptance.
    def draft(self,identifier):
        row=one(self._read(),'priming_drafts',id=identifier);return row['payload'] if row else None
    def put_draft(self,identifier,payload):
        def apply(value):
            row=one(value,'priming_drafts',id=identifier)
            if row:row['payload']=payload
            else:insert(value,'priming_drafts',dict(id=identifier,payload=payload))
        self._mutate(apply)
    def latest_classifier_revision(self,identifier):return max((r['revision'] for r in rows(self._read(),'classifiers',id=identifier)),default=None)
    def item_exists(self,list_id,identifier):return bool(rows(self._read(),'list_item_revisions',list_id=list_id,id=identifier))
    def insert_item(self,row):self._mutate(lambda value:insert(value,'list_item_revisions',dict(zip(TABLES['list_item_revisions'],row))))
    def insert_feedback(self,row):self._mutate(lambda value:insert(value,'item_feedback',dict(sequence=0,**dict(zip(TABLES['item_feedback'][1:],row)))))
    def accepted_records(self,list_id,classifiers):
        value=self._read();result=[]
        for row in self._items(value,list_id):
            votes={(r['classifier_id'],r['classifier_revision']):clean(r) for r in self._latest_votes(value,list_id,row['id'],row['revision'])}
            result.append({'payload':row['payload'],'revision':row['revision'],'votes':{c['id']:votes[c['id'],c['revision']] for c in classifiers if (c['id'],c['revision']) in votes}})
        return result
    @property
    def priming_storage(self):return self
    @property
    def catalog_storage(self):return self
    @property
    def definition_storage(self):return self
    @property
    def run_storage(self):return self
    def connect(self):raise ValueError('cloud workspace has no SQL connection or native fallback')

    def cyclotrons(self):
        value=self._read()
        return [{**clean(r),'versions':[{'revision':v['revision'],'run_id':v['run_id']} for v in sorted(rows(value,'cyclotron_versions',cyclotron_id=r['id']),key=lambda v:v['revision'])]} for r in sorted(rows(value,'cyclotrons'),key=lambda r:(r['name'],r['id']))]
    def cyclotron_versions(self,cyclotron_id):
        value=self._read();result=[]
        for version in sorted(rows(value,'cyclotron_versions',cyclotron_id=cyclotron_id),key=lambda r:r['revision']):
            run=self._run(value,version['run_id']);metrics={}
            for event in rows(value,'web_events',run_id=run['id']):
                payload=json.loads(event['payload'])
                if payload.get('kind')=='cycle-metrics':metrics[payload['classifier_id']]=payload['metrics']
            result.append({**clean(version),'classifiers':load_config(run['config'])['classifiers'],'metrics':metrics})
        return result
    def activate_cyclotron_version(self,cyclotron_id,revision):
        def apply(value):
            version=one(value,'cyclotron_versions',cyclotron_id=cyclotron_id,revision=revision)
            if version is None:raise ValueError('unknown cyclotron version')
            ids={r['run_id'] for r in rows(value,'cyclotron_versions',cyclotron_id=cyclotron_id)}
            if any(r['run_id'] in ids and r['status'] in ('pending','running') for r in rows(value,'web_jobs')):raise ValueError('wait for current work before reverting')
            one(value,'cyclotrons',id=cyclotron_id)['active_revision']=revision
            return decoded(self._run(value,version['run_id']),'config')
        return self._mutate(apply)
    def inherited_labels(self,run_id,item_id):return [json.loads(r['payload']) for r in rows(self._read(),'replay_feedback',run_id=run_id,item_id=item_id)]
    @classmethod
    def _register_definition(cls,value,run_id,identifier,name,config,created):
        existing=one(value,'run_cyclotron_definitions',run_id=run_id)
        if existing:return existing['definition_revision']
        refs=[{'id':c['id'],'revision':c['revision']} for c in config['classifiers']]
        settings={key:config[key] for key in ('decisions_provider','decisions_model','optimizer_model','optimizer_transport','selection_policy','seed','optimize_every','rubric_changes_every') if key in config}
        pinned=config.get('cyclotron_definition_revision')
        if pinned is not None:
            definition=cls._definition(value,identifier,pinned)
            if definition['classifiers']!=encoded(refs):raise ValueError('run classifiers do not match pinned definition')
        else:
            definition=next((r for r in sorted(rows(value,'cyclotron_definitions',id=identifier),key=lambda r:r['revision']) if (r['name'],r['classifiers'],r['settings'])==(name,encoded(refs),encoded(settings))),None)
            if definition is None:definition=cls._definition_insert(value,identifier,name,refs,settings,created)
        insert(value,'run_cyclotron_definitions',dict(run_id=run_id,cyclotron_id=identifier,definition_revision=definition['revision']))
        return definition['revision']
    def extend_cyclotron(self,parent_run_id,classifier_ids,*,name,seed_supporting_questions=None):
        identifier,family_id,created=str(uuid4()),str(uuid4()),now()
        def apply(value):
            parent=self._run(value,parent_run_id);config=load_config(parent['config'])
            if any(r['status'] in ('pending','running') for r in rows(value,'web_jobs',run_id=parent_run_id)):raise ValueError('wait for current work before extending the cyclotron')
            if parent['mode']!='live' or not config.get('classifiers'):raise ValueError('extend a live multi-classifier run')
            old=config['classifiers'];old_ids={r['id'] for r in old}
            if not classifier_ids or len(set(classifier_ids))!=len(classifier_ids) or old_ids.intersection(classifier_ids):raise ValueError('add distinct new classifiers')
            added=[]
            for cid in classifier_ids:
                classifier=decoded(self._classifier(value,cid),'config');definition=classifier['config']
                positive=next((r['label'] for r in definition['classes'] if r.get('role')=='positive'),None)
                classifier['config']={**definition,'selection_policy':definition.get('selection_policy',{'primary':'f1','aggregation':'positive' if positive else 'macro','positive_class':positive})};added.append(classifier)
            config={**config,'classifiers':[*old,*added],'parent_run_id':parent_run_id,'learning_policy':'fresh-replay','review_policy':'missing-labels-first'}
            for key in ('seed_supporting_questions','cyclotron_definition_revision','cyclotron_definition_fingerprint'):config.pop(key,None)
            if seed_supporting_questions:config['seed_supporting_questions']=seed_supporting_questions
            items=[json.loads(r['payload']) for r in rows(value,'web_items',run_id=parent_run_id)];inherited={}
            for item in items:
                votes=[clean(r) for r in self._latest_votes(value,config['item_list_id'],item['id'],item['revision']) if any(c['id']==r['classifier_id'] and c['revision']==r['classifier_revision'] for c in old)]
                if votes:inherited[item['id']]=votes
            ordered=[r for r in items if r['id'] in inherited]+[r for r in items if r['id'] not in inherited]
            previous=one(value,'cyclotron_versions',run_id=parent_run_id)
            family=previous['cyclotron_id'] if previous else family_id
            if not previous:
                insert(value,'cyclotrons',dict(id=family,name=name,active_revision=1))
                insert(value,'cyclotron_versions',dict(cyclotron_id=family,revision=1,run_id=parent_run_id,parent_run_id=None,created_at=parent['created_at']))
            revision=max(r['revision'] for r in rows(value,'cyclotron_versions',cyclotron_id=family))+1
            config.update(cyclotron_id=family,cyclotron_revision=revision,cyclotron_name=name,backfill_count=len(inherited))
            run=insert(value,'web_runs',dict(id=identifier,name=f'{name} · v{revision} · missing-label review',mode='live',status='ready',created_at=created,config=encoded(config)))
            for item in ordered:
                insert(value,'web_items',dict(run_id=identifier,id=item['id'],payload=encoded(item),prediction=None,reviewed=0))
                for vote in inherited.get(item['id'],[]):insert(value,'replay_feedback',dict(run_id=identifier,item_id=item['id'],classifier_id=vote['classifier_id'],payload=encoded(vote)))
            insert(value,'cyclotron_versions',dict(cyclotron_id=family,revision=revision,run_id=identifier,parent_run_id=parent_run_id,created_at=created))
            one(value,'cyclotrons',id=family)['active_revision']=revision
            self._register_definition(value,parent_run_id,family,name,load_config(parent['config']),created)
            definition_revision=self._register_definition(value,identifier,family,name,config,created)
            definition=self._definition(value,family,definition_revision)
            config.update(cyclotron_definition_revision=definition_revision,cyclotron_definition_fingerprint=definition['fingerprint'])
            run['config']=encoded(config);self._activate(value,family,definition_revision)
            return decoded(run,'config')
        return self._mutate(apply)
    def checkpoint_cyclotron(self,run_id,wheels):
        from dataclasses import asdict
        created=now()
        def apply(value):
            self._worker_write(value,run_id);config=load_config(self._run(value,run_id)['config'])
            snapshot={'classifiers':{identifier:{'fingerprint':wheel.active.fingerprint,'state':asdict(wheel.active)} for identifier,wheel in wheels.items()},
                'definitions':[{k:r[k] for k in ('id','revision')} for r in config['classifiers']],
                'decisions_model':config.get('decisions_model'),'optimizer_model':config.get('optimizer_model'),'selection_policy':config.get('selection_policy'),'seed':config.get('seed'),
                'event_cursor':max((r['sequence'] for r in rows(value,'web_events',run_id=run_id)),default=0)}
            payload=encoded(snapshot);fingerprint=sha256(payload.encode()).hexdigest()
            if not one(value,'cyclotron_checkpoints',run_id=run_id,fingerprint=fingerprint):insert(value,'cyclotron_checkpoints',dict(id=0,run_id=run_id,fingerprint=fingerprint,payload=payload,created_at=created))
            return fingerprint
        return self._mutate(apply)
    def cyclotron_checkpoints(self,run_id):
        value=self._read();self._run(value,run_id);return [decoded(r,'payload') for r in rows(value,'cyclotron_checkpoints',run_id=run_id)]
    # Existing algorithm is record-only; it calls the named cloud methods above.
    from .cyclotrons import Cyclotrons as _NativeAlgorithms
    promote_supporting_question=_NativeAlgorithms.promote_supporting_question
    matched_run_preflight=_NativeAlgorithms.matched_run_preflight
    matched_run_sources=_NativeAlgorithms.matched_run_sources


class _CommandView:
    """Trusted per-call worker capability; no mutable global owner binding."""
    RUN_METHODS={'run','counts','set_status','summary','complete_run','append_batch','append_event','events',
        'all_events','event_cursor','current_item','update_item','items','unreviewed_items','record_item_prediction',
        'inherited_labels','checkpoint_cyclotron','cyclotron_checkpoints','matched_evaluation_inputs','matched_evaluation_target'}
    def __init__(self,store,claim,run_id):self._store=store;self._authority=claim;self._run_id=run_id
    def __getattr__(self,name):
        if name not in self.RUN_METHODS|{'item_labels','label_item','retract_item_label','finish_command'}:raise AttributeError(name)
        target=getattr(self._store,name)
        if not callable(target):return target
        def invoke(*args,**kwargs):
            if name in self.RUN_METHODS and (args[0] if args else kwargs.get('run_id'))!=self._run_id:raise ValueError('command run boundary refused')
            if name=='finish_command' and (args[0] if args else kwargs.get('job_id'))!=self._authority[0]:raise ValueError('command identity boundary refused')
            token=self._store._claim.set(self._authority)
            try:
                if name in ('label_item','retract_item_label'):
                    import inspect
                    params=inspect.signature(target).bind(*args,**kwargs).arguments
                    value=self._store._read();job=one(value,'web_jobs',id=self._authority[0])
                    config=load_config(self._store._run(value,self._run_id)['config'])
                    if name=='label_item':
                        fields=params
                        if job['kind'] not in ('label','correct','replay-next'):raise ValueError('command does not authorize a catalog vote')
                    else:
                        fields=one(value,'item_feedback',request_id=params['feedback_request_id'])
                        if job['kind']!='undo' or fields is None:raise ValueError('command does not authorize a catalog retraction')
                    classifier=next((c for c in config.get('classifiers',[]) if c['id']==fields['classifier_id']),None)
                    item=one(value,'web_items',run_id=self._run_id,id=fields['item_id'])
                    if (classifier is None or classifier['revision']!=fields['classifier_revision']
                            or config.get('item_list_id')!=fields['list_id'] or item is None
                            or json.loads(item['payload'])['revision']!=fields['item_revision']):raise ValueError('catalog write outside frozen command inputs')
                    prefix=job['request_id'] if job['kind']!='replay-next' else 'replay:'+fields['item_id']
                    if params['request_id']!=prefix+':'+fields['classifier_id']:raise ValueError('catalog write outside command identity')
                if name=='finish_command':
                    kwargs.setdefault('owner',self._authority[1]);kwargs.setdefault('fence',self._authority[2])
                return target(*args,**kwargs)
            finally:self._store._claim.reset(token)
        return invoke
