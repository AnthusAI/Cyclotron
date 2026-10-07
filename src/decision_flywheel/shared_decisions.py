"""Durable complete-request batch cache and a run-wide paid-attempt ceiling."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from .batched_classification import batch_request
from .classifier_config import ClassifiedAnswers
from .decision_cache import CacheOptions, CacheMiss
from .models import DecisionResult


def key(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',', ':'),allow_nan=False).encode()).hexdigest()


class SharedDecisions:
    def __init__(self,path,model,*,max_requests,observer):
        self.model,self.max_requests,self.observer=model,max_requests,observer
        self.db=sqlite3.connect(path)
        self.db.executescript('CREATE TABLE IF NOT EXISTS batches(key TEXT PRIMARY KEY,status TEXT,payload TEXT); CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,key TEXT);')
        self.prepared={};self.identity='unprepared'

    @property
    def requests(self): return self.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]

    def close(self): self.db.close()

    async def prepare(self,configs,target,training,*,now=None,options=None):
        # Prepared children belong to exactly one complete batch. A later
        # scope must not silently retain answers from its predecessor.
        self.prepared={}
        options=options or CacheOptions()
        now=now or datetime.now(timezone.utc)
        request,_=batch_request(configs,target,training,now=now)
        fingerprint=key({'model':self.model.model_identity,'request':request})
        self.identity=fingerprint
        row=self.db.execute('SELECT status,payload FROM batches WHERE key=?',(fingerprint,)).fetchone()
        if options.policy=='cache_only' and (not row or row[0]!='complete'): raise CacheMiss('complete batch not cached')
        if row and row[0]!='complete' and not options.retry_failed: raise RuntimeError('explicit retry required for interrupted batch')
        cached=bool(row and row[0]=='complete' and options.policy!='refresh')
        if cached:
            payload=json.loads(row[1])
        else:
            if self.requests>=self.max_requests: raise RuntimeError('run decision request ceiling reached')
            with self.db:
                self.db.execute('INSERT INTO attempts(key) VALUES (?)',(fingerprint,))
                self.db.execute('INSERT OR REPLACE INTO batches VALUES (?,"pending",NULL)',(fingerprint,))
            exchanges=[]
            try:
                result=await self.model.classify_many(configs,target,training,now=now,event_sink=exchanges.append)
                if set(result.answers)!=set(configs): raise ValueError('batch missing a classifier')
                from .flywheel import DecisionFlywheel
                for identifier,config in configs.items():
                    DecisionFlywheel._features(config,ClassifiedAnswers(result.answers[identifier],result.model,None,result.latency_ms))
                payload={'result':asdict(result),'exchanges':exchanges}
                with self.db:self.db.execute('UPDATE batches SET status="complete",payload=? WHERE key=?',(json.dumps(payload,allow_nan=False),fingerprint))
            except Exception:
                with self.db:self.db.execute('UPDATE batches SET status="failed" WHERE key=?',(fingerprint,))
                raise
        self.observer({'kind':'shared-decision-batch','request_fingerprint':fingerprint,'classifier_ids':list(configs),
                       'cached':cached,'usage':None if cached else payload['result']['usage'],'requests':self.requests})
        for identifier,config in configs.items():
            child=key({'classifier':identifier,'request':config.request(target,training[identifier],now=now)})
            self.prepared[child]=(payload,identifier,fingerprint)
        return payload,now

    def adapter(self,identifier):
        shared=self
        class Adapter:
            @property
            def model_identity(self):return shared.model.model_identity+':shared-v1:'+shared.identity
            def cache_identity(self,config,target,training,*,now=None):
                child=key({'classifier':identifier,'request':config.request(target,training,now=now)})
                if child in shared.prepared:
                    fingerprint=shared.prepared[child][2]
                else:
                    request,_=batch_request({identifier:config},target,{identifier:training},now=now)
                    fingerprint=key({'model':shared.model.model_identity,'request':request})
                return shared.model.model_identity+':shared-answer-v2:'+fingerprint
            async def classify(self,config,target,training,*,now=None,event_sink=None):
                child=key({'classifier':identifier,'request':config.request(target,training,now=now)})
                if child not in shared.prepared:
                    await shared.prepare({identifier:config},target,{identifier:training},now=now)
                payload,_,fingerprint=shared.prepared[child]
                for exchange in payload['exchanges']:
                    if event_sink:event_sink({**exchange,'shared_request_fingerprint':fingerprint,'usage':None,'shared_exchange':True})
                raw=payload['result']
                return ClassifiedAnswers({name:DecisionResult(**answer) for name,answer in raw['answers'][identifier].items()},raw['model'],None,raw['latency_ms'])
        return Adapter()
