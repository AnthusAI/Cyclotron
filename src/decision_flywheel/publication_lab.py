"""Publication-scoped application contract over an injected Cyclotron facade.

This module owns no physical storage adapter, credentials, cloud configuration or worker
lifecycle. Hosts verify actors and supply account-scoped engines and bindings.
Local HTTP tests are not an authorization to host the SQLite engine remotely.
"""
import hashlib
import inspect
import json
from dataclasses import dataclass, replace
from types import MappingProxyType
from urllib.parse import quote

from .models import Item

CONTRACT = 'cyclotron-publication/v1'
MAX_BYTES = 2_000_000
CAPABILITIES = {'decide':'decide', 'review':'review', 'status':'read', 'history':'read', 'events':'read', 'replay':'read', 'steer':'steer'}


class PublicationError(ValueError):
    def __init__(self, message, status=409, *, code='publication_error'):
        super().__init__(message)
        self.status = status
        self.code = code


def text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise PublicationError(f'{name} must be non-empty bounded text', 400)
    return value


@dataclass(frozen=True)
class PublicationBinding:
    publication_id: str
    outputs: tuple[str, ...]
    revision: str

    def __post_init__(self):
        text(self.publication_id, 'publication identity')
        text(self.revision, 'binding revision')
        object.__setattr__(self, 'outputs', tuple(self.outputs))
        if not self.outputs or len(set(self.outputs)) != len(self.outputs):
            raise ValueError('publication needs distinct output identities')
        for output in self.outputs: text(output, 'output identity')


@dataclass(frozen=True)
class PublicationActor:
    """Already-verified host identity; never accepted from an operation body."""
    account_id: str
    actor_id: str
    grants: frozenset[tuple[str, str]]

    def __post_init__(self):
        text(self.account_id, 'account identity')
        text(self.actor_id, 'actor identity')
        object.__setattr__(self, 'grants', frozenset(self.grants))


class PublicationLab:
    """One account's engine; authority stays with its existing persistence owner."""
    def __init__(self, engine, *, account_id, bindings, records=None, steering=None):
        self.engine = engine
        self.account_id = text(account_id, 'account identity')
        rows = tuple(bindings)
        if len({row.publication_id for row in rows}) != len(rows):
            raise ValueError('distinct publication bindings required')
        outputs = [output for row in rows for output in row.outputs]
        if len(set(outputs)) != len(outputs):
            raise ValueError('distinct publication feedback outputs required')
        self.specs = {spec.id: spec for spec in engine.definition.classifiers}
        known = set(self.specs)
        if not set(outputs) <= known:
            raise ValueError('binding names an unknown classifier output')
        self.bindings = MappingProxyType({row.publication_id: row for row in rows})
        self.records,self.steering = records,steering
        if records is not None:
            if records.account_id!=self.account_id:raise ValueError('publication record account scope mismatch')
            for binding in rows:records.check_binding(binding)

    def authorize(self, actor, publication, capability):
        if not isinstance(actor, PublicationActor) or actor.account_id != self.account_id:
            raise PublicationError('account access refused', 403)
        if (publication, capability) not in actor.grants:
            raise PublicationError('publication capability refused', 403)
        binding = self.bindings.get(publication)
        if binding is None:
            raise PublicationError('publication binding unavailable', 404)
        return binding

    @staticmethod
    def project(decision, binding):
        # Legacy top-level review fields describe the first bound output. Every
        # bound output has its own actual routing values under review.outputs.
        result = replace(decision, results={cid: decision.results[cid] for cid in binding.outputs},
                         review=decision.review.for_output(binding.outputs[0])).to_json()
        result['review']['outputs'] = {
            cid: replace(decision, review=decision.review.for_output(cid)).to_json()['review']
            for cid in binding.outputs}
        return result

    async def command(self, actor, publication, operation, payload, *, execution_guard=None):
        if not isinstance(operation, str):
            raise PublicationError('operation must be text', 400)
        capability = CAPABILITIES.get(operation)
        if capability is None:
            raise PublicationError('unsupported publication operation', 400)
        binding = self.authorize(actor, publication, capability)
        if not isinstance(payload, dict):
            raise PublicationError('structured operation payload required', 400)
        if execution_guard is not None and operation in ('status','history','events','replay'):
            async with self.engine.execution(execution_guard=execution_guard):
                return await self.command(actor,publication,operation,payload)
        async def guarded(preflight):
            if execution_guard is not None:
                result=execution_guard()
                if inspect.isawaitable(result):result=await result
                if result is not None:raise TypeError('execution guard must return None or raise')
            preflight()
        envelope = {'contractVersion':CONTRACT, 'accountId':self.account_id, 'publicationId':publication, 'bindingRevision':binding.revision,
                    'features':{'frozenContext':self.records is not None,'replayExport':self.records is not None,'steering':self.steering is not None},
                    'outputs':[{'id':cid, 'question':self.specs[cid].question,
                                'classes':list(self.specs[cid].labels),
                                'positiveLabel':self.specs[cid].positive_label} for cid in binding.outputs]}
        if operation in ('decide', 'review', 'steer') and payload.get('bindingRevision') != binding.revision:
            raise PublicationError('publication binding revision changed')
        try:
            if operation == 'decide':
                expected={'bindingRevision','item'}|({'context'} if self.records is not None else set())
                if set(payload) != expected:
                    raise PublicationError('unexpected decision fields', 400)
                item = payload['item']
                if not isinstance(item, dict) or set(item) != {'id','values'} or not isinstance(item['values'], dict):
                    raise PublicationError('item needs an explicit identity and structured values', 400)
                text(item['id'], 'item identity')
                encoded = json.dumps(item, allow_nan=False)
                if len(encoded.encode()) > MAX_BYTES:
                    raise PublicationError('item exceeds size limit', 413)
                target=Item(item['id'],json.loads(json.dumps(item['values'],allow_nan=False)))
                prepared=None
                def preflight():
                    nonlocal prepared
                    if self.records is not None:
                        prepared=self.records.prepare(binding,target,payload['context'])
                        for row in self.engine.decision_log():
                            if row['decision']['itemId']==target.id and row['item']!=dict(target.values) and any(value.get('label') is not None for value in row['reviews'].values()):
                                raise PublicationError('labeled source revision cannot change until revision-specific learning is supported')
                if execution_guard is None:
                    preflight();decision=await self.engine.decide(target)
                else:
                    decision=await self.engine.decide(target,execution_guard=lambda:guarded(preflight))
                context=None
                if prepared is not None:
                    def freeze():return self.records.freeze(decision.decision_id,decision.created_at,prepared,prediction=self.project(decision,binding))
                    if execution_guard is None:context=freeze()
                    else:
                        try:
                            async with self.engine.execution(execution_guard=execution_guard):context=freeze()
                        except Exception as exc:
                            raise PublicationError('decision completed; provenance outcome requires reconciliation',code='reconciliation_required') from exc
                return {**envelope, 'decision':self.project(decision, binding),**({'context':context} if context else {})}
            if operation == 'review':
                expected = {'bindingRevision','decisionId','classifier','predictionVersion','predictionFingerprint',
                            'requestId','label','explanation','reasonCode'}
                if self.records is not None:expected.add('contextId')
                if set(payload)-{'explanation'} != expected-{'explanation'}:
                    raise PublicationError('explicit review fields required; actor is host supplied', 400)
                cid = payload['classifier']
                if cid not in binding.outputs:
                    raise PublicationError('classifier is outside publication binding', 403)
                for key in ('decisionId','requestId','label'):
                    text(payload[key], key)
                explanation=payload.get('explanation')
                if explanation is None:explanation=''
                if not isinstance(explanation,str) or len(explanation)>4000:
                    raise PublicationError('explanation must be bounded text when provided',400)
                context=None
                def preflight():
                    nonlocal context
                    decision=self.engine.decision(payload['decisionId'])
                    predicted=decision.results[cid]
                    if type(payload['predictionVersion']) is not int or (predicted.version,predicted.fingerprint)!=(payload['predictionVersion'],payload['predictionFingerprint']):
                        raise PublicationError('displayed prediction revision does not match')
                    if self.records is not None:
                        context=self.records.context(payload['contextId'],publication,decision.decision_id)
                        if context['binding']['values']['revision']!=binding.revision:
                            raise PublicationError('historical binding is read-only; obtain a current decision context')
                        current=next(row for row in self.engine.decision_log() if row['decision']['decisionId']==decision.decision_id)
                        if current['item']!=context['source']['values']:
                            raise PublicationError('superseded source is read-only; revision-specific review is unavailable')
                # Request IDs cannot collide across publication/actor scopes.
                identity=hashlib.sha256(json.dumps([self.account_id,publication,actor.actor_id,payload['requestId'],*([payload['contextId']] if self.records is not None else [])],separators=(',',':')).encode()).hexdigest()
                kwargs={'classifier':cid,'explanation':explanation,'reason_code':payload['reasonCode'],
                        'reviewer':actor.actor_id,'review_id':'publication-'+identity}
                if execution_guard is None:preflight()
                else:kwargs['execution_guard']=lambda:guarded(preflight)
                review=await self.engine.review(payload['decisionId'],payload['label'],**kwargs)
                if context is not None:
                    def receipt():self.records.review(context,review.to_json(),source_cursor=self.review_cursor(review.review_id))
                    if execution_guard is None:receipt()
                    else:
                        try:
                            async with self.engine.execution(execution_guard=execution_guard):receipt()
                        except Exception as exc:
                            raise PublicationError('review completed; provenance outcome requires reconciliation',code='reconciliation_required') from exc
                return {**envelope, 'review':review.to_json()}
            if operation == 'replay':
                if payload or self.records is None:raise PublicationError('frozen replay records require configured provenance and an empty payload',400)
                if not self.feedback_composed(publication,binding):raise PublicationError('canonical feedback requires provenance reconciliation before replay',code='reconciliation_required')
                return {**envelope,'executesLearning':False,'records':self.records.replay(publication,set(binding.outputs))}
            if operation == 'steer':
                if self.steering is None:raise PublicationError('output-scoped steering is not configured',503)
                self.authorize(actor,publication,'read')
                result=await self.steering.command(actor,binding,payload,**({'execution':self.engine.execution,'execution_guard':execution_guard} if execution_guard is not None else {}))
                return {**envelope,'steering':result}
            if operation == 'events':
                if set(payload) != {'after','limit'}:
                    raise PublicationError('events requires an explicit cursor and page size', 400)
                page = self.engine.subscribe(after=payload['after'],limit=payload['limit'])
                events = []
                for event in page['events']:
                    if event['kind'] == 'decision':
                        decision = self.engine.decision(event['decision']['decisionId'])
                        events.append({**event, 'decision':self.project(decision,binding)})
                    elif event['kind'] == 'review' and event['review']['classifier'] in binding.outputs:
                        receipt=self.records.receipt(publication,event['review']['reviewId']) if self.records is not None else None
                        if self.records is None or receipt is not None:
                            events.append({**event,**({'contextId':receipt['contextId']} if receipt else {})})
                    elif event.get('classifier') in binding.outputs:
                        events.append(event)
                return {**envelope, 'events':events, 'cursor':page['cursor']}
            if payload:
                raise PublicationError('this read operation takes an empty payload', 400)
            if operation == 'status':
                return {**envelope, 'classifiers':{cid:self.engine.status(cid).to_json() for cid in binding.outputs}}
            history = []
            rows = self.engine.decision_log()
            if len(rows) > 200:
                raise PublicationError('history exceeds local slice limit; bounded history adapter required', 413)
            replay=self.records.replay(publication,set(binding.outputs)) if self.records is not None else []
            reconciliation=self.records is not None and not self.feedback_composed(publication,binding)
            for row in rows:
                decision = self.engine.decision(row['decision']['decisionId'])
                if self.records is not None:
                    contexts=self.records.contexts(publication,decision.decision_id)
                    for index,context in enumerate(contexts):
                        context=self.records.context(context['contextId'],publication,decision.decision_id)
                        prior=context['binding']['values']
                        outputs=tuple(cid for cid in prior['outputs'] if cid in binding.outputs)
                        if not outputs:continue
                        projected=PublicationBinding(publication,outputs,prior['revision'])
                        reviews={record['review']['classifier']:record['review'] for record in replay if record['contextId']==context['contextId']}
                        history.append({**row,'decision':self.project(decision,projected),'item':context['source']['values'],
                                        'reviews':reviews,'context':context,
                                        'feedbackReconciliationRequired':reconciliation,
                                        'historicalContext':reconciliation or index<len(contexts)-1 or prior['revision']!=binding.revision or context['source']['values']!=row['item']})
                    if not contexts:
                        history.append({**row,'decision':self.project(decision,binding),'item':{},'reviews':{},'provenanceUnavailable':True,'historicalContext':True})
                    continue
                history.append({**row, 'decision':self.project(decision, binding),
                                'reviews':{cid:value for cid,value in row['reviews'].items() if cid in binding.outputs}})
            return {**envelope, 'history':history}
        except PublicationError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise PublicationError('operation conflicts with the classifier contract') from exc

    def review_cursor(self,review_id):
        after=0
        for _ in range(10):
            page=self.engine.subscribe(after=after,limit=1000)
            for event in page['events']:
                if event['kind']=='review' and event['review']['reviewId']==review_id:return event['cursor']
            if len(page['events'])<1000:break
            after=page['cursor']
        raise PublicationError('review acknowledgement requires bounded authoritative event lookup')

    def feedback_composed(self,publication,binding):
        after=0
        for _ in range(10):
            page=self.engine.subscribe(after=after,limit=1000)
            for event in page['events']:
                if event['kind']=='review' and event['review']['classifier'] in binding.outputs:
                    if self.records.receipt(publication,event['review']['reviewId']) is None:return False
            if len(page['events'])<1000:return True
            after=page['cursor']
        raise PublicationError('canonical feedback reconciliation exceeds bounded event lookup')


class EmbeddedPublicationClient:
    """Same command contract without an external service or external store."""
    def __init__(self, lab, actor): self.lab, self.actor = lab, actor
    async def command(self, publication, operation, payload):
        return await self.lab.command(self.actor, publication, operation, payload)


class HTTPPublicationClient:
    """Host owns endpoint, verified credentials and httpx transport. No auto retry."""
    def __init__(self, client, *, account_id=None):
        self.client = client
        self.account_id = text(account_id, 'account identity') if account_id is not None else None
    async def command(self, publication, operation, payload):
        response = await self.client.post('/publications/'+quote(publication,safe='')+'/operations',
            json={'contractVersion':CONTRACT, 'operation':operation, 'payload':payload},
            headers={'X-Cyclotron-Account':self.account_id} if self.account_id is not None else {}, follow_redirects=False)
        if response.status_code >= 300:
            code='publication_error'
            if len(response.content)<=MAX_BYTES:
                try:
                    error=response.json()
                    if isinstance(error,dict) and error.get('code') in ('reconciliation_required','account_changed'):code=error['code']
                except ValueError:pass
            message='remote operation requires explicit reconciliation; outcome is uncertain' if code=='reconciliation_required' else 'remote operation failed; check its outcome before retrying'
            if code=='account_changed':message='selected account changed; refresh account context'
            raise PublicationError(message,response.status_code,code=code)
        if len(response.content) > MAX_BYTES:
            raise PublicationError('remote response exceeds size limit', 502)
        value = response.json()
        if not isinstance(value,dict) or value.get('contractVersion') != CONTRACT or value.get('publicationId') != publication:
            raise PublicationError('remote publication contract mismatch', 502)
        if self.account_id is not None and value.get('accountId') != self.account_id:
            raise PublicationError('remote account contract mismatch', 502)
        return value


@dataclass(frozen=True)
class PublicationAccess:
    """Trusted host resolution of an already-open lab and verified actor."""
    lab: PublicationLab
    actor: PublicationActor
    execution_guard: object = None


def create_publication_host(resolve_access, *, require_execution_guard=True):
    """Resolve fresh account/publication grants for each bounded operation.

    The host owns session verification and engine lifecycle. No cached membership,
    fallback lab, credential issuer, or storage backend is provided here.
    """
    return _create_publication_http(resolve_access=resolve_access,require_execution_guard=require_execution_guard)


def create_publication_app(lab, *, authorize):
    """Local contract host. authorize must return a verified PublicationActor.

    There is no credential/token issuer, default authority or hosted backend
    selector here. A future production host must supply its reviewed service-role
    and human authorization boundaries after the storage conversion is complete.
    """
    return _create_publication_http(lab=lab, authorize=authorize)


def _create_publication_http(*, lab=None, authorize=None, resolve_access=None, require_execution_guard=False):
    import inspect
    from urllib.parse import urlsplit

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    app = FastAPI()

    @app.post('/publications/{publication:path}/operations')
    async def operate(publication: str, request: Request):
        try:
            origin = request.headers.get('origin')
            if origin and urlsplit(origin).netloc != request.headers.get('host'):
                raise PublicationError('cross-origin access refused', 403)
            if authorize is not None:
                actor = authorize(request)
                if inspect.isawaitable(actor): actor = await actor
                if not isinstance(actor, PublicationActor):
                    raise PublicationError('host authorization required', 403)
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise PublicationError('request exceeds size limit', 413)
            try: body = json.loads(raw)
            except (ValueError, UnicodeError): raise PublicationError('invalid JSON', 400) from None
            if not isinstance(body, dict) or set(body) != {'contractVersion','operation','payload'} or body['contractVersion'] != CONTRACT:
                raise PublicationError('unknown publication contract', 400)
            operation = body['operation']
            if not isinstance(operation,str) or operation not in CAPABILITIES or not isinstance(body['payload'],dict):
                raise PublicationError('unknown publication operation or payload',400)
            selected_lab = lab
            execution_guard = None
            if resolve_access is not None and not request.headers.get('x-cyclotron-account'):
                raise PublicationError('expected account pin required',400)
            if resolve_access is not None:
                try:
                    access = resolve_access(request, publication, CAPABILITIES[operation])
                    if inspect.isawaitable(access): access = await access
                except PublicationError:raise
                except Exception:
                    raise PublicationError('host authorization unavailable',503) from None
                if not isinstance(access,PublicationAccess) or not isinstance(access.lab,PublicationLab):
                    raise PublicationError('host authorization required',403)
                selected_lab,actor = access.lab,access.actor
                execution_guard = access.execution_guard
                if require_execution_guard and not callable(execution_guard):
                    raise PublicationError('execution authorization guard required',503)
            selected_lab.authorize(actor,publication,CAPABILITIES[operation])
            expected_account = request.headers.get('x-cyclotron-account')
            if expected_account is not None and expected_account != selected_lab.account_id:
                raise PublicationError('selected account changed',409,code='account_changed')
            result = await selected_lab.command(actor, publication, operation, body['payload'],**({'execution_guard':execution_guard} if execution_guard is not None else {}))
            if len(json.dumps(result, allow_nan=False).encode()) > MAX_BYTES:
                raise PublicationError('response exceeds contract size limit', 413)
            return JSONResponse(result, headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        except PublicationError as exc:
            return JSONResponse({'error':str(exc),'code':exc.code},status_code=exc.status,headers={'Cache-Control':'no-store'})
    return app
