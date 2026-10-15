"""Optional application-level priming; customer transformation code stays external.

The only domain plugin boundary is HTTP. Endpoints are supplied by the host,
never by untrusted items. A transformer returns a complete candidate, never a
classification. Drafts are excluded from operational catalog membership.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import quote, urlsplit
import httpx
from .priming_files import checked_schema, read_rows, template, validate_rows, MAX_BYTES
from .workspace_catalog import encoded


def fingerprint(value):return hashlib.sha256(encoded(value).encode()).hexdigest()


@dataclass(frozen=True)
class HTTPTransformer:
    id: str
    name: str
    endpoint: str
    fields: tuple[dict, ...]
    headers: dict | None = None
    allow_local: bool = False

    def __post_init__(self):
        url=urlsplit(self.endpoint)
        local=url.hostname in ('127.0.0.1','localhost','::1') and self.allow_local
        if (url.scheme!='https' and not (local and url.scheme=='http')) or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('transformer endpoint requires HTTPS (explicit loopback allowed for local development)')
        names=[field['name'] for field in self.fields]
        if not self.id or len(set(names))!=len(names):raise ValueError('transformer fields need distinct names')


class PrimingService:
    def __init__(self,store,*,transformers=(),schemas=None,client=None,redact=(),storage=None):
        self.store=store;self.transformers={row.id:row for row in transformers};self.schemas=schemas or {};self.redact=tuple(redact)+tuple(secret for transformer in transformers for value in (transformer.headers or {}).values() for secret in (value,value[7:] if value.lower().startswith('bearer ') else value))
        if len(self.transformers)!=len(transformers):raise ValueError('duplicate transformer identity')
        self.client=client or httpx.Client(timeout=20,follow_redirects=False)
        self.owns_client=client is None
        self.storage=storage if storage is not None else store.priming_storage

    def close(self):
        if self.owns_client:self.client.close()

    def safe(self,value):
        data=encoded(value)
        if len(data.encode())>MAX_BYTES*2:raise ValueError('import exceeds size limit')
        if any(secret and secret in data for secret in self.redact):raise ValueError('import contains a credential')
        return json.loads(data)

    def contract(self,list_id,refs):
        if list_id not in {row['id'] for row in self.store.item_lists()}:raise ValueError('unknown item list')
        if not isinstance(refs,list) or not refs or len(refs)>20 or len({row['id'] for row in refs})!=len(refs):
            raise ValueError('choose distinct classifier revisions')
        classifiers=[self.store.classifier(row['id'],row['revision']) for row in refs]
        schema=self.schemas.get(list_id)
        declarations=[row['config']['item_schema'] for row in classifiers if 'item_schema' in row['config']]
        if schema is None and declarations:schema=declarations[0]
        schema=checked_schema(schema or {'type':'object'})
        if any(declaration!=schema for declaration in declarations):raise ValueError('selected classifiers require different item schemas')
        return self.safe({'list_id':list_id,'classifiers':classifiers,'schema':schema,'transformers':[
            {'id':t.id,'name':t.name,'fields':t.fields} for t in self.transformers.values()]})

    def examples(self,list_id,refs):
        """Accepted, explicitly labeled examples for a new embedded SDK store."""
        from .priming_context import catalog_priming,decode_priming
        contract=self.contract(list_id,refs)
        return decode_priming(catalog_priming(self.store,list_id,contract['classifiers'],set(),storage=self.storage))

    def template(self,list_id,refs,kind):
        contract=self.contract(list_id,refs)
        return template(kind,contract['schema'],contract['classifiers'])

    def _read(self,db,identifier):
        row=db.draft(identifier)
        if row is None:raise ValueError('unknown import draft')
        return json.loads(row)

    def _write(self,db,draft):
        self.safe(draft)
        db.put_draft(draft['id'],encoded(draft))

    def get(self,identifier):
        with self.storage.transaction() as db:return self._read(db,identifier)

    def _begin(self,identifier,contract,source):
        if not isinstance(identifier,str) or not identifier.strip() or len(identifier)>200:raise ValueError('bounded draft identity required')
        signature=fingerprint({'contract':contract,'source':source})
        with self.storage.transaction() as db:
            existing=db.draft(identifier)
            if existing:
                draft=json.loads(existing)
                if draft['signature']!=signature:raise ValueError('draft identity has different content')
                return draft,False
            draft={'id':identifier,'version':1,'status':'starting','contract':contract,'source':source,'signature':signature,
                   'rows':[],'errors':[],'feedback':[],'actions':{},'job_id':None,'message':None,'fields':[]}
            self._write(db,draft)
            return draft,True

    def start_file(self,identifier,list_id,refs,kind,content):
        contract=self.contract(list_id,refs)
        self.safe(content)
        try:
            raw,errors=read_rows(kind,content,contract['schema'],contract['classifiers'])
            self.safe(raw)
            rows,validation=validate_rows(raw,contract['schema'],contract['classifiers'])
            status='invalid' if errors or validation else 'preview'
            errors+=validation
        except ValueError as exc:
            if 'credential' in str(exc):raise
            rows=[];status='invalid';errors=[{'row':0,'field':'file','message':str(exc)}]
        draft,created=self._begin(identifier,contract,{'kind':'file','format':kind,'content':content})
        if not created and draft['status']!='starting':return draft
        draft.update(status=status,rows=rows,errors=errors)
        with self.storage.transaction() as db:
            current=self._read(db,identifier)
            if current['status']!='starting' or current['version']!=draft['version']:return current
            try:self._write(db,draft)
            except ValueError:
                draft.update(status='invalid',rows=[],errors=[{'row':0,'field':'file','message':'Expanded preview exceeds the aggregate draft limit. Split this file into smaller batches.'}])
                self._write(db,draft)
        return draft

    def _request(self,transformer,method,path,body=None,key=None):
        headers=dict(transformer.headers or {})
        if key:headers['Idempotency-Key']=key
        with self.client.stream(method,transformer.endpoint.rstrip('/')+path,json=body,headers=headers) as response:
            response.raise_for_status();content=bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content)>MAX_BYTES:raise ValueError('transformer response exceeds size limit')
        return self.safe(json.loads(content))

    def _inputs(self,fields,inputs):
        if not isinstance(inputs,dict) or set(inputs)-{row['name'] for row in fields}:raise ValueError('unknown input fields')
        for field in fields:
            value=inputs.get(field['name'],'')
            if not isinstance(value,str) or (field.get('required') and not value.strip()):raise ValueError('required input missing or invalid')
        return self.safe(inputs)

    def start_transform(self,identifier,list_id,refs,transformer_id,inputs):
        transformer=self.transformers.get(transformer_id)
        if transformer is None:raise ValueError('unknown configured transformer')
        inputs=self._inputs(transformer.fields,inputs)
        contract=self.contract(list_id,refs)
        draft,created=self._begin(identifier,contract,{'kind':'transform','transformer':transformer_id,'inputs':inputs})
        # A failed/uncertain start retries the same external idempotency key.
        if not created and (draft['status'] not in ('starting','failed') or draft['job_id']):return draft
        return self._remote(draft,transformer,'POST','/jobs',{'contract_version':'cyclotron-transform/v1','request_id':identifier,
            'inputs':inputs,'item_schema':contract['schema'],'classifiers':[{'id':c['id'],'input_field':c['config'].get('input_field','text')} for c in contract['classifiers']]},identifier)

    def _remote(self,draft,transformer,method,path,body=None,key=None):
        try:result=self._request(transformer,method,path,body,key)
        except Exception:result={'status':'failed','message':'Transformer request failed. Retry or cancel; no examples were imported.'}
        with self.storage.transaction() as db:
            current=self._read(db,draft['id'])
            job=result.get('job_id')
            if current['status']=='cancelled' and not current['job_id'] and isinstance(job,str) and 0<len(job)<=200:
                current.update(job_id=job,remote_cancel='pending');self._write(db,current)
            if current['status'] in ('cancelled','accepted') or current['version']!=draft['version']:return current
            if job is not None:
                if not isinstance(job,str) or not job or len(job)>200:raise ValueError('invalid transformer job identity')
                if current['job_id'] and current['job_id']!=job:raise ValueError('transformer changed job identity')
                current['job_id']=job
            status=result.get('status')
            current.update(rows=[],errors=[],fields=[],message=None)
            if status=='complete':
                candidate=result.get('candidate')
                if not isinstance(candidate,dict) or 'labels' in candidate:
                    current.update(status='failed',message='Transformer must return a complete item without classification labels.')
                else:
                    rows,errors=validate_rows([candidate],current['contract']['schema'],current['contract']['classifiers'],require_labels=False)
                    current.update(status='invalid' if errors else 'preview',rows=rows,errors=errors)
            elif status in ('queued','running','clarification') and current['job_id']:
                fields=result.get('fields',[])
                if not isinstance(fields,list) or any(not isinstance(f,dict) or not isinstance(f.get('name'),str) or not isinstance(f.get('label'),str) for f in fields):
                    raise ValueError('invalid clarification fields')
                current.update(status=status,message=result.get('message'),fields=fields)
            elif status=='cancelled':current['status']='cancelled'
            else:current.update(status='failed',message='Transformation failed. Retry or revise; no examples were imported.')
            self._write(db,current)
            return current

    def poll(self,identifier):
        draft=self.get(identifier)
        if draft['status'] in ('accepted','cancelled','preview') or not draft['job_id']:return draft
        transformer=self.transformers[draft['source']['transformer']]
        return self._remote(draft,transformer,'GET','/jobs/'+quote(draft['job_id'],safe=''))

    def feedback(self,identifier,version,request_id,reason,inputs):
        if not isinstance(reason,str) or not reason.strip():raise ValueError('transformation feedback required')
        with self.storage.transaction() as db:
            draft=self._read(db,identifier)
            if draft['source']['kind']!='transform' or not draft['job_id']:raise ValueError('no transformer job to revise')
            transformer=self.transformers[draft['source']['transformer']]
            fields=draft['fields'] if draft['status']=='clarification' else transformer.fields
            action=self.safe({'reason':reason,'inputs':inputs,'version':version})
            retry=request_id in draft['actions']
            if retry:
                if draft['actions'][request_id]!=action:raise ValueError('feedback identity has different content')
                if draft['status']!='failed':return draft
            else:
                if draft['version']!=version or draft['status'] not in ('preview','clarification','invalid','failed'):raise ValueError('stale or closed preview')
                self._inputs(fields,inputs if draft['status']=='clarification' else {**draft['source']['inputs'],**inputs})
                if not request_id or len(request_id)>200:raise ValueError('bounded feedback identity required')
                draft['actions'][request_id]=action;draft['feedback'].append(action)
                draft['version']+=1
            draft.update(status='running',rows=[],errors=[],fields=[],message=None)
            self._write(db,draft)
        return self._remote(draft,transformer,'POST','/jobs/'+quote(draft['job_id'],safe='')+'/feedback',
                            {'request_id':request_id,'reason':reason,'inputs':inputs},identifier+':'+request_id)

    def cancel(self,identifier):
        with self.storage.transaction() as db:
            draft=self._read(db,identifier)
            if draft['status']=='accepted':raise ValueError('accepted examples cannot be cancelled')
            draft.update(status='cancelled',rows=[])
            if draft['job_id'] and draft.get('remote_cancel')!='confirmed':draft['remote_cancel']='pending'
            self._write(db,draft)
        if draft.get('remote_cancel')=='pending':
            transformer=self.transformers[draft['source']['transformer']]
            try:
                result=self._request(transformer,'DELETE','/jobs/'+quote(draft['job_id'],safe=''),key=identifier+':cancel')
                if result.get('status')=='cancelled':
                    with self.storage.transaction() as db:
                        draft=self._read(db,identifier);draft['remote_cancel']='confirmed';self._write(db,draft)
            except Exception:pass  # locally terminal; caller sees pending remote cancellation and can retry
        return draft

    def accept(self,identifier,version,labels):
        with self.storage.transaction() as db:
            draft=self._read(db,identifier)
            acceptance=self.safe({'version':version,'labels':labels})
            if draft['status']=='accepted':
                if draft['acceptance']!=acceptance:raise ValueError('acceptance has different content')
                return draft
            if draft['version']!=version or draft['status']!='preview':raise ValueError('stale or closed preview')
            contract=draft['contract'];classifiers=contract['classifiers']
            for classifier in classifiers:
                latest=db.latest_classifier_revision(classifier['id'])
                if latest!=classifier['revision']:raise ValueError('classifier revision changed; preview again')
            if labels is not None and (not isinstance(labels,list) or len(labels)!=len(draft['rows'])):raise ValueError('labels must match preview rows')
            raw=[{**row['item'],'labels':labels[n] if labels is not None else row['labels']} for n,row in enumerate(draft['rows'])]
            rows,errors=validate_rows(raw,contract['schema'],classifiers)
            if errors:raise ValueError('explicit human labels and valid optional explanations required for every row: '+encoded(errors))
            now=datetime.now(timezone.utc).isoformat()
            for row in rows:
                item=row['item'];provenance=item.get('provenance',{})
                if not isinstance(provenance,dict):raise ValueError('provenance must be an object')
                if db.item_exists(contract['list_id'],item['id']):raise ValueError('item already exists; use the catalog correction workflow')
                item['provenance']={**provenance,'priming':{'draft_id':identifier,'version':version,'route':draft['source']['kind'],
                    'inputs':draft['source'].get('inputs'),'file_sha256':fingerprint(draft['source']) if draft['source']['kind']=='file' else None,
                    'feedback':draft['feedback'],'model_representation':row['model_representation'],'classifiers':[{'id':c['id'],'revision':c['revision']} for c in classifiers]}}
                data=encoded(self.safe(item))
                if len(data.encode())>MAX_BYTES:raise ValueError('item exceeds size limit')
                db.insert_item((contract['list_id'],item['id'],1,item['occurred_at'],fingerprint(item),data,now))
                for classifier in classifiers:
                    cid=classifier['id'];vote=row['labels'][cid]
                    db.insert_feedback(('priming:'+fingerprint([identifier,item['id'],cid]),cid,classifier['revision'],contract['list_id'],item['id'],1,vote['label'],vote['rationale'],now))
            draft.update(status='accepted',acceptance=acceptance)
            self._write(db,draft)
            return draft
