"""OpenAI implementation of Cyclotron's provider-neutral decision contract."""
from __future__ import annotations
import asyncio, json, time
from dataclasses import dataclass
from ..models import DecisionResult, ModelCapabilities
from ..classifier_config import ClassifiedAnswers

@dataclass(frozen=True)
class OpenAIDecisionConfiguration:
    model: str = 'gpt-4.1-mini'
    max_tokens: int = 500
    def __post_init__(self):
        if not isinstance(self.model,str) or not self.model.strip() or type(self.max_tokens) is not int or self.max_tokens < 64:
            raise ValueError('OpenAI decision model and max_tokens must be explicit and valid')
    @property
    def model_identity(self): return f'openai:{self.model}'

class OpenAIDecisionAdapter:
    name='openai'
    capabilities=ModelCapabilities(supports_labeled_context=True,supports_probability_distributions=True)
    def __init__(self, client, *, configuration=OpenAIDecisionConfiguration()): self.client,self.configuration=client,configuration
    @property
    def model_identity(self): return self.configuration.model_identity
    @classmethod
    def from_environment(cls, *, configuration=OpenAIDecisionConfiguration()):
        from dotenv import load_dotenv
        from openai import OpenAI
        load_dotenv(override=False); return cls(OpenAI(max_retries=0,timeout=60),configuration=configuration)
    async def classify(self, config, target, training, *, now=None, event_sink=None):
        request=config.request(target,training,now=now); observe=event_sink or (lambda _:None)
        questions={name:{'instructions':detail['instructions'],'labels':detail['options']} for name,detail in request['questions'].items()}
        observe({'kind':'decision-request','target_id':target.id,'model':self.model_identity,'state':request['state'],'questions':questions})
        answer_schema=lambda labels:{'type':'object','additionalProperties':False,'required':['choice','probabilities'],'properties':{'choice':{'type':'string','enum':list(labels)},'probabilities':{'type':'object','additionalProperties':False,'required':list(labels),'properties':{label:{'type':'number','minimum':0,'maximum':1} for label in labels}}}}
        schema={'type':'object','additionalProperties':False,'required':['answers'],'properties':{'answers':{'type':'object','additionalProperties':False,'required':list(questions),'properties':{name:answer_schema(detail['labels']) for name,detail in questions.items()}}}}
        messages=[{'role':'system','content':'Classify the target. Return the required JSON schema exactly; probabilities must sum to one.'},{'role':'user','content':json.dumps({'state':request['state'],'questions':questions})}]
        response_format={'type':'json_schema','json_schema':{'name':'cyclotron_decisions','strict':True,'schema':schema}}
        started=time.perf_counter(); response=await asyncio.to_thread(self.client.chat.completions.create,model=self.configuration.model,messages=messages,response_format=response_format,max_tokens=self.configuration.max_tokens)
        payload=json.loads(response.choices[0].message.content or '{}'); raw=payload.get('answers',payload)
        # Some schema-following models name the primary answer after the human
        # task instead of Cyclotron's internal ``decision`` feature key.
        if 'decision' not in raw and config.task.name in raw:
            raw={**raw,'decision':raw[config.task.name]}
        usage=response.usage.model_dump() if getattr(response,'usage',None) else {}
        observe({'kind':'decision-response','target_id':target.id,'answers':raw,'model':getattr(response,'model',None),'usage':usage})
        tasks={'decision':config.task,**{task.name:task for task in config.tasks}}; answers={}
        for name,task in tasks.items():
            value=raw.get(name,{})
            try: answers[name]=task.validate_result(DecisionResult(value.get('choice'),value.get('probabilities'),getattr(response,'model',None),None,None,value.get('confidence')))
            except (TypeError,ValueError) as error:
                observe({'kind':'decision-invalid-response','target_id':target.id,'error':str(error),'top_level_keys':sorted(payload) if isinstance(payload,dict) else [],'answer_keys':sorted(raw) if isinstance(raw,dict) else []})
                raise
        return ClassifiedAnswers(answers,getattr(response,'model',None),usage,round((time.perf_counter()-started)*1000,2))
