import asyncio
from types import SimpleNamespace

from .system_one import SystemOneAdapter
from ..models import DecisionTask, Item, LabeledItem


class FakeClient:
    async def system_one(self, *, state, questions):
        self.state, self.questions = state, questions
        answer = SimpleNamespace(model_dump=lambda: {"choice": "yes", "probabilities": {"yes": .8, "no": .2}})
        return SimpleNamespace(answers={"topic": answer}, model="fake", usage=None)


def test_a_system_one_adapter_keeps_target_and_context_separate():
    client = FakeClient(); task = DecisionTask("topic", ("yes", "no"), "Classify only target.")
    result = asyncio.run(SystemOneAdapter(client, name="jev").decide(task, Item("t", {"text": "target"}), [LabeledItem(Item("d", {"text": "demo"}), "yes")]))
    assert result.label == "yes"
    assert client.state == {"labeled_examples": [{"text": "demo", "label": "yes"}], "target": {"text": "target"}}
    assert client.questions["topic"]["criteria"] == {"yes": None, "no": None}
