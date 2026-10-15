"""Publication-scoped application contract over an injected Cyclotron facade.

This module owns no persistence, credentials, cloud configuration or worker
lifecycle. Hosts verify actors and supply account-scoped engines and bindings.
Local HTTP tests are not an authorization to host the SQLite engine remotely.
"""
from dataclasses import dataclass, replace
import hashlib
import json
from types import MappingProxyType
from urllib.parse import quote

from .models import Item

CONTRACT = 'cyclotron-publication/v1'
MAX_BYTES = 2_000_000


class PublicationError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


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
    def __init__(self, engine, *, account_id, bindings):
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

    async def command(self, actor, publication, operation, payload):
        if not isinstance(operation, str):
            raise PublicationError('operation must be text', 400)
        capability = {'decide':'decide', 'review':'review', 'status':'read', 'history':'read', 'events':'read'}.get(operation)
        if capability is None:
            raise PublicationError('unsupported publication operation', 400)
        binding = self.authorize(actor, publication, capability)
        if not isinstance(payload, dict):
            raise PublicationError('structured operation payload required', 400)
        envelope = {'contractVersion':CONTRACT, 'publicationId':publication, 'bindingRevision':binding.revision,
                    'outputs':[{'id':cid, 'question':self.specs[cid].question,
                                'classes':list(self.specs[cid].labels),
                                'positiveLabel':self.specs[cid].positive_label} for cid in binding.outputs]}
        if operation in ('decide', 'review') and payload.get('bindingRevision') != binding.revision:
            raise PublicationError('publication binding revision changed')
        try:
            if operation == 'decide':
                if set(payload) != {'bindingRevision','item'}:
                    raise PublicationError('unexpected decision fields', 400)
                item = payload['item']
                if not isinstance(item, dict) or set(item) != {'id','values'} or not isinstance(item['values'], dict):
                    raise PublicationError('item needs an explicit identity and structured values', 400)
                text(item['id'], 'item identity')
                encoded = json.dumps(item, allow_nan=False)
                if len(encoded.encode()) > MAX_BYTES:
                    raise PublicationError('item exceeds size limit', 413)
                decision = await self.engine.decide(Item(item['id'], item['values']))
                return {**envelope, 'decision':self.project(decision, binding)}
            if operation == 'review':
                expected = {'bindingRevision','decisionId','classifier','predictionVersion','predictionFingerprint',
                            'requestId','label','explanation','reasonCode'}
                if set(payload) != expected:
                    raise PublicationError('explicit review fields required; actor is host supplied', 400)
                cid = payload['classifier']
                if cid not in binding.outputs:
                    raise PublicationError('classifier is outside publication binding', 403)
                for key in ('decisionId','requestId','label','explanation'):
                    text(payload[key], key)
                decision = self.engine.decision(payload['decisionId'])
                predicted = decision.results[cid]
                if type(payload['predictionVersion']) is not int or (predicted.version, predicted.fingerprint) != (payload['predictionVersion'], payload['predictionFingerprint']):
                    raise PublicationError('displayed prediction revision does not match')
                # Request IDs cannot collide across publication/actor scopes.
                identity = hashlib.sha256(json.dumps([self.account_id, publication, actor.actor_id, payload['requestId']],separators=(',',':')).encode()).hexdigest()
                review = await self.engine.review(decision.decision_id, payload['label'], classifier=cid,
                    explanation=payload['explanation'], reason_code=payload['reasonCode'], reviewer=actor.actor_id,
                    review_id='publication-'+identity)
                return {**envelope, 'review':review.to_json()}
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
                        events.append(event)
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
            for row in rows:
                decision = self.engine.decision(row['decision']['decisionId'])
                history.append({**row, 'decision':self.project(decision, binding),
                                'reviews':{cid:value for cid,value in row['reviews'].items() if cid in binding.outputs}})
            return {**envelope, 'history':history}
        except PublicationError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise PublicationError('operation conflicts with the classifier contract') from exc


class EmbeddedPublicationClient:
    """Same command contract without an external service or external store."""
    def __init__(self, lab, actor): self.lab, self.actor = lab, actor
    async def command(self, publication, operation, payload):
        return await self.lab.command(self.actor, publication, operation, payload)


class HTTPPublicationClient:
    """Host owns endpoint, verified credentials and httpx transport. No auto retry."""
    def __init__(self, client): self.client = client
    async def command(self, publication, operation, payload):
        response = await self.client.post('/publications/'+quote(publication,safe='')+'/operations',
            json={'contractVersion':CONTRACT, 'operation':operation, 'payload':payload}, follow_redirects=False)
        if response.status_code >= 300:
            raise PublicationError('remote publication operation refused', response.status_code)
        if len(response.content) > MAX_BYTES:
            raise PublicationError('remote response exceeds size limit', 502)
        value = response.json()
        if value.get('contractVersion') != CONTRACT or value.get('publicationId') != publication:
            raise PublicationError('remote publication contract mismatch', 502)
        return value


def create_publication_app(lab, *, authorize):
    """Local contract host. authorize must return a verified PublicationActor.

    There is no credential/token issuer, default authority or hosted backend
    selector here. A future production host must supply its reviewed service-role
    and human authorization boundaries after the storage conversion is complete.
    """
    import inspect
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from urllib.parse import urlsplit
    app = FastAPI()

    @app.post('/publications/{publication:path}/operations')
    async def operate(publication: str, request: Request):
        try:
            origin = request.headers.get('origin')
            if origin and urlsplit(origin).netloc != request.headers.get('host'):
                raise PublicationError('cross-origin access refused', 403)
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
            result = await lab.command(actor, publication, body['operation'], body['payload'])
            if len(json.dumps(result, allow_nan=False).encode()) > MAX_BYTES:
                raise PublicationError('response exceeds contract size limit', 413)
            return JSONResponse(result, headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        except PublicationError as exc:
            return JSONResponse({'error':str(exc)},status_code=exc.status,headers={'Cache-Control':'no-store'})
    return app
