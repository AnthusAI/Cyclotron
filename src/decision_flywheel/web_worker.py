"""Serialized web commands adapt to the existing reusable flywheel engine."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from threading import Event, Thread

from .classifier_config import ClassifierConfig
from .classification_metrics import classification_metrics
from .flywheel import DecisionFlywheel
from .reviewer_core import reviewer_item, reviewer_task
from .reviewer_flywheel import ReviewerFlywheel
from .reviewer_store import Article, ReviewStore
from .selection_policy import SelectionPolicy


class WebWorker:
    def __init__(self, store, directory, *, sink_factory, model_factory, articles=(), allow_live=False, redact=()):
        self.store, self.directory = store, Path(directory)
        self.sink_factory, self.model_factory = sink_factory, model_factory
        self.articles, self.allow_live, self.redact = tuple(articles), allow_live, tuple(redact)
        self.sessions, self.sinks = {}, {}
        self.stopping = Event()
        self.thread = None

    def create_run(self, name, config):
        if not self.allow_live:
            raise ValueError('live collection is not enabled')
        config = {**config}
        if set(config) - {'selection_policy','max_requests','max_optimizer_calls','optimize_every','rubric_changes_every',
                           'seed','decisions_model','optimizer_model'}:
            raise ValueError('unknown run configuration option')
        for key, default in (('max_requests',500),('max_optimizer_calls',10),('optimize_every',20),('rubric_changes_every',2)):
            value = config.setdefault(key,default)
            if type(value) is not int or value < 1:
                raise ValueError('run ceilings and cadences must be positive integers')
        config.setdefault('selection_policy',{'primary':'f1','positive_class':'include'})
        policy = SelectionPolicy(**config['selection_policy'])
        if policy.positive_class and policy.positive_class not in reviewer_task().labels:
            raise ValueError('unknown positive class')
        config['selection_policy'] = asdict(policy)
        config.setdefault('seed','arxiv-web-v1')
        config.setdefault('decisions_model','jev-1.13.0')
        config.setdefault('optimizer_model','gpt-6-luna')
        config['class_config'] = [{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}]
        config['dataset_fingerprint'] = hashlib.sha256(json.dumps(self.articles,sort_keys=True).encode()).hexdigest()
        return self.store.create_run(name,'live',config,items=self.articles)

    def start(self):
        self.store.recover_interrupted()
        self.thread = Thread(target=self._loop,daemon=True,name='flywheel-web-worker')
        self.thread.start()

    def _loop(self):
        try:
            while not self.stopping.is_set():
                job = self.store.claim_command()
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
            if any(key != run_id and value.current_cycle is not None for key,value in self.sessions.items()):
                raise ValueError('finish the current review before operating another live run')
            reviewer = self.session(run_id)
            payload, kind = job['payload'], job['kind']
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
            self.store.finish_command(job['id'],'failed',{'error_type':type(error).__name__,
                'reason':'command failed; state retained, no automatic paid retry'})
            self.store.set_status(run_id,'failed')
            if run_id in self.sessions:
                reviewer = self.sessions[run_id]
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
        count = sum(e['kind']=='human-feedback' and e.get('action')=='submitted' for e in reviewer.core.history(100000))
        for stage in ('questions','examples','classifier'):
            due = count > 0 and count % config['optimize_every'] == 0
            reviewer.current_cycle.check_trigger(stage,due=due,reason='label cadence reached' if due else 'label cadence not reached',
                details={'feedback_count':count,'threshold':config['optimize_every']})
            if due:
                reviewer.improve(stage=stage,trigger='feedback-cadence')

    @staticmethod
    def _metrics(reviewer):
        truth, labels, probabilities = [], [], []
        for vote in reviewer.store._active_actions().values():
            if vote.action != 'vote' or not vote.presentation_id:
                continue
            shown = next(p for p in reviewer.store.presentations_for(vote.article_id) if p.id == vote.presentation_id)
            truth.append(vote.label)
            labels.append(shown.predicted_label)
            probabilities.append({label:shown.confidence if label==shown.predicted_label else 1-shown.confidence
                                  for label in reviewer.core.initial.task.labels})
        metrics = classification_metrics(reviewer.core.initial.task.labels,truth,labels,probabilities)
        reviewer.core._emit({'kind':'cycle-metrics','metric_scope':'prequential reviewed predictions; not final held-out model accuracy',
                            'metrics':metrics})
