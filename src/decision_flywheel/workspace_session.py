"""Source-neutral application coordinator; all learning uses DecisionFlywheel."""
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4
import hashlib
from copy import copy
from .classifier_config import ClassifierConfig
from .calibration_history import reviewed_calibration_metrics
from .feedback import FeedbackItem,LABEL_SOURCE_VETTED
from .feedback_trigger import LabelTransitionTrigger,PROTECTED_ASSIGNMENTS,learning_feedback
from .flywheel import DecisionFlywheel,development_assignment
from .models import DecisionTask,Item,LabeledItem
from .selection_policy import SelectionPolicy
from .shared_decisions import SharedDecisions


def freeze_configuration(store,config):
    config=dict(config)
    ids=config.pop('classifier_ids')
    if not ids or len(set(ids))!=len(ids): raise ValueError('choose distinct classifiers')
    revisions=config.pop('classifier_revisions',{})
    config['classifiers']=[store.classifier(identifier,revisions.get(identifier)) for identifier in ids]
    items=[];offset=0
    while page:=store.list_items(config['item_list_id'],after=offset):items.extend(page);offset+=len(page)
    if not items: raise ValueError('item list is empty')
    config['item_revisions']=[{'id':row['id'],'revision':row['revision'],'fingerprint':row['fingerprint']} for row in items]
    config['class_config']=config['classifiers'][0]['config']['classes']
    for classifier in config['classifiers']:
        definition=classifier['config']
        labels=[row['label'] for row in definition['classes']]
        task=DecisionTask(classifier['id'],tuple(labels),definition['question'],definition.get('input_field','text'))
        for item in items:task.validate_target(Item(item['id'],item['values']))
        policy=definition.get('selection_policy',config.get('selection_policy',{'primary':'f1','aggregation':'macro'}))
        positive=next((row['label'] for row in definition['classes'] if row.get('role')=='positive'),None)
        policy={**policy,'aggregation':'positive' if positive else 'macro','positive_class':positive}
        classifier['config']={**definition,'selection_policy':policy}
        if policy.get('positive_class') and policy['positive_class'] not in labels: raise ValueError('unknown positive class')
        SelectionPolicy(**policy)
    return config


class WorkspaceSession:
    current_cycle=None  # cycles are durably suspended between prediction and feedback
    def __init__(self,store,run,directory,model,optimizer,observer,*,redact=()):
        self.store,self.run,self.config,self.directory,self.observer=store,run,run['config'],directory,observer
        directory.mkdir(parents=True,exist_ok=True)
        self.items={row['id']:row for row in store.items(run['id'])}
        self.shared=SharedDecisions(directory/'shared.sqlite3',model,max_requests=self.config['max_requests'],observer=lambda event:self.emit('transport',event))
        self.wheels={}
        for classifier in self.config['classifiers']:
            identifier=classifier['id'];definition=classifier['config']
            task=DecisionTask(identifier,tuple(row['label'] for row in definition['classes']),definition['question'],definition.get('input_field','text'))
            path=hashlib.sha256(identifier.encode()).hexdigest()+'.sqlite3'
            # Each wheel owns its observer; only the paid transport and its
            # budget are shared. Reusing the agent chains every wheel's traces.
            self.wheels[identifier]=DecisionFlywheel(directory/path,ClassifierConfig(task),self.shared.adapter(identifier),copy(optimizer),
                max_requests=self.config['max_requests'],observer=lambda event,cid=identifier:self.emit(cid,event),redact=redact,
                selection_policy=SelectionPolicy(**definition.get('selection_policy',{'primary':'f1','aggregation':'macro'})),min_evaluation_per_class=2)
        transport=getattr(optimizer,'complete',None)
        if hasattr(transport,'calls'):transport.calls=sum(sum(e['kind']=='optimizer-request' for e in wheel.history(100000)) for wheel in self.wheels.values())

    def emit(self,identifier,event):
        event={**event,'classifier_id':identifier}
        if hasattr(self.observer,'ingest'):
            # Transport sequence is durable across restarts, independent of classifier event IDs.
            source=f'classifier:{identifier}:{event["event_id"]}' if 'event_id' in event else f'transport:{uuid4()}'
            self.observer.ingest(source,event)
        else:self.observer(event)

    def close(self):
        for wheel in self.wheels.values():wheel.close()
        self.shared.close()

    def skip(self,payload):
        current=self.store.current_item(self.run['id'])
        if not current or current['item']['id']!=payload.get('item_id'):raise ValueError('prepare this item first')
        if self.store.inherited_labels(self.run['id'],current['item']['id']):
            raise ValueError('complete missing historical labels before advancing to new items')
        target=Item(current['item']['id'],current['item']['values'])
        for wheel in self.wheels.values():
            cycle=wheel.resume_cycle(target)
            if cycle:
                wheel._emit({'kind':'human-skipped','target_id':target.id})
                cycle.__exit__(None,None,None)
        self.store.update_item(self.run['id'],target.id,reviewed=True)
        return {'skipped':target.id}

    def partitions(self,identifier):
        active={}
        for event in self.wheels[identifier].history(100000):
            if event['kind']=='human-feedback':
                feedback=event['feedback'];active.pop(feedback['item_id'],None)
                if event.get('action')=='submitted':active[feedback['item_id']]=event
        training=[];development=[];protected=[]
        task=self.wheels[identifier].initial.task
        for item_id,row in self.items.items():
            item=Item(item_id,row['values'])
            if item_id not in active or active[item_id].get('assignment') in PROTECTED_ASSIGNMENTS or development_assignment(self.config['seed']+':audit',item_id,rate=.2):
                protected.append(item);continue
            feedback=active[item_id]['feedback'];comment=feedback.get('edit_comment_value')
            labeled=LabeledItem(item,task.validate_label(feedback['final_answer_value']),'trusted',{'human_feedback':comment} if comment else {})
            (development if active[item_id].get('assignment')=='development' or development_assignment(self.config['seed'],item_id) else training).append(labeled)
        # Preserve feedback arrival order for recent-balanced evaluation selection.
        position={item_id:index for index,item_id in enumerate(active)}
        training.sort(key=lambda row:position[row.item.id]);development.sort(key=lambda row:position[row.item.id])
        return tuple(training),tuple(development),tuple(protected)

    async def prepare(self):
        current=self.store.current_item(self.run['id'])
        if current:return current
        row=next(iter(self.store.unreviewed_items(self.run['id'])),None)
        if row is None:return {'finished':True}
        target=Item(row['id'],row['values']);now=datetime.now(timezone.utc)
        training={};configs={}
        for identifier,wheel in self.wheels.items():
            train,dev,_=self.partitions(identifier);wheel.reconcile_feedback(train,development=dev)
            training[identifier]=train;configs[identifier]=wheel.active.config
        self.shared.bind_context(configs,training)
        # Refit stale heads inside the item's operational cycle. Sibling
        # context changes can alter probability features even when this
        # classifier's own question list stays unchanged.
        for _ in range(len(self.wheels)+1):
            stale=[identifier for identifier,wheel in self.wheels.items() if wheel.active.head and
                wheel.active.head.provenance.source_model_provenance!=wheel.model_context(wheel.active.config,training[identifier])]
            if not stale:break
            for identifier in stale:
                wheel=self.wheels[identifier]
                cycle=wheel.resume_cycle(target) or wheel.cycle(target).__enter__()
                try:
                    wheel.reconcile_model_context(training[identifier])
                    cycle.check_trigger('classifier',due=True,reason='shared decision feature context changed',details={})
                    train,dev,protected=self.partitions(identifier)
                    await wheel.step('classifier',train,dev,protected=protected,
                        propensities={r.item.id:1. for r in train},min_development_per_class=2,
                        limit=200,trigger='shared-context-change')
                    cycle.suspend()
                except Exception as error:
                    cycle.__exit__(type(error),error,None)
                    raise
            configs={identifier:wheel.active.config for identifier,wheel in self.wheels.items()}
            self.shared.bind_context(configs,training)
        # A bounded reconciliation must never serve an incompatible head.
        for identifier,wheel in self.wheels.items():wheel.reconcile_model_context(training[identifier])
        scorecard_fingerprint=self.store.checkpoint_scorecard(self.run['id'],self.wheels)
        await self.shared.prepare(configs,target,training,now=now)
        predictions={}
        for identifier,wheel in self.wheels.items():
            cycle=wheel.resume_cycle(target) or wheel.cycle(target).__enter__()
            try:
                result=await wheel.predict(target,training[identifier],now=now)
                name=next(c['name'] for c in self.config['classifiers'] if c['id']==identifier)
                predictions[identifier]={**asdict(result),'name':name,'version':wheel.active.fingerprint,'classes':list(wheel.initial.task.labels)}
                cycle.suspend()
            except Exception as error:
                cycle.__exit__(type(error),error,None);raise
        shown={'presentation_id':str(uuid4()),'classifiers':predictions,
               'scorecard_fingerprint':scorecard_fingerprint,
               'recorded_labels':self.store.inherited_labels(self.run['id'],target.id)}
        self.store.update_item(self.run['id'],target.id,prediction=shown)
        self.store.record_item_prediction(self.run['id'],self.config['item_list_id'],target.id,row['revision'],shown)
        return {'item':row,'prediction':shown}

    async def feedback(self,payload,request_id):
        current=self.store.current_item(self.run['id'])
        if not current or payload.get('item_id')!=current['item']['id'] or payload.get('presentation_id')!=current['prediction']['presentation_id']:
            raise ValueError('feedback must reference the displayed batch prediction')
        labels=payload.get('labels',[])
        if (not labels and self.config.get('input_mode')!='replay') or len({row['classifier_id'] for row in labels})!=len(labels):raise ValueError('choose distinct classifier labels')
        for row in labels:
            if row['classifier_id'] not in self.wheels:raise ValueError('classifier not in run')
            self.wheels[row['classifier_id']].initial.task.validate_label(row['label'])
            if not isinstance(row.get('comment',''),str):raise ValueError('comment must be text')
        item=current['item'];target=Item(item['id'],item['values']);warnings=[]
        inherited=self.store.inherited_labels(self.run['id'],item['id'])
        inherited_ids={row['classifier_id'] for row in inherited}
        if inherited_ids.intersection(row['classifier_id'] for row in labels):
            raise ValueError('existing labels are replayed; correct them separately')
        if inherited and set(self.wheels)-inherited_ids-set(row['classifier_id'] for row in labels):
            raise ValueError('provide every missing historical classifier label before continuing')
        labels=[*labels,*[{'classifier_id':row['classifier_id'],'label':row['label'],'comment':row['comment'],'source_request_id':row['request_id']} for row in inherited]]
        for label in labels:
            identifier=label['classifier_id'];wheel=self.wheels[identifier]
            feedback_id=f'{request_id}:{identifier}'
            existing=next((e['feedback'] for e in wheel.history(100000) if e['kind']=='human-feedback' and e['feedback']['id']==feedback_id),None)
            if existing:
                if existing['final_answer_value']!=label['label'] or (existing.get('edit_comment_value') or '')!=label.get('comment',''):
                    raise ValueError('feedback identity has different content')
                # The vote already reached the durable engine. Recovery never
                # reruns its optimization, including failed or paid work.
                cycle=wheel.resume_cycle(target)
                if cycle:
                    try:
                        self.metrics(identifier)
                        cycle.__exit__(None,None,None)
                    except Exception as error:
                        cycle.__exit__(type(error),error,None);raise
                continue
            cycle=wheel.resume_cycle(target)
            if cycle is None:raise ValueError('prediction cycle unavailable; explicit recovery required')
            try:
                classifier=next(c for c in self.config['classifiers'] if c['id']==identifier)
                if identifier not in inherited_ids:
                    self.store.label_item(identifier,classifier['revision'],self.config['item_list_id'],item['id'],item['revision'],label['label'],label.get('comment',''),feedback_id)
                feedback=FeedbackItem(feedback_id,item['id'],identifier,initial_answer_value=current['prediction']['classifiers'][identifier]['label'],
                    final_answer_value=label['label'],edit_comment_value=label.get('comment') or None,label_source=LABEL_SOURCE_VETTED,selection_propensity=1.,review_provenance=f"replayed-human-vote:{label['source_request_id']}" if identifier in inherited_ids else 'interactive-human-vote')
                role='scoreboard' if development_assignment(self.config['seed']+':audit',item['id'],rate=.2) else 'development' if development_assignment(self.config['seed'],item['id']) else 'training'
                wheel.record_feedback_event(feedback,assignment=role)
                from .observability import StepFailed
                try:
                    warnings.extend(await self.optimize(identifier,cycle) or [])
                except (StepFailed,RuntimeError) as error:
                    warning={'classifier_id':identifier,'reason':'optimization failed; labels retained, no automatic retry','error_type':type(error).__name__}
                    warnings.append(warning)
                    wheel._emit({'kind':'optimization-unavailable',**warning})
                self.metrics(identifier)
                cycle.__exit__(None,None,None)
            except Exception as error:
                cycle.__exit__(type(error),error,None);raise
        # Unlabeled classifier predictions are valid abstentions, not invented votes.
        for identifier,wheel in self.wheels.items():
            if identifier not in {row['classifier_id'] for row in labels}:
                cycle=wheel.resume_cycle(target)
                if cycle:cycle.__exit__(None,None,None)
        self.store.update_item(self.run['id'],item['id'],reviewed=True)
        self.store.checkpoint_scorecard(self.run['id'],self.wheels)
        return {'reviewed':item['id'],'optimization_warnings':warnings}

    async def resume_optimization(self):
        results=[]
        for identifier,wheel in self.wheels.items():
            feedback=next((e['feedback'] for e in reversed(wheel.history(100000)) if e['kind']=='human-feedback' and e.get('action')=='submitted'),None)
            if not feedback:continue
            target=self.items[feedback['item_id']]
            with wheel.cycle(Item(target['id'],target['values']),reason='optimization-resume') as cycle:
                warnings=await self.optimize(identifier,cycle,resume_stages=('rubric','questions','examples','classifier'))
                self.metrics(identifier)
                results.append({'classifier_id':identifier,'warnings':warnings})
        return {'resumed':results}

    def correct_feedback(self,payload,request_id):
        """Append an explicit correction and invalidate dependent learning.

        The original displayed prediction is immutable. Corrections do not
        silently rerun prediction or optimization, or change frozen replay
        inputs. The expected feedback ID protects against stale edits.
        """
        if self.config.get('input_mode')=='replay':
            raise ValueError('frozen replay feedback cannot be corrected; correct its source and create a new replay')
        item_id=payload.get('item_id');labels=payload.get('labels',[])
        if not request_id or item_id not in self.items or not labels or len({r['classifier_id'] for r in labels})!=len(labels):
            raise ValueError('correction needs an item, distinct classifier labels and a command identity')
        if any(row['id']==item_id for row in self.store.unreviewed_items(self.run['id'])):
            raise ValueError('finish recording this item before correcting its feedback')
        plans=[]
        for label in labels:
            identifier=label['classifier_id']
            if identifier not in self.wheels:raise ValueError('classifier not in run')
            wheel=self.wheels[identifier]
            canonical=wheel.initial.task.validate_label(label['label'])
            comment=label.get('comment','')
            if not isinstance(comment,str):raise ValueError('comment must be text')
            expected=label.get('expected_feedback_id')
            if not isinstance(expected,str) or not expected:raise ValueError('correction needs the expected feedback identity')
            history=wheel.history(100000);feedback_id=f'{request_id}:{identifier}'
            existing=next((e for e in history if e['kind']=='human-feedback' and e['feedback']['id']==feedback_id),None)
            if existing:
                if (existing['feedback']['item_id']!=item_id or existing['feedback']['final_answer_value']!=canonical or
                    (existing['feedback'].get('edit_comment_value') or '')!=comment or
                    existing['feedback'].get('review_provenance')!=f'corrected-human-vote:{expected}'):
                    raise ValueError('feedback identity has different content')
                original=existing
            else:
                original=next((e for e in reversed(history) if e['kind']=='human-feedback' and e['feedback']['item_id']==item_id),None)
                if not original or original.get('action')!='submitted':raise ValueError('item has no active feedback to correct')
                if expected!=original['feedback']['id']:
                    raise ValueError('feedback changed; reload before correcting')
            plans.append((identifier,wheel,canonical,comment,original,existing,feedback_id,expected))
        item=self.items[item_id];target=Item(item_id,item['values'])
        for identifier,wheel,canonical,comment,original,existing,feedback_id,expected in plans:
            if any(e['kind']=='feedback-correction-completed' and e.get('feedback_id')==feedback_id
                   for e in wheel.history(100000)):continue
            cycle=wheel.resume_cycle(target) if existing else None
            with_cycle=cycle or wheel.cycle(target,reason='human-correction').__enter__()
            try:
                classifier=next(c for c in self.config['classifiers'] if c['id']==identifier)
                self.store.label_item(identifier,classifier['revision'],self.config['item_list_id'],item_id,item['revision'],
                                      canonical,comment,feedback_id)
                if not existing:
                    prior=original['feedback']
                    wheel.record_feedback_event(FeedbackItem(feedback_id,item_id,identifier,
                        initial_answer_value=prior.get('initial_answer_value'),final_answer_value=canonical,
                        edit_comment_value=comment or None,label_source=prior['label_source'],
                        selection_propensity=prior.get('selection_propensity'),
                        review_provenance=f"corrected-human-vote:{prior['id']}"),assignment=original.get('assignment'))
                training,development,_=self.partitions(identifier)
                wheel.reconcile_feedback(training,development=development)
                wheel.set_optimizer_context([r.context['human_feedback'] for r in training if r.context.get('human_feedback')])
                self.metrics(identifier)
                wheel._emit({'kind':'feedback-correction-completed','feedback_id':feedback_id,
                             'target_id':item_id,'previous_feedback_id':expected})
                with_cycle.__exit__(None,None,None)
            except Exception as error:
                with_cycle.__exit__(type(error),error,None);raise
        self.store.checkpoint_scorecard(self.run['id'],self.wheels)
        return {'corrected':item_id,'classifier_ids':[r[0] for r in plans]}

    def undo_feedback(self,request_id):
        """Retract the last reviewed item's local labels and reopen its display."""
        if not isinstance(request_id,str) or not request_id:raise ValueError('undo needs a command identity')
        if self.config.get('input_mode')=='replay':raise ValueError('frozen replay feedback cannot be undone')
        owner=next(iter(self.wheels.values()))
        history=owner.history(100000)
        completed=next((e for e in history if e['kind']=='feedback-undo-completed' and e.get('request_id')==request_id),None)
        if completed:return completed['result']
        plan=next((e for e in history if e['kind']=='feedback-undo-started' and e.get('request_id')==request_id),None)
        if plan is None:
            active={}
            for identifier,wheel in self.wheels.items():
                for event in wheel.history(100000):
                    if event['kind']!='human-feedback':continue
                    key=(identifier,event['feedback']['item_id'])
                    active.pop(key,None)
                    if event.get('action')=='submitted':active[key]=event
            local={key:event for key,event in active.items()
                   if not (event['feedback'].get('review_provenance') or '').startswith('replayed-human-vote:')}
            if not local:return {'undone':None}
            latest=max(local.values(),key=lambda e:e['created_at'])
            item_id=latest['feedback']['item_id']
            if any(row['id']==item_id for row in self.store.unreviewed_items(self.run['id'])):
                raise ValueError('finish recording this item before undoing its feedback')
            targets=[{'classifier_id':cid,'feedback_id':event['feedback']['id']}
                     for (cid,item),event in local.items() if item==item_id]
            plan=owner._emit({'kind':'feedback-undo-started','request_id':request_id,'target_id':item_id,'targets':targets})
        item_id=plan['target_id'];item=self.items[item_id];target=Item(item_id,item['values'])
        for ref in plan['targets']:
            identifier=ref['classifier_id'];wheel=self.wheels[identifier];history=wheel.history(100000)
            if any(e['kind']=='feedback-retraction-completed' and e.get('request_id')==request_id for e in history):continue
            original=next(e for e in history if e['kind']=='human-feedback' and e['feedback']['id']==ref['feedback_id'])
            current=next(e for e in reversed(history) if e['kind']=='human-feedback' and e['feedback']['item_id']==item_id)
            if current['feedback']['id']!=ref['feedback_id']:raise ValueError('feedback changed; cannot resume stale undo')
            with wheel.cycle(target,reason='human-retraction'):
                self.store.retract_item_label(ref['feedback_id'],f'{request_id}:{identifier}')
                if current.get('action')!='retracted':
                    wheel.record_feedback_event(FeedbackItem(**original['feedback']),action='retracted',assignment=original.get('assignment'))
                training,development,_=self.partitions(identifier)
                wheel.reconcile_feedback(training,development=development)
                wheel.set_optimizer_context([r.context['human_feedback'] for r in training if r.context.get('human_feedback')])
                self.metrics(identifier)
                wheel._emit({'kind':'feedback-retraction-completed','request_id':request_id,'feedback_id':ref['feedback_id']})
        self.store.update_item(self.run['id'],item_id,reviewed=False)
        # The saved pre-vote prediction is still the thing the human reviews.
        # Do not invent a new prediction or make a model call just to undo.
        for wheel in self.wheels.values():
            cycle=wheel.resume_cycle(target) or wheel.cycle(target,reason='review-after-undo').__enter__()
            try:
                prediction=next(e for e in reversed(wheel.history(100000)) if e['kind']=='prediction' and e['target_id']==item_id)
                wheel._emit({'kind':'displayed-prediction-reused','target_id':item_id,
                             'prediction_event_id':prediction['event_id'],'reason':'review after undo'})
                cycle.suspend()
            except Exception as error:
                cycle.__exit__(type(error),error,None);raise
        self.store.checkpoint_scorecard(self.run['id'],self.wheels)
        result={'undone':item_id,'classifier_ids':[row['classifier_id'] for row in plan['targets']]}
        owner._emit({'kind':'feedback-undo-completed','request_id':request_id,'result':result})
        return result

    async def optimize(self,identifier,cycle,*,resume_stages=()):
        warnings=[]
        wheel=self.wheels[identifier];training,development,protected=self.partitions(identifier)
        wheel.reconcile_feedback(training,development=development)
        wheel.set_optimizer_context([row.context['human_feedback'] for row in training if row.context.get('human_feedback')])
        history=wheel.history(100000)
        latest=next((e for e in reversed(history) if e['kind']=='human-feedback'),None)
        eligible=bool(latest and learning_feedback(latest) and latest.get('action')=='submitted')
        check=LabelTransitionTrigger(self.config['rubric_changes_every']).check(history)
        cycle.check_trigger('rubric',**check)
        count=len(training)+len(development)
        stages=['rubric'] if check['due'] else []
        for stage in ('questions','examples','classifier'):
            due=eligible and count>0 and count%self.config['optimize_every']==0
            cycle.check_trigger(stage,due=due,reason='feedback cadence reached' if due else 'feedback cadence not reached',details={'feedback_count':count,'threshold':self.config['optimize_every']})
            if due:stages.append(stage)
        stages=list(dict.fromkeys([*stages,*resume_stages]))
        for stage in stages:
            self.shared.bind_context({cid:w.active.config for cid,w in self.wheels.items()},
                {cid:self.partitions(cid)[0] for cid in self.wheels})
            transport=getattr(wheel.optimizer,'complete',None)
            if stage!='classifier' and hasattr(transport,'max_calls') and transport.calls>=transport.max_calls:
                wheel._emit({'kind':'optimization-paused','stage':stage,'reason':'optimizer call limit reached','calls':transport.calls,'limit':transport.max_calls})
                warnings.append({'classifier_id':identifier,'reason':'optimizer call limit reached; labeling can continue'})
                continue
            result=await wheel.step(stage,training,development,protected=protected,propensities={row.item.id:1. for row in training},min_development_per_class=2,limit=200,trigger='label-transitions' if stage=='rubric' else 'feedback-cadence')
            if result['status'] not in ('completed','waiting'):raise RuntimeError('optimization step did not complete')
            if stage in ('rubric','questions','examples') and result['status']=='completed':
                await wheel.step('classifier',training,development,protected=protected,propensities={row.item.id:1. for row in training},min_development_per_class=2,trigger='context-handoff')
        return warnings

    def metrics(self,identifier):
        wheel=self.wheels[identifier]
        definition=next(row for row in self.config['classifiers'] if row['id']==identifier)
        wheel._emit({'kind':'cycle-metrics','class_config':definition['config']['classes'],'metric_scope':'latest 200 human-labeled items; prequential predictions, not protected evaluation','metrics':reviewed_calibration_metrics(wheel.initial.task.labels,wheel.history(100000))})
