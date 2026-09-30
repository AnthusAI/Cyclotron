"""A deterministic, synthetic optimize-save-decide walkthrough with no network."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Sequence

from .artifacts import create_artifact, load_artifact
from .budget import ContextBudget
from .context import PerLabelLexicalRetrieval, PrototypeBalanced, RandomBalanced
from .models import DecisionResult, DecisionTask, Item, LabeledItem, ModelCapabilities
from .optimizer import TrialSpec, search_context_policies


POOL_REVISION = "synthetic-demo-v1"
MODEL_FINGERPRINT = "synthetic-rule-model-v1"


class SyntheticRuleModel:
    """A fake model whose behavior is deliberately transparent and offline."""

    name = "synthetic-rule-model"
    fingerprint = MODEL_FINGERPRINT
    capabilities = ModelCapabilities()

    async def decide(self, task: DecisionTask, target: Item,
                     context: Sequence[LabeledItem]) -> DecisionResult:
        text = target.values[task.input_field]
        marker, expected = ("crisp", "approve") if "crisp" in text else ("cold", "reject")
        has_matching_example = len(context) == len(task.labels) and any(
            example.label == expected and marker in example.item.values[task.input_field]
            for example in context
        )
        prediction = expected if has_matching_example else next(label for label in task.labels if label != expected)
        return DecisionResult(prediction, model=self.name)


def synthetic_inputs() -> tuple[DecisionTask, list[LabeledItem], list[LabeledItem], Item]:
    """Return tiny trusted synthetic splits and an intentionally unlabeled target."""
    task = DecisionTask("review", ("approve", "reject"), "Classify the review outcome.")
    pool = [
        LabeledItem(Item("approve-crisp", {"text": "crisp approval signal"}), "approve"),
        LabeledItem(Item("approve-bright", {"text": "approval signal"}), "approve"),
        LabeledItem(Item("approve-unused", {"text": "approval unused archive"}), "approve"),
        LabeledItem(Item("reject-cold", {"text": "cold rejection signal"}), "reject"),
        LabeledItem(Item("reject-dull", {"text": "rejection signal"}), "reject"),
        LabeledItem(Item("reject-unused", {"text": "rejection unused archive"}), "reject"),
    ]
    development = [
        LabeledItem(Item("development-approve", {"text": "crisp development review"}), "approve"),
        LabeledItem(Item("development-reject", {"text": "cold development review"}), "reject"),
    ]
    return task, pool, development, Item("unlabeled-target", {"text": "crisp unlabeled request"})


async def run_demo(output_directory: Path) -> dict[str, object]:
    """Run synthetic optimization, save/reload its artifact, and decide a new item.

    Files contain hashes, IDs, policy/budget metadata, and the synthetic result;
    they intentionally never contain source texts, labels for the new target,
    credentials, or a network checkpoint.
    """
    task, pool, development, target = synthetic_inputs()
    trials = (
        TrialSpec(RandomBalanced(seed=1), per_label=0, name="random-zero",
                  budget=ContextBudget(per_label=0)),
        TrialSpec(RandomBalanced(seed=1), per_label=1, name="random-seed-1",
                  budget=ContextBudget(per_label=1)),
        TrialSpec(RandomBalanced(seed=2), per_label=1, name="random-seed-2",
                  budget=ContextBudget(per_label=1)),
        TrialSpec(PrototypeBalanced(), per_label=1, name="prototype-size-1",
                  budget=ContextBudget(per_label=1)),
        TrialSpec(PrototypeBalanced(), per_label=2, name="prototype-size-2",
                  budget=ContextBudget(per_label=2)),
        TrialSpec(PerLabelLexicalRetrieval(), per_label=1, name="lexical-size-1",
                  budget=ContextBudget(per_label=1)),
        TrialSpec(PerLabelLexicalRetrieval(), per_label=2, name="lexical-size-2",
                  budget=ContextBudget(per_label=2)),
    )
    checkpoint: dict[str, dict[str, str]] = {}
    call_accounting: dict[str, int | str] = {}
    model = SyntheticRuleModel()
    optimization = await search_context_policies(
        task, pool, development, model, trials, max_model_calls=14, objective="accuracy",
        checkpoint=checkpoint, call_accounting=call_accounting, model_fingerprint=MODEL_FINGERPRINT,
        display_order="canonical", order_seed=0,
    )
    if optimization.winner is None:
        raise RuntimeError("the deterministic synthetic demo did not freeze a winner")

    serialized = create_artifact(task, pool, POOL_REVISION, optimization)
    destination = Path(output_directory)
    destination.mkdir(parents=True, exist_ok=True)
    artifact_path = destination / "artifact.json"
    artifact_path.write_text(serialized + "\n", encoding="utf-8")
    reloaded = load_artifact(serialized, task, pool, POOL_REVISION)
    plan = reloaded.context_for(target)
    decision = await reloaded.apply(task, target, model, model_fingerprint=MODEL_FINGERPRINT)

    document = json.loads(serialized)
    summary: dict[str, object] = {
        "artifact_hash": document["artifact_hash"],
        "frozen_winner": optimization.winner is not None,
        "model_calls_attempted": optimization.model_calls_attempted,
        "prediction": decision.label,
        "provisional_best": optimization.provisional_best.trial_name if optimization.provisional_best else None,
        "reloaded_context_ids": list(plan.example_ids),
        "synthetic": True,
        "data_origin": "synthetic-scripted-fixture",
        "target_id": target.id,
        "trial_objectives": {trial.trial_name: trial.objective for trial in optimization.trials},
        "winner": optimization.winner.trial_name,
    }
    (destination / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the offline synthetic Decision Flywheel walkthrough.")
    parser.add_argument("--output", type=Path, default=Path("demo-output"),
                        help="folder for text-free artifact.json and summary.json")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_demo(args.output)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
