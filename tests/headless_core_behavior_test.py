"""Executable Gherkin contract for the headless public library."""
import asyncio
import ast
from pathlib import Path

from pytest_bdd import given, scenarios, then, when

from decision_flywheel import ClassifierConfig, DecisionFlywheel
from decision_flywheel.classifier_config import ClassifiedAnswers
from decision_flywheel.models import DecisionResult, DecisionTask, Item


scenarios("features/headless_core.feature")


class ScriptedModel:
    """A zero-network adapter for the public core contract."""

    model_identity = "scripted-model-v1"

    async def classify(self, config, target, training, *, now=None, event_sink=None):
        result = DecisionResult("include", {"include": 0.8, "exclude": 0.2})
        return ClassifiedAnswers({"decision": result}, self.model_identity, {"input_tokens": 3}, 0)


@given("a headless flywheel with a scripted decision model", target_fixture="wheel")
def headless_wheel(tmp_path):
    task = DecisionTask("include-item", ("include", "exclude"), "Should this item be included?")
    wheel = DecisionFlywheel(tmp_path / "core.sqlite3", ClassifierConfig(task), ScriptedModel())
    yield wheel
    wheel.close()


@when("the application predicts a new item", target_fixture="prediction")
def predict_item(wheel):
    return asyncio.run(wheel.predict(Item("item-1", {"text": "A useful paper"}), ()))


@then("the application receives the predicted label and confidence")
def prediction_has_public_result(wheel, prediction):
    assert prediction.label == "include"
    assert prediction.confidence == 0.8


@then("the application can inspect a prediction event")
def prediction_has_event(wheel):
    assert wheel.history()[-1]["kind"] == "prediction"


@given("the reusable core module list", target_fixture="core_modules")
def reusable_core_modules():
    root = Path(__file__).parents[1] / "src" / "decision_flywheel"
    excluded_prefixes = ("application_runtime", "web_", "trace_", "reviewer")
    return tuple(path for path in root.glob("*.py")
                 if not path.name.endswith("_test.py")
                 and not path.stem.startswith(excluded_prefixes))


@when("its imports are inspected", target_fixture="core_imports")
def inspect_core_imports(core_modules):
    imports = []
    for module in core_modules:
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.append((module.name, node.module))
            elif isinstance(node, ast.Import):
                imports.extend((module.name, alias.name) for alias in node.names)
    return tuple(imports)


@then("no core module imports a web, trace, or reviewer module")
def core_does_not_import_application_modules(core_imports):
    forbidden = ("decision_flywheel.web_", "decision_flywheel.trace_", "decision_flywheel.reviewer")
    assert not [entry for entry in core_imports if entry[1].startswith(forbidden)]
