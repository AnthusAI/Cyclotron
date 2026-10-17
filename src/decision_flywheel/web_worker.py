"""Serialized web commands adapt to the existing reusable flywheel engine."""
import logging
import sqlite3
from pathlib import Path
from threading import Event, Thread

from .application_runtime import ArticleReviewRuntime
from .cyclotron_runtime import CyclotronRuntime, run_cyclotron_command


class WebWorker:
    def __init__(self, store, directory, *, sink_factory=None, model_factory=None,
                 articles=(), allow_live=False, redact=(), runtime=None):
        """Run API commands without making the worker own an application domain.

        ``runtime`` is the application seam: an article reviewer, another item
        source, or a future host can compose the reusable core and translate
        command results into item updates.  Without one, article review runs
        through ``ArticleReviewRuntime`` and catalog cyclotron runs through
        ``CyclotronRuntime``.
        """
        self.store, self.directory = store, Path(directory)
        self.sink_factory, self.model_factory = sink_factory, model_factory
        self.articles, self.allow_live, self.redact = tuple(articles), allow_live, tuple(redact)
        self.injected_runtime = runtime is not None
        self.runtime = runtime if runtime is not None else ArticleReviewRuntime(
            self.directory, sink_factory=sink_factory, model_factory=model_factory, redact=redact)
        self.sessions = {}
        self.session_runtimes = {}
        self.stopping = Event()
        self.thread = None

    def create_run(self, name, config):
        if not self.allow_live:
            raise ValueError('live collection is not enabled')
        if not self.injected_runtime and (
                config.get('cyclotron_id') or config.get('classifier_ids') or config.get('item_list_id')):
            return self._cyclotron_runtime().create_run(name, config)
        normalized = self.runtime.normalize_run_config(config, self.articles)
        return self.store.create_run(name, 'live', normalized, items=self.articles)

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
            reviewer.close()
        self.sessions.clear()
        self.session_runtimes.clear()

    def _runtime_session(self, run_id):
        if run_id in self.sessions:
            return self.sessions[run_id]
        run = self.store.run(run_id)
        config = run['config']
        if run['mode'] != 'live' or not self.allow_live:
            raise ValueError('run is read-only')
        runtime = self._cyclotron_runtime() if not self.injected_runtime and 'classifiers' in config else self.runtime
        session = runtime.open_session(
            run_id, config, self.store.items(run_id), self.store.current_item(run_id),
        )
        if not hasattr(session, 'close') or not hasattr(session, 'abort') or not hasattr(session, 'current_cycle'):
            raise ValueError('workspace runtime returned an invalid session')
        self.sessions[run_id] = session
        self.session_runtimes[run_id] = runtime
        return session

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
                if job['kind'] in ('label', 'skip') and command.prepares_next_item_after_feedback:
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
            raise ValueError('workspace session has no runtime to execute commands')
        except Exception as error:
            # Do not log exception text: providers can include secrets or prompts.
            import traceback
            locations=[{'module':Path(frame.filename).name,'function':frame.name,'line':frame.lineno}
                       for frame in traceback.extract_tb(error.__traceback__)]
            self.store.finish_command(job['id'],'failed',{'error_type':type(error).__name__,
                'code_locations':locations,
                'reason':'command failed; state retained, no automatic paid retry'})
            self.store.set_status(run_id,'failed')
            if run_id in self.session_runtimes:
                try:
                    self.sessions[run_id].abort(error)
                except Exception:
                    # API failure also prevents delivery of the failure event.
                    # Retain the failed job; never continue paid work or export
                    # a substitute history file.
                    pass
