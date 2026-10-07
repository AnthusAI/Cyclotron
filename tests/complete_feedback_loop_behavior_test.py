"""Executable end-to-end Gherkin contract for the reusable flywheel."""
import asyncio

from pytest_bdd import given, scenarios, then, when

from decision_flywheel import ClassifierConfig, DecisionFlywheel
from decision_flywheel.classifier_config import ClassifiedAnswers
from decision_flywheel.models import DecisionResult, DecisionTask, Item, LabeledItem
from decision_flywheel.optimizer_agent import OptimizerAgent, OptimizerReply


scenarios("features/complete_feedback_loop.feature")


TASK = DecisionTask("inclusion", ("include", "exclude"), "Should this item be included?")


class ScriptedDecisionModel:
    """A local adapter which exposes a deliberately useful added feature."""

    model_identity = "scripted-complete-loop-v1"

    def __init__(self):
        self.calls = 0

    async def classify(self, config, target, training, *, now=None, event_sink=None):
        self.calls += 1
        positive = target.values["text"].startswith("yes")
        answers = {"decision": DecisionResult("exclude", {"include": 0.2, "exclude": 0.8})}
        for task in config.tasks:
            answers[task.name] = DecisionResult(
                "yes" if positive else "no", {"yes": 0.98 if positive else 0.02, "no": 0.02 if positive else 0.98}
            )
        request = config.request(target, training, now=now)
        if event_sink:
            event_sink({"kind": "decision-request", "target_id": target.id, "model": self.model_identity,
                        "state": request["state"], "questions": request["questions"]})
            event_sink({"kind": "decision-response", "target_id": target.id, "model": self.model_identity,
                        "answers": {name: {"choice": result.label, "probabilities": result.probabilities}
                                    for name, result in answers.items()}})
        return ClassifiedAnswers(answers, self.model_identity, {"input_tokens": 1}, 0)


def _optimizer():
    return OptimizerAgent(lambda _: OptimizerReply(
        '{"rationale":"The feedback separates useful work",'
        '"rubric":"Prefer useful work",'
        '"example_ids":["train-0","train-1"],'
        '"tasks":[{"name":"useful","instructions":"Is this useful?","labels":["yes","no"]}]}',
        "scripted-optimizer-v1",
    ))


def _labels():
    training = tuple(
        LabeledItem(Item(f"train-{index}", {"text": f"{'yes' if index % 2 else 'no'} training {index}"}),
                    "include" if index % 2 else "exclude")
        for index in range(6)
    )
    development = tuple(
        LabeledItem(Item(f"development-{index}", {"text": f"{'yes' if index % 2 else 'no'} development {index}"}),
                    "include" if index % 2 else "exclude")
        for index in range(2)
    )
    return training, development


@given("a reusable flywheel with scripted feedback and trusted labels", target_fixture="state")
def reusable_flywheel(tmp_path):
    model = ScriptedDecisionModel()
    training, development = _labels()
    path = tmp_path / "complete-loop.sqlite3"
    return {
        "path": path,
        "model": model,
        "training": training,
        "development": development,
        "wheel": DecisionFlywheel(path, ClassifierConfig(TASK), model, _optimizer(), max_requests=50),
    }


@when("the application improves the classifier")
def improve_classifier(state):
    state["result"] = asyncio.run(state["wheel"].improve(
        state["training"], state["development"], protected=(),
        propensities={row.item.id: 1.0 for row in state["training"]},
    ))


@then("the optimizer proposal, decision requests, and fitted head are observable")
def observable_loop(state):
    wheel = state["wheel"]
    assert state["result"]["promoted"]
    assert wheel.active.head is not None
    kinds = {event["kind"] for event in wheel.history(10_000)}
    assert {"optimizer-request", "optimizer-response", "proposal-validated", "decision-request",
            "decision-response", "fit-started", "fit-completed", "promoted"} <= kinds


@then("a new prediction is served by the fitted head")
def fitted_prediction(state):
    prediction = asyncio.run(state["wheel"].predict(Item("new-item", {"text": "yes new item"}), state["training"]))
    state["prediction"] = prediction
    assert prediction.label == "include"
    assert set(state["wheel"].history()[-1]["ml_features"]) == set(state["wheel"].active.head.feature_names)


@then("reopening the flywheel preserves the fitted version without a new model call")
def reopen_preserves_fit(state):
    wheel = state["wheel"]
    version, calls = wheel.active.fingerprint, state["model"].calls
    wheel.close()
    reopened = DecisionFlywheel(state["path"], ClassifierConfig(TASK), state["model"], _optimizer(), max_requests=50)
    state["wheel"] = reopened
    assert reopened.active.fingerprint == version
    assert asyncio.run(reopened.predict(Item("new-item", {"text": "yes new item"}), state["training"])) == state["prediction"]
    assert state["model"].calls == calls
    reopened.close()


@when("a trusted label is corrected")
def correct_label(state):
    corrected = (LabeledItem(state["training"][0].item, "include"), *state["training"][1:])
    state["wheel"].reconcile_feedback(corrected, development=state["development"])


@then("the fitted classifier is invalidated and the correction is observable")
def correction_invalidates_fit(state):
    wheel = state["wheel"]
    assert wheel.active.head is None
    assert wheel.history()[-1]["kind"] == "classifier-invalidated"
    wheel.close()
