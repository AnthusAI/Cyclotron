"""Decision Flywheel's model-neutral decision-context toolkit."""

__version__ = "0.6.0"

from .models import DecisionModel, DecisionResult, DecisionTask, Item, LabeledItem, ModelCapabilities
from .artifacts import DeployableArtifact, create_artifact, load_artifact
from .bundle import ClassifierBundle, load_bundle
from .budget import ContextBudget, ContextPlan, build_context_ladder, build_context_plan
from .context import FixedExampleList, PerLabelLexicalRetrieval, PrototypeBalanced, RandomBalanced
from .example_list import (ExampleListImprovement, example_list_from_policy, improve_example_list,
                           plan_example_list_round)
from .events import FlywheelEvent, JsonlEventStream
from .optimizer import OptimizationResult, TrialSpec, search_context_policies
from .run_ledger import (FeatureActivity, FeatureDefinition, FlywheelRound, FlywheelStatus,
                         JsonlRunLedger, TrialActivity, feedback_fingerprint)


def run_demo(*args, **kwargs):
    """Lazily expose the optional walkthrough without preloading its CLI module."""
    from .demo import run_demo as _run_demo
    return _run_demo(*args, **kwargs)

__all__ = [
    "ClassifierBundle", "ContextBudget", "ContextPlan", "DecisionModel", "DecisionResult", "DecisionTask",
    "DeployableArtifact", "ExampleListImprovement", "FixedExampleList", "Item", "LabeledItem", "ModelCapabilities", "OptimizationResult",
    "FeatureActivity", "FeatureDefinition", "FlywheelEvent", "FlywheelRound", "FlywheelStatus", "JsonlEventStream", "JsonlRunLedger", "TrialActivity",
    "PerLabelLexicalRetrieval", "PrototypeBalanced", "RandomBalanced", "TrialSpec",
    "build_context_ladder", "build_context_plan", "create_artifact", "example_list_from_policy", "improve_example_list", "load_artifact", "load_bundle",
    "plan_example_list_round", "run_demo",
    "search_context_policies", "feedback_fingerprint",
]
