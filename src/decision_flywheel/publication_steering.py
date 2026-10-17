"""Scoped steering delegates native trials; the host retains writer ownership.

Catalog operations are composed, not reimplemented. This single-owner gateway
is not a distributed claim/coordinator and must not enable cloud execution until
that owner contract exists. Pending ambiguous operations never auto-dispatch.
"""
from datetime import datetime, timezone
from types import MappingProxyType

from .publication_lab import MAX_BYTES, PublicationActor, PublicationError, text
from .publication_records import canonical, copied, digest


class NativeOutputEvaluator:
    """One native wheel, with explicit authoritative partitions/propensities."""
    def __init__(self,output_id,wheel,*,partitions):
        if wheel.initial.task.name!=output_id:raise ValueError('native evaluator output identity mismatch')
        self.output_id,self.wheel,self.partitions=output_id,wheel,partitions

    @property
    def active_fingerprint(self):return self.wheel.active.fingerprint

    async def execute(self,action,payload):
        evidence=self.partitions()
        if not isinstance(evidence,dict) or set(evidence)!={'training','development','protected','propensities'}:
            raise ValueError('authoritative partitions and recorded propensities are required')
        if action=='evaluate':
            result=await self.wheel.improve(evidence['training'],evidence['development'],protected=evidence['protected'],
                propensities=evidence['propensities'],candidate_proposal=payload['proposal'],apply_promotion=False)
            # Sibling safety is enforced natively; sibling metrics/labels are not
            # exposed as the publication's own command result.
            result={key:value for key,value in result.items() if key in {'promoted','improved','trial_fingerprint','reason','incumbent','candidate','promotion_metric','development_counts'}}
            return {**result,'active_fingerprint':self.active_fingerprint}
        self.wheel.promote_trial(payload['trialFingerprint'],evidence['training'],evidence['development'])
        return {'promoted':True,'trial_fingerprint':payload['trialFingerprint'],'active_fingerprint':self.active_fingerprint}


class PublicationSteering:
    def __init__(self,records,evaluators,*,owner_lock=None,owner_execution=None,allow_evaluation=False,max_result_bytes=64_000):
        if (owner_lock is None)==(owner_execution is None):raise ValueError('exactly one existing engine execution owner required')
        if owner_lock is not None and not hasattr(owner_lock,'__aenter__'):raise ValueError('existing engine owner lock required')
        if owner_execution is not None and not callable(owner_execution):raise ValueError('public execution scope factory required')
        if any(cid!=value.output_id for cid,value in evaluators.items()):raise ValueError('scoped evaluator identity mismatch')
        self.records=records;self.evaluators=MappingProxyType(dict(evaluators));self.owner_lock=owner_lock;self.owner_execution=owner_execution
        self.allow_evaluation=allow_evaluation
        if type(max_result_bytes) is not int or not 1<=max_result_bytes<=MAX_BYTES:raise ValueError('bounded positive steering result byte budget required')
        self.max_result_bytes=max_result_bytes

    def found(self,kind,operation_id):
        rows=[row['data'] for row in self.records.records() if row['kind']==kind and row['data']['operationId']==operation_id]
        if len(rows)>1:raise PublicationError('steering operation receipt conflict')
        return rows[0] if rows else None

    async def command(self,actor,binding,payload,*,execution=None,execution_guard=None):
        publication=binding.publication_id
        if not isinstance(actor,PublicationActor) or actor.account_id!=self.records.account_id or not {(publication,'steer'),(publication,'read')}<=actor.grants:
            raise PublicationError('publication steering capability refused',403)
        if not isinstance(payload,dict):raise PublicationError('structured steering command required',400)
        action=payload.get('action');cid=payload.get('classifier')
        if action not in ('evaluate','promote'):raise PublicationError('only evaluated proposal and guarded promotion are supported',400)
        expected={'bindingRevision','bindingRef','classifier','requestId','expectedFingerprint','action','explanation', 'proposal' if action=='evaluate' else 'trialFingerprint'}
        if set(payload)!=expected:raise PublicationError('explicit steering command fields required',400)
        if cid not in binding.outputs or cid not in self.evaluators:raise PublicationError('steering output is outside the publication binding',403)
        if payload['bindingRevision']!=binding.revision:raise PublicationError('publication binding changed')
        for key in ('requestId','expectedFingerprint','explanation'):text(payload[key],key)
        if action=='evaluate':
            proposal=payload['proposal']
            if not isinstance(proposal,dict) or not proposal or set(proposal)-{'rubric','tasks','example_ids','dynamic_elements'}:
                raise PublicationError('explicit native proposal fields required',400)
        else:text(payload['trialFingerprint'],'trial identity')
        if len(canonical(payload).encode())>MAX_BYTES:raise PublicationError('steering command exceeds byte limit',413)
        operation_id=digest([actor.account_id,publication,actor.actor_id,payload['requestId']]);signature=digest(payload)
        execution=execution or self.owner_execution
        async with execution(execution_guard=execution_guard) if execution is not None else self.owner_lock:
            if execution_guard is not None and execution is None:raise PublicationError('guarded execution owner required',503)
            try:self.records.binding_revision(binding,payload['bindingRef'])
            except ValueError as exc:raise PublicationError('steering binding reference does not match') from exc
            try:self.records.check_binding(binding)
            except ValueError as exc:raise PublicationError('historical publication output ownership conflicts') from exc
            complete=self.found('steering-complete',operation_id)
            if complete is not None:
                if complete['signature']!=signature:raise PublicationError('steering request identity has different content')
                return complete['receipt']
            pending=self.found('steering-pending',operation_id)
            if pending is not None:
                if pending['signature']!=signature:raise PublicationError('steering request identity has different content')
                raise PublicationError('steering outcome requires explicit reconciliation; no automatic reexecution',code='reconciliation_required')
            evaluator=self.evaluators[cid]
            if evaluator.active_fingerprint!=payload['expectedFingerprint']:raise PublicationError('active classifier revision changed')
            if self.allow_evaluation is not True:raise PublicationError('host has not authorized steering execution',503)
            if action=='promote':
                authorized=[row['data']['receipt'] for row in self.records.records() if row['kind']=='steering-complete'
                    and row['data']['receipt']['publicationId']==publication and row['data']['receipt']['classifier']==cid
                    and row['data']['receipt']['bindingRevision']==binding.revision and row['data']['receipt']['action']=='evaluate'
                    and row['data']['receipt']['result'].get('trial_fingerprint')==payload['trialFingerprint']]
                if not authorized:raise PublicationError('trial was not evaluated for this publication binding')
            intent={'operationId':operation_id,'signature':signature,'publicationId':publication,'actorId':actor.actor_id,'payload':copied(payload)}
            at=datetime.now(timezone.utc).isoformat(timespec='microseconds')
            receipt={'operationId':operation_id,'publicationId':publication,'classifier':cid,'bindingRevision':binding.revision,
                     'bindingRef':payload['bindingRef'],'action':action,'explanation':payload['explanation'],'setBy':actor.actor_id}
            try:
                pending_record=self.records.preview('steering-pending',intent,at)
                # A JSON string of this size bounds any accepted serialized
                # result, with conservative extra quote bytes and row metadata.
                terminal_record=self.records.preview('steering-complete',{'operationId':operation_id,'signature':signature,
                    'receipt':{**receipt,'result':'x'*self.max_result_bytes}},at)
                self.records.ensure_capacity([pending_record,terminal_record])
            except ValueError as exc:
                raise PublicationError('steering receipt capacity unavailable; nothing dispatched',413) from exc
            # Host guarantees one existing engine writer. This catalog append is
            # not presented as a distributed CAS/dispatch reservation.
            self.records.put('steering-pending',intent,at)
            try:
                result=await evaluator.execute(action,payload)
                if len(canonical(result).encode())>self.max_result_bytes:raise ValueError('steering result exceeds declared byte budget')
                receipt={**receipt,'result':result}
                receipt=copied(receipt)
                self.records.put('steering-complete',{'operationId':operation_id,'signature':signature,'receipt':receipt},datetime.now(timezone.utc).isoformat(timespec='microseconds'))
            except Exception as exc:
                raise PublicationError('steering outcome is uncertain; explicit reconciliation required',code='reconciliation_required') from exc
            return receipt
