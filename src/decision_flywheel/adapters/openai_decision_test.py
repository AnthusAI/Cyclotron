import asyncio
from types import SimpleNamespace
from .openai_decision import OpenAIDecisionAdapter
from ..classifier_config import ClassifierConfig
from ..models import DecisionTask,Item

def test_openai_decision_adapter_requires_and_preserves_provider_probabilities():
 class Create:
  def create(self,**kwargs):
   self.kwargs=kwargs; return SimpleNamespace(model='fake',usage=SimpleNamespace(model_dump=lambda:{'total_tokens':3}),choices=[SimpleNamespace(message=SimpleNamespace(content='{"answers":{"decision":{"choice":"yes","probabilities":{"yes":0.8,"no":0.2}}}}'))])
 client=SimpleNamespace(chat=SimpleNamespace(completions=Create()))
 task=DecisionTask('topic',('yes','no'),'Choose.')
 result=asyncio.run(OpenAIDecisionAdapter(client).classify(ClassifierConfig(task),Item('x',{'text':'hello'}),[]))
 assert result.answers['decision'].probabilities=={'yes':.8,'no':.2}
 assert client.chat.completions.kwargs['response_format']['type']=='json_schema'
 assert client.chat.completions.kwargs['max_tokens']==500

def test_openai_decision_adapter_accepts_primary_answer_named_after_human_task():
 class Create:
  def create(self,**_): return SimpleNamespace(model='fake',usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content='{"editorial":{"choice":"publish","probabilities":{"publish":0.7,"reject":0.3}}}'))])
 task=DecisionTask('editorial',('publish','reject'),'Choose.')
 client=SimpleNamespace(chat=SimpleNamespace(completions=Create()))
 result=asyncio.run(OpenAIDecisionAdapter(client).classify(ClassifierConfig(task),Item('x',{'text':'hello'}),[]))
 assert result.answers['decision'].label == 'publish'
