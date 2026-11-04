import asyncio
import json

from .adapters.optimizer_transport import validate_optimizer_transport  # noqa: F401
from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from .embedded_cyclotron_test import Model, item
from .iteration_learning import IterationPlan, learn_iteration
from .iteration_transfer import adopt_iteration
from .models import LabeledItem
from .optimizer_agent import OptimizerAgent, OptimizerReply
from .priming_context import decode_priming, encode_priming
from .vote_examples import Vote


class Luna:
    """A scripted optimizer: one improved rubric, then one new decision element."""

    calls = 0

    def __call__(self, messages):
        type(self).calls += 1
        text = json.dumps(messages)
        if '"control_under_test": "tasks"' in text or 'control_under_test\\": \\"tasks' in text:
            proposal = {
                "tasks": [{"name": "beat", "labels": ["yes", "no"], "instructions": "Is it on the beat?"}],
                "rationale": "a new element",
            }
        else:
            proposal = {"rubric": "Keep what the editors keep.", "rationale": "from the votes"}
        return OptimizerReply(json.dumps(proposal), "scripted", {}, ())


def definition():
    specs = tuple(
        ClassifierSpec(name, ("include", "exclude"), f"For {name}?", positive_label="include") for name in ("one", "two")
    )
    return CyclotronDefinition("shared", specs, seed="seed")


def votes(count, output="one"):
    return tuple(
        Vote(
            output,
            f"{output}-{n}",
            {"text": f"{'yes' if n % 2 else 'no'} story {n} {output}"},
            "include" if n % 2 else "exclude",
            "said why",
        )
        for n in range(count)
    )


def priming():
    rows = {
        name: [
            LabeledItem(
                item(f"{name}-p{n}", f"{'yes' if n % 2 else 'no'} primed {n}"), "include" if n % 2 else "exclude", "trusted", {}
            )
            for n in range(8)
        ]
        for name in ("one", "two")
    }
    tasks = {spec.id: spec.task for spec in definition().classifiers}
    return encode_priming(rows, tasks)


def plan(**changes):
    values = dict(
        identity="first",
        definition=definition(),
        rubrics={},
        priming=priming(),
        votes={"one": votes(24), "two": ()},
        due=("one",),
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


def test_evaluation_votes_stored_as_examples_by_an_earlier_iteration_stay_hidden_from_the_optimizer(tmp_path):
    from .vote_examples import split_votes

    rows = votes(24)
    earlier = encode_priming(
        {"one": [LabeledItem(item(v.item_id, v.values["text"]), v.label, "trusted", {}) for v in rows]},
        {spec.id: spec.task for spec in definition().classifiers},
    )
    seen = []

    class Watching(Luna):
        def __call__(self, messages):
            seen.append(json.dumps(messages))
            return super().__call__(messages)

    merged = {**priming(), "one": priming()["one"] + earlier["one"]}
    result = asyncio.run(learn_iteration(plan(priming=merged), Model(), OptimizerAgent(Watching()), tmp_path))
    assert result["record"] is not None
    _, evaluation = split_votes(rows, "seed")
    assert evaluation and seen
    assert not any(v.values["text"] in text for v in evaluation for text in seen)


def test_one_text_voted_under_two_identities_counts_once(tmp_path):
    rows = votes(24) + (Vote("one", "one-copy", {"text": votes(24)[0].values["text"]}, "exclude", ""),)
    result = asyncio.run(learn_iteration(plan(votes={"one": rows, "two": ()}), Model(), OptimizerAgent(Luna()), tmp_path))
    assert result["report"]["one"]["votes"] == 24
    assert result["record"] is not None
