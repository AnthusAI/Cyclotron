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
from .cyclotron_runtime import CyclotronRuntime, run_cyclotron_command


class WebWorker:
    def __init__(self, store, directory, *, sink_factory=None, model_factory=None,
                 articles=(), allow_live=False, redact=(), runtime=None):
        """Run API commands without making the worker own an application domain.

        ``runtime`` is the application seam: an article reviewer, another item
        source, or a future host can compose the reusable core and translate
        command results into item updates.  The legacy reviewer path remains
        the default until the cyclotron workspace is moved behind the same
        seam; it must not change active study behaviour during that migration.
        """
        self.store, self.directory = store, Path(directory)
        self.sink_factory, self.model_factory = sink_factory, model_factory
        self.articles, self.allow_live, self.redact = tuple(articles), allow_live, tuple(redact)
        self.runtime = runtime
        self.sessions, self.sinks = {}, {}
        self.session_runtimes = {}
        self.stopping = Event()
        self.thread = None

    def create_run(self, name, config):
        if self.runtime is not None:
            if not self.allow_live:
                raise ValueError('live collection is not enabled')
            normalized = self.runtime.normalize_run_config(config, self.articles)
            return self.store.create_run(name, 'live', normalized, items=self.articles)
        if config.get('cyclotron_id') or config.get('classifier_ids') or config.get('item_list_id'):
            if not self.allow_live:
                raise ValueError('live collection is not enabled')
            return self._cyclotron_runtime().create_run(name, config)
        config,items=self._run_inputs(config)
        return self.store.create_run(name,'live',config,items=items)

    def _cyclotron_runtime(self):
        return CyclotronRuntime(self.store, self.directory, model_factory=self.model_factory,
                                sink_factory=self.sink_factory, redact=self.redact)

    def create_comparison(self,name,before_run_id,after_run_id,approved_fingerprint,*,max_requests,limit=200,request_id=None):
        from .matched_run_plan import plan_matched_runs
        from .matched_run_evaluation import _endpoint
        if not self.allow_live:raise ValueError('comparison execution requires live-call authority')
        authorization={'name':name,'before_run_id':before_run_id,'after_run_id':after_run_id,
                       'approved_fingerprint':approved_fingerprint,'max_requests':max_requests,'limit':limit}
        existing=self.store.matched_evaluation_authorization(request_id,authorization)
        if existing is not None:return existing
        endpoints=self.store.matched_run_sources(before_run_id,after_run_id)
        plan=plan_matched_runs(**endpoints,limit=limit)
        if plan['fingerprint']!=approved_fingerprint:raise ValueError('preflight changed; approve a new plan')
        if not plan['sample_count']:raise ValueError('no protected matched samples available')
        if type(max_requests) is not int or max_requests<plan['request_upper_bound']:
            raise ValueError('request ceiling is below preflight upper bound')
        for source in endpoints.values():_endpoint(source)
        return self.store.create_matched_evaluation(name,plan,endpoints,max_requests,
            request_id=request_id,authorization=authorization)

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
                           'seed','decisions_model','optimizer_model','optimizer_transport'}:
            raise ValueError('unknown run configuration option')
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
        from .adapters.optimizer_transport import validate_optimizer_transport
        config['optimizer_transport']=validate_optimizer_transport(config.get('optimizer_transport','openai'))
        config['evaluation_protocol']='protected-feedback-v1'
        config['class_config'] = [{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}]
        config['dataset_fingerprint'] = hashlib.sha256(json.dumps(self.articles,sort_keys=True).encode()).hexdigest()
        return config,self.articles

    def start(self):
        self.store.recover_interrupted()
        self.thread = Thread(target=self._loop,daemon=True,name='flywheel-web-worker')
        self.thread.start()

    def create_replay(self,name,source_run_id,config):
        """Freeze source feedback before creating a new, empty learning session."""
        if not self.allow_live:
            raise ValueError('live collection is not enabled')
        return self._cyclotron_runtime().create_replay(name, source_run_id, config)

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
        for run_id, reviewer in self.sessions.items():
            # Shutdown is not a completed review. An open cycle remains visible
            # as incomplete, rather than inventing a completion event.
            if run_id in self.session_runtimes:reviewer.close()
            elif hasattr(reviewer,'wheels'):reviewer.close()
            else:
                reviewer.core.close()
                reviewer.store.close()
        self.sessions.clear()
        self.session_runtimes.clear()

    def _runtime_session(self, run_id):
        if run_id in self.sessions:
            return self.sessions[run_id]
        run = self.store.run(run_id)
        config = run['config']
        if run['mode'] != 'live' or not self.allow_live:
            raise ValueError('run is read-only')
        runtime = self.runtime
        if runtime is None and 'classifiers' in config:
            runtime = self._cyclotron_runtime()
        if runtime is not None:
            session = runtime.open_session(
                run_id, config, self.store.items(run_id), self.store.current_item(run_id),
            )
            if not hasattr(session, 'close') or not hasattr(session, 'abort') or not hasattr(session, 'current_cycle'):
                raise ValueError('workspace runtime returned an invalid session')
            self.sessions[run_id] = session
            self.session_runtimes[run_id] = runtime
            return session
        directory = self.directory / run_id
        directory.mkdir(parents=True,exist_ok=True)
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

    # Kept for existing application callers while the API boundary migrates.
    session = _runtime_session

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
            reviewer = self._runtime_session(run_id)
            runtime = self.session_runtimes.get(run_id)
            if runtime is not None:
                current = self.store.current_item(run_id)
                config = self.store.run(run_id)['config']
                if isinstance(runtime, CyclotronRuntime):
                    command = run_cyclotron_command(runtime, reviewer, job['kind'], job['payload'], current,
                                                     config, request_id=job['request_id'])
                else:
                    command = runtime.execute(reviewer, job['kind'], job['payload'], current, config)
                for update in command.updates:
                    changes = {}
                    if update.prediction is not None:
                        changes['prediction'] = update.prediction
                    if update.reviewed is not None:
                        changes['reviewed'] = update.reviewed
                    self.store.update_item(run_id, update.item_id, **changes)
                self.store.finish_command(job['id'], 'completed', dict(command.result))
                self.store.set_status(run_id, 'completed' if command.result.get('finished') is True else 'ready')
                if job['kind'] in ('label', 'skip'):
                    self.store.command(run_id, f"after-feedback:{job['id']}", 'prepare', {})
                return
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
                elif kind=='correct':result=reviewer.correct_feedback(payload,job['request_id'])
                elif kind=='undo':
                    if payload:raise ValueError('cyclotron undo needs an empty payload')
                    result=reviewer.undo_feedback(job['request_id'])
                elif kind=='optimize':result=asyncio.run(reviewer.resume_optimization())
                elif kind=='replay-next':
                    if config.get('input_mode')!='replay':raise ValueError('this run is not a replay')
                    shown=asyncio.run(reviewer.prepare())
                    if shown.get('finished'):result=shown
                    else:result=asyncio.run(reviewer.feedback({'item_id':shown['item']['id'],'presentation_id':shown['prediction']['presentation_id'],'labels':[]},f"replay:{shown['item']['id']}"))
                else:raise ValueError('use catalog label correction; automatic learning rollback is not supported')
                self.store.finish_command(job['id'],'completed',result)
                self.store.set_status(run_id,'completed' if result.get('finished') is True else 'ready')
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
            elif kind == 'undo':
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
            else:
                raise ValueError('unknown legacy workspace command')
            self.store.finish_command(job['id'],'completed',result)
            self.store.set_status(run_id,'completed' if result.get('finished') is True else 'ready')
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
                if run_id in self.session_runtimes:
                    try:
                        reviewer.abort(error)
                    except Exception:
                        pass
                    return
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
