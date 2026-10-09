"""Decision Flywheel's model-neutral decision-context toolkit."""

__version__ = "0.16.0"

from .models import DecisionModel, DecisionResult, DecisionTask, Item, LabeledItem, ModelCapabilities
from .artifacts import DeployableArtifact, create_artifact, load_artifact
from .bundle import ClassifierBundle, load_bundle
from .budget import ContextBudget, ContextPlan, build_context_ladder, build_context_plan
from .context import FixedExampleList, PerLabelLexicalRetrieval, PrototypeBalanced, RandomBalanced
from .example_list import (ExampleListImprovement, example_list_from_policy, improve_example_list,
                           plan_example_list_round)
from .events import FlywheelEvent, JsonlEventStream
from .optimizer import ObjectiveFallbackResult, OptimizationResult, TrialSpec, search_context_policies
from .run_ledger import (FeatureActivity, FeatureDefinition, FlywheelRound, FlywheelStatus,
                         JsonlRunLedger, TrialActivity, feedback_fingerprint)
from .steering import steering_observations
from .classifier_config import ClassifierConfig
from .optimizer_agent import DisabledOptimizer, FeedbackBriefing, OptimizerAgent, OptimizerReply
from .flywheel import DecisionFlywheel, FittedClassifier
from .evaluation_policy import EvaluationPolicy
from .selection_policy import SelectionPolicy
from .feedback_trigger import LabelTransitionTrigger
from .classification_metrics import classification_metrics
from .replay import ReplayPlan, plan_replay, recent_balanced, run_replay
from .feature_bank import FeatureBank, probability_diagnostics
from .question_measurement import measure_questions, rank_question, select_window
from .classifier_training import train_classifier
from .example_attribution import measure_example_swaps, plan_swaps
from .cyclotron_status import CyclotronStatus, ReviewRate, ReviewRateOverride, status_from_flywheel
from .review_program import ReviewProgram
from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition, Decision, FileLease, Review, StoreLocked


def run_demo(*args, **kwargs):
    """Lazily expose the optional walkthrough without preloading its CLI module."""
    from .demo import run_demo as _run_demo
    return _run_demo(*args, **kwargs)

__all__ = [
    "CyclotronStatus", "ReviewRate", "ReviewRateOverride", "status_from_flywheel",
    "ReviewProgram", "ClassifierSpec", "Cyclotron", "CyclotronDefinition", "Decision", "FileLease", "Review", "StoreLocked",
    "EvaluationPolicy",
    "SelectionPolicy",
    "LabelTransitionTrigger",
    "FeatureBank", "probability_diagnostics",
    "measure_questions", "rank_question", "select_window",
    "train_classifier",
    "measure_example_swaps", "plan_swaps",
    "ClassifierConfig", "DecisionFlywheel", "DisabledOptimizer", "FittedClassifier", "FeedbackBriefing", "OptimizerAgent", "OptimizerReply", "classification_metrics",
    "ReplayPlan", "plan_replay", "recent_balanced", "run_replay",
    "ClassifierBundle", "ContextBudget", "ContextPlan", "DecisionModel", "DecisionResult", "DecisionTask",
    "DeployableArtifact", "ExampleListImprovement", "FixedExampleList", "Item", "LabeledItem", "ModelCapabilities", "ObjectiveFallbackResult", "OptimizationResult",
    "FeatureActivity", "FeatureDefinition", "FlywheelEvent", "FlywheelRound", "FlywheelStatus", "JsonlEventStream", "JsonlRunLedger", "TrialActivity",
    "PerLabelLexicalRetrieval", "PrototypeBalanced", "RandomBalanced", "TrialSpec",
    "build_context_ladder", "build_context_plan", "create_artifact", "example_list_from_policy", "improve_example_list", "load_artifact", "load_bundle",
    "plan_example_list_round", "run_demo",
    "search_context_policies", "feedback_fingerprint", "steering_observations",
]
