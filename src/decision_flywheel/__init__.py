"""Decision Flywheel's model-neutral decision-context toolkit."""

__version__ = "0.4.1"

from .models import DecisionModel, DecisionResult, DecisionTask, Item, LabeledItem, ModelCapabilities
from .artifacts import DeployableArtifact, create_artifact, load_artifact
from .budget import ContextBudget, ContextPlan, build_context_ladder, build_context_plan
from .context import PerLabelLexicalRetrieval, PrototypeBalanced, RandomBalanced
from .optimizer import OptimizationResult, TrialSpec, search_context_policies


def run_demo(*args, **kwargs):
    """Lazily expose the optional walkthrough without preloading its CLI module."""
    from .demo import run_demo as _run_demo
    return _run_demo(*args, **kwargs)

__all__ = [
    "ContextBudget", "ContextPlan", "DecisionModel", "DecisionResult", "DecisionTask",
    "DeployableArtifact", "Item", "LabeledItem", "ModelCapabilities", "OptimizationResult",
    "PerLabelLexicalRetrieval", "PrototypeBalanced", "RandomBalanced", "TrialSpec",
    "build_context_ladder", "build_context_plan", "create_artifact", "load_artifact", "run_demo",
    "search_context_policies",
]
