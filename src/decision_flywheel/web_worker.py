"""Serialized web commands adapt to the existing reusable flywheel engine."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from threading import Event, Thread

from .classifier_config import ClassifierConfig
from .calibration_history import reviewed_calibration_metrics
from .flywheel import DecisionFlywheel
from .reviewer_core import reviewer_item, reviewer_task
from .reviewer_flywheel import ReviewerFlywheel
from .reviewer_store import Article, ReviewStore
from .selection_policy import SelectionPolicy
from .feedback_trigger import learning_feedback


class WebWorker:
    def __init__(self, store, directory, *, sink_factory, model_factory, articles=(), allow_live=False, redact=()):
        self.store, self.directory = store, Path(directory)
        self.sink_factory, self.model_factory = sink_factory, model_factory
        self.articles, self.allow_live, self.redact = tuple(articles), allow_live, tuple(redact)
        self.sessions, self.sinks = {}, {}
        self.stopping = Event()
        self.thread = None

    def create_run(self, name, config):
        config,items=self._run_inputs(config)
        return self.store.create_run(name,'live',config,items=items)

    def create_comparison(self,name,before_run_id,after_run_id,approved_fingerprint,*,max_requests,limit=200):
        from .matched_run_plan import plan_matched_runs
        from .matched_run_evaluation import _endpoint
        if not self.allow_live:raise ValueError('comparison execution requires live-call authority')
        endpoints=self.store.matched_run_sources(before_run_id,after_run_id)
        plan=plan_matched_runs(**endpoints,limit=limit)
        if plan['fingerprint']!=approved_fingerprint:raise ValueError('preflight changed; approve a new plan')
        if not plan['sample_count']:raise ValueError('no protected matched samples available')
        if type(max_requests) is not int or max_requests<plan['request_upper_bound']:
            raise ValueError('request ceiling is below preflight upper bound')
        for source in endpoints.values():_endpoint(source)
        return self.store.create_matched_evaluation(name,plan,endpoints,max_requests)

    def resume_comparison(self,run_id,request_id,*,max_requests,retry_failed=False):
        if not self.allow_live:raise ValueError('comparison resume requires live-call authority')
        return self.store.resume_matched_evaluation(run_id,request_id,max_requests=max_requests,retry_failed=retry_failed)

    def _evaluate_comparison(self,run_id,authorization):
        import asyncio
        from .matched_run_evaluation import evaluate_matched_runs
        run=self.store.run(run_id);config=run['config']
        if not self.allow_live or config.get('input_mode')!='comparison':
            raise ValueError('comparison execution is not authorized')
        endpoints=self.store.matched_evaluation_inputs(run_id)
        models={side:self.model_factory(source['config'])[0] for side,source in endpoints.items()}
        return asyncio.run(evaluate_matched_runs(config['plan'],endpoints,models,self.directory/run_id,
            max_requests=authorization.get('max_requests',config['max_requests']),
            retry_failed=authorization.get('retry_failed',False),observer=self.sink_factory(run_id)))

    def _run_inputs(self, config):
        """Validate and freeze inputs without writing a run or making model calls."""
        if not self.allow_live:
            raise ValueError('live collection is not enabled')
        config = {**config}
        if set(config) - {'selection_policy','max_requests','max_optimizer_calls','optimize_every','rubric_changes_every',
                           'seed','decisions_model','optimizer_model','classifier_ids','item_list_id','scorecard_id','scorecard_definition_revision'}:
            raise ValueError('unknown run configuration option')
        if config.get('scorecard_id'):
            if config.get('classifier_ids'):
                raise ValueError('select a scorecard or independent classifiers, not both')
            definition=self.store.scorecard_definition(config['scorecard_id'],config.get('scorecard_definition_revision'))
            allowed_settings={'selection_policy','seed','decisions_model','optimizer_model','optimize_every','rubric_changes_every'}
            config={**{key:value for key,value in definition['settings'].items() if key in allowed_settings},**config}
            config['scorecard_definition_revision']=definition['revision']
            config['scorecard_definition_fingerprint']=definition['fingerprint']
            config['classifier_ids']=[row['id'] for row in definition['classifiers']]
            config['classifier_revisions']={row['id']:row['revision'] for row in definition['classifiers']}
        for key, default in (('max_requests',500),('max_optimizer_calls',1000),('optimize_every',20),('rubric_changes_every',2)):
            value = config.setdefault(key,default)
            if type(value) is not int or value < 1:
                raise ValueError('run ceilings and cadences must be positive integers')
        config.setdefault('selection_policy',{'primary':'f1','positive_class':'include'})
        policy = SelectionPolicy(**config['selection_policy'])
        if not config.get('classifier_ids') and policy.positive_class and policy.positive_class not in reviewer_task().labels:
            raise ValueError('unknown positive class')
        config['selection_policy'] = asdict(policy)
        config.setdefault('seed','arxiv-web-v1')
        config.setdefault('decisions_model','jev-1.13.0')
        config.setdefault('optimizer_model','gpt-6-luna')
        config['evaluation_protocol']='protected-feedback-v1'
        if 'classifier_ids' in config or 'item_list_id' in config:
            if not config.get('classifier_ids') or not config.get('item_list_id'):
                raise ValueError('choose classifiers and an item list')
            from .workspace_session import freeze_configuration
            config=freeze_configuration(self.store,config)
            items=[];offset=0
            while page:=self.store.list_items(config['item_list_id'],after=offset):items.extend(page);offset+=len(page)
            return config,items
        config['class_config'] = [{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}]
        config['dataset_fingerprint'] = hashlib.sha256(json.dumps(self.articles,sort_keys=True).encode()).hexdigest()
        return config,self.articles

    def start(self):
        self.store.recover_interrupted()
        self.thread = Thread(target=self._loop,daemon=True,name='flywheel-web-worker')
        self.thread.start()

    def create_replay(self,name,source_run_id,config):
        """Freeze source feedback before creating a new, empty learning session."""
        source=self.store.run(source_run_id)
        if not config.get('scorecard_id'):raise ValueError('choose a versioned scorecard for replay')
        definition=self.store.scorecard_definition(config['scorecard_id'],config.get('scorecard_definition_revision'))
        refs=definition['classifiers'];source_refs={row['id']:row['revision'] for row in source['config'].get('classifiers',[])}
        if any(source_refs.get(row['id'])!=row['revision'] for row in refs):
            raise ValueError('source feedback must match every classifier definition; collect missing labels first')
        votes={};order=[]
        for row in self.store.all_events(source_run_id):
            event=row['payload']
            if event.get('kind')!='human-feedback' or event.get('classifier_id') not in source_refs:continue
            feedback=event['feedback'];identifier=feedback['item_id'];key=(identifier,event['classifier_id'])
            if event.get('action')=='retracted':votes.pop(key,None);continue
            if identifier not in order:order.append(identifier)
            votes[key]={'classifier_id':event['classifier_id'],'label':feedback['final_answer_value'],
                        'comment':feedback.get('edit_comment_value') or '', 'request_id':feedback['id']}
        source_items={row['id']:row for row in self.store.items(source_run_id)}
        eligible=[identifier for identifier in order if identifier in source_items and all((identifier,ref['id']) in votes for ref in refs)]
        if not eligible:raise ValueError('no fully labeled source items available for this scorecard')
        # Creation validates providers, policies and all operating ceilings;
        # it never prepares an item or instantiates a model client.
        validated,_=self._run_inputs({**config,'item_list_id':source['config']['item_list_id']})
        frozen={**validated,'input_mode':'replay','source_run_id':source_run_id,
                'learning_policy':'fresh-replay','replay_count':len(eligible)}
        frozen['item_revisions']=[{'id':source_items[key]['id'],'revision':source_items[key]['revision'],'fingerprint':source_items[key]['fingerprint']} for key in eligible]
        return self.store.create_run(name,'live',frozen,items=[source_items[identifier] for identifier in eligible],
            frozen_feedback=[(identifier,ref['id'],votes[(identifier,ref['id'])]) for identifier in eligible for ref in refs],
            initial_events=[('replay-created',{'kind':'replay-created','source_run_id':source_run_id,
                            'sample_count':len(eligible),'initial_state':'empty context and no fitted heads'})])

    def _loop(self):
        try:
            while not self.stopping.is_set():
                try:
                    job = self.store.claim_command()
                except sqlite3.OperationalError:
                    # Only retry claiming persisted work, never a provider call
                    # or partially applied feedback. Each claim opens a fresh
                    # connection, so transient storage failures can recover.
                    logging.getLogger(__name__).warning('Command queue unavailable; retrying storage access')
                    self.stopping.wait(1)
                    continue
                if job:
                    self.process(job)
                else:
                    self.stopping.wait(.1)
        finally:
            self._close_sessions()

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=5)
        else:
            self._close_sessions()

    def _close_sessions(self):
        for reviewer in self.sessions.values():
            # Shutdown is not a completed review. An open cycle remains visible
            # as incomplete, rather than inventing a completion event.
            if hasattr(reviewer,'wheels'):reviewer.close()
            else:
                reviewer.core.close()
                reviewer.store.close()
        self.sessions.clear()

    def session(self, run_id):
        if run_id in self.sessions:
            return self.sessions[run_id]
        run = self.store.run(run_id)
        config = run['config']
        if run['mode'] != 'live' or not self.allow_live:
            raise ValueError('run is read-only')
        directory = self.directory / run_id
        directory.mkdir(parents=True,exist_ok=True)
        if 'classifiers' in config:
            from .workspace_session import WorkspaceSession
            model,optimizer=self.model_factory(config)
            reviewer=WorkspaceSession(self.store,run,directory,model,optimizer,self.sink_factory(run_id),redact=self.redact)
            self.sessions[run_id]=reviewer
            return reviewer
        reviews = ReviewStore(directory / 'reviews.sqlite3',study_seed=config['seed'])
        reviews.import_articles(tuple(Article(**item) for item in self.store.items(run_id)))
        sink = self.sink_factory(run_id)
        model, optimizer = self.model_factory(config)
        core = DecisionFlywheel(directory / 'runtime.sqlite3',ClassifierConfig(reviewer_task()),model,optimizer,
            observer=sink,max_requests=config['max_requests'],selection_policy=SelectionPolicy(**config['selection_policy']),
            min_evaluation_per_class=2,redact=self.redact)
        # Preserve cumulative paid ceilings across clean restarts. Failed work
        # remains interrupted; it is never automatically resubmitted.
        events = [json.loads(row[0]) for row in core.db.execute('SELECT payload FROM runtime_events')]
        core.requests = sum(e['kind']=='features-requested' for e in events)
        transport = getattr(optimizer,'complete',None)
        if hasattr(transport,'calls'):
            transport.calls = sum(e['kind']=='optimizer-request' for e in events)
        reviewer = ReviewerFlywheel(reviews,core,min_stage_evaluation_per_class=2,
            rubric_changes_every=config['rubric_changes_every'],include_protected_guidance=False)
        current = self.store.current_item(run_id)
        if current and current['prediction']['version']==core.active.fingerprint:
            reviewer.current_cycle = core.resume_cycle(reviewer_item(reviews.article(current['item']['id'])))
        self.sessions[run_id], self.sinks[run_id] = reviewer, sink
        return reviewer

    def process(self, job):
        run_id = job['run_id']
        self.store.set_status(run_id,'working')
        try:
            if job['kind']=='matched-evaluate':
                result=self._evaluate_comparison(run_id,job['payload'])
                self.store.finish_command(job['id'],'completed',result)
                self.store.set_status(run_id,'completed')
                return
            if any(key != run_id and value.current_cycle is not None for key,value in self.sessions.items()):
                raise ValueError('finish the current review before operating another live run')
            reviewer = self.session(run_id)
            if hasattr(reviewer,'wheels'):
                config=self.store.run(run_id)['config']
                reviewer.config=config
                reviewer.shared.max_requests=config['max_requests']
                for wheel in reviewer.wheels.values():
                    wheel.max_requests=config['max_requests']
                    transport=getattr(wheel.optimizer,'complete',None)
                    if hasattr(transport,'max_calls'):transport.max_calls=config['max_optimizer_calls']
            payload, kind = job['payload'], job['kind']
            if hasattr(reviewer,'wheels'):
                import asyncio
                if kind=='prepare':result=asyncio.run(reviewer.prepare())
                elif kind=='label':result=asyncio.run(reviewer.feedback(payload,job['request_id']))
                elif kind=='skip':result=reviewer.skip(payload)
                elif kind=='optimize':result=asyncio.run(reviewer.resume_optimization())
                elif kind=='replay-next':
                    if config.get('input_mode')!='replay':raise ValueError('this run is not a replay')
                    shown=asyncio.run(reviewer.prepare())
                    if shown.get('finished'):result=shown
                    else:result=asyncio.run(reviewer.feedback({'item_id':shown['item']['id'],'presentation_id':shown['prediction']['presentation_id'],'labels':[]},f"replay:{shown['item']['id']}"))
                else:raise ValueError('use catalog label correction; automatic learning rollback is not supported')
                self.store.finish_command(job['id'],'completed',result)
                self.store.set_status(run_id,'ready')
                if kind in ('label','skip'):
                    # Feedback is acknowledged separately from the next paid
                    # prediction. A preparation failure cannot turn saved
                    # labels into a failed vote or cause duplicate feedback.
                    self.store.command(run_id,f"after-feedback:{job['id']}",'prepare',{})
                return
            if kind == 'prepare':
                current = self.store.current_item(run_id)
                if current and reviewer.current_cycle is not None:
                    result = current
                else:
                    article = reviewer.store.next_unreviewed()
                    if article is None:
                        result = {'finished':True}
                    else:
                        prediction = reviewer.predict(article)
                        shown = reviewer.store.record_prediction(article.id,prediction.label,prediction.confidence,
                            prediction.kind,prediction.fingerprint,prediction.training_label_count)
                        value = {'label':prediction.label,'confidence':prediction.confidence,
                                 'presentation_id':shown.id,'version':prediction.fingerprint}
                        self.store.update_item(run_id,article.id,prediction=value)
                        result = {'item':asdict(article),'prediction':value}
            elif kind in ('label','skip'):
                current = self.store.current_item(run_id)
                if not current or payload.get('item_id') != current['item']['id'] or reviewer.current_cycle is None:
                    raise ValueError('prepare this item before submitting feedback')
                item_id = current['item']['id']
                if kind == 'label':
                    if payload.get('presentation_id') != current['prediction']['presentation_id']:
                        raise ValueError('feedback must reference the displayed prediction')
                    event = reviewer.store.record_vote(item_id,payload['label'],comment=payload.get('comment') or None,
                        presentation_id=payload['presentation_id'])
                    self.store.update_item(run_id,item_id,reviewed=True)
                    reviewer.record_review_event(event)
                    reviewer.reconcile()
                    self._optimize(reviewer,self.store.run(run_id)['config'])
                    self._metrics(reviewer)
                else:
                    reviewer.store.record_skip(item_id)
                    reviewer.core._emit({'kind':'human-skipped','target_id':item_id})
                reviewer.finish_cycle()
                self.store.update_item(run_id,item_id,reviewed=True)
                result = {'reviewed':item_id}
            else:
                reviewer.finish_cycle()
                article = reviewer.store.undo_last_vote()
                if article:
                    event = reviewer.store.events_for(article.id)[-1]
                    with reviewer.core.cycle(reviewer_item(article),reason='human-correction'):
                        reviewer.record_review_event(event)
                        reviewer.reconcile()
                        self._metrics(reviewer)
                    self.store.update_item(run_id,article.id,reviewed=False)
                result = {'undone':article.id if article else None}
            self.store.finish_command(job['id'],'completed',result)
            self.store.set_status(run_id,'ready')
        except Exception as error:
            # Do not log exception text: providers can include secrets or prompts.
            import traceback
            locations=[{'module':Path(frame.filename).name,'function':frame.name,'line':frame.lineno}
                       for frame in traceback.extract_tb(error.__traceback__)]
            self.store.finish_command(job['id'],'failed',{'error_type':type(error).__name__,
                'code_locations':locations,
                'reason':'command failed; state retained, no automatic paid retry'})
            self.store.set_status(run_id,'failed')
            if run_id in self.sessions:
                reviewer = self.sessions[run_id]
                if hasattr(reviewer,'wheels'):return
                cycle, reviewer.current_cycle = reviewer.current_cycle, None
                if cycle and cycle.token is not None:
                    try:
                        cycle.__exit__(type(error),error,None)
                    except Exception:
                        # API failure also prevents delivery of the failure
                        # event. Retain the failed job; never continue paid work
                        # or export a substitute history file.
                        pass

    def _optimize(self, reviewer, config):
        if reviewer.feedback_trigger(config['optimize_every']):
            reviewer.improve(stage='rubric',trigger='label-transitions')
        history=reviewer.core.history(100000)
        latest=next((e for e in reversed(history) if e['kind']=='human-feedback'),None)
        eligible=bool(latest and learning_feedback(latest) and latest.get('action')=='submitted')
        count = sum(learning_feedback(e) and e.get('action')=='submitted' for e in history)
        for stage in ('questions','examples','classifier'):
            due = eligible and count > 0 and count % config['optimize_every'] == 0
            reviewer.current_cycle.check_trigger(stage,due=due,reason='label cadence reached' if due else 'label cadence not reached',
                details={'feedback_count':count,'threshold':config['optimize_every']})
            if due:
                reviewer.improve(stage=stage,trigger='feedback-cadence')

    @staticmethod
    def _metrics(reviewer):
        metrics = reviewed_calibration_metrics(reviewer.core.initial.task.labels,reviewer.core.history(100000))
        reviewer.core._emit({'kind':'cycle-metrics','metric_scope':'latest 200 human-labeled items; prequential predictions, not final held-out model accuracy',
                            'metrics':metrics})
