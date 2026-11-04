import asyncio

from .adapters.optimizer_transport import validate_optimizer_transport  # noqa: F401
from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from .embedded_cyclotron_test import Model, item
from .iteration_learning import IterationPlan, learn_iteration
from .iteration_transfer import adopt_iteration
from .optimizer_agent import OptimizerAgent, OptimizerReply
from .priming_context import decode_priming, encode_priming
from .models import LabeledItem
from .vote_examples import Vote
import json


class Luna:
    """A scripted optimizer: one improved rubric, then one new decision element."""

    calls = 0

    def __call__(self, messages):
        type(self).calls += 1
        text = json.dumps(messages)
        if '"control_under_test": "tasks"' in text or "control_under_test\\\": \\\"tasks" in text:
            proposal = {
                "tasks": [{"name": "beat", "labels": ["yes", "no"], "instructions": "Is it on the beat?"}],
                "rationale": "a new element",
            }
        else:
            proposal = {"rubric": "Keep what the editors keep.", "rationale": "from the votes"}
        return OptimizerReply(json.dumps(proposal), "scripted", {}, ())


def definition():
    specs = tuple(ClassifierSpec(name, ("include", "exclude"), f"For {name}?", positive_label="include") for name in ("one", "two"))
    return CyclotronDefinition("shared", specs, seed="seed")


def votes(count, output="one"):
    return tuple(
        Vote(output, f"{output}-{n}", {"text": f"{'yes' if n % 2 else 'no'} story {n} {output}"}, "include" if n % 2 else "exclude", "said why")
        for n in range(count)
    )


def priming():
    rows = {
        name: [
            LabeledItem(item(f"{name}-p{n}", f"{'yes' if n % 2 else 'no'} primed {n}"), "include" if n % 2 else "exclude", "trusted", {})
            for n in range(8)
        ]
        for name in ("one", "two")
    }
    tasks = {spec.id: spec.task for spec in definition().classifiers}
    return encode_priming(rows, tasks)


def plan(**changes):
    values = dict(
        identity="first", definition=definition(), rubrics={}, priming=priming(), votes={"one": votes(24), "two": ()}, due=("one",)
    )
    return IterationPlan(**{**values, **changes})


def test_a_due_site_gets_every_technique_and_the_live_store_can_adopt_the_result(tmp_path):
    result = asyncio.run(learn_iteration(plan(), Model(), OptimizerAgent(Luna()), tmp_path))
    record, report = result["record"], result["report"]["one"]
    assert report["acceptance"]["accepted"]
    assert report["rubric"] == "Keep what the editors keep."
    assert report["fittedClassifier"]
    live = Cyclotron.open(tmp_path / "live", definition(), Model(), priming_examples=decode_priming(priming()))
    assert asyncio.run(adopt_iteration(live, record))["adopted"]
    assert live.wheels["one"].active.head is not None
    assert live.wheels["one"].active.config.rubric == "Keep what the editors keep."
    assert len(live.wheels["one"].active.config.example_ids) == 32
    live.close()


def test_a_site_that_is_not_due_keeps_its_version(tmp_path):
    result = asyncio.run(learn_iteration(plan(), Model(), OptimizerAgent(Luna()), tmp_path))
    assert set(result["record"]["versions"]) == {"one"}
