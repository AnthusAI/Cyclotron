"""Strict, text-free artifacts with SHA-256 integrity checksums, not signatures."""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass
from typing import Any, Sequence

from .budget import ContextBudget, ContextPlan, build_context_plan
from .context import (POLICY_VERSION, FixedExampleList, PerLabelLexicalRetrieval, PolicyMetadata,
                      PrototypeBalanced, RandomBalanced)
from .models import DecisionModel, DecisionResult, DecisionTask, Item, LabeledItem


ARTIFACT_VERSION = 1
_TOP_KEYS = {"artifact_version", "artifact_hash", "task_fingerprint", "policy", "pool", "budget",
             "candidate_pool_fingerprint", "display_order", "order_seed", "presentation_label_order",
             "model_fingerprint", "development", "search"}


class ArtifactValidationError(ValueError):
    pass


@dataclass(frozen=True)
class DeployableArtifact:
    """Validated data references plus a rehydrated built-in policy; never source text."""

    task: DecisionTask
    pool: tuple[LabeledItem, ...]
    pool_revision: str
    policy: Any
    budget: ContextBudget
    display_order: str
    order_seed: int
    presentation_label_order: tuple[str, ...]
    model_fingerprint: str
    development_objective: float
    development_split_fingerprint: str
    search_fingerprint: str
    trial_name: str
    artifact_hash: str  # An integrity checksum; it does not authenticate an author.

    def context_for(self, target: Item) -> ContextPlan:
        return build_context_plan(
            self.task, target, self.pool, self.policy, budget=self.budget,
            display_order=self.display_order, order_seed=self.order_seed,
            presentation_label_order=self.presentation_label_order,
        )

    async def apply(self, task: DecisionTask, target: Item, model: DecisionModel, *,
                    model_fingerprint: str) -> DecisionResult:
        if task.fingerprint != self.task.fingerprint:
            raise ArtifactValidationError("task fingerprint does not match artifact")
        if model_fingerprint != self.model_fingerprint:
            raise ArtifactValidationError("model fingerprint does not match artifact")
        return task.validate_result(await model.decide(task, target, self.context_for(target).examples))


def create_artifact(task: DecisionTask, pool: Sequence[LabeledItem], pool_revision: str,
                    optimization: Any) -> str:
    """Serialize the optimizer's fully frozen winner into canonical strict JSON."""
    winner = getattr(optimization, "winner", None)
    if winner is None or winner.status != "completed" or winner.objective is None:
        raise ArtifactValidationError("only a fully frozen winner may produce a deployable artifact")
    if not isinstance(pool_revision, str) or not pool_revision:
        raise ArtifactValidationError("pool revision must be a non-empty string")
    if getattr(optimization, "task_fingerprint", None) != task.fingerprint:
        raise ArtifactValidationError("optimizer task fingerprint does not match artifact task")
    candidate_pool_fingerprint = _pool_fingerprint(task, pool)
    if getattr(optimization, "candidate_pool_fingerprint", None) != candidate_pool_fingerprint:
        raise ArtifactValidationError("optimizer candidate pool fingerprint does not match artifact pool")
    development_fingerprint = getattr(optimization, "development_split_fingerprint", None)
    search_fingerprint = getattr(optimization, "search_fingerprint", None)
    if not _is_hex_digest(development_fingerprint) or not _is_hex_digest(search_fingerprint):
        raise ArtifactValidationError("optimizer provenance fingerprints are invalid")
    policy = _policy_from_metadata(winner.policy_metadata, winner.policy_fingerprint)
    document = {
        "artifact_version": ARTIFACT_VERSION,
        "task_fingerprint": task.fingerprint,
        "candidate_pool_fingerprint": candidate_pool_fingerprint,
        "policy": _policy_document(policy.metadata, policy.fingerprint),
        "pool": {"revision": pool_revision, "items": _pool_document(task, pool)},
        "budget": _budget_document(winner.budget),
        "display_order": winner.display_order,
        "order_seed": winner.order_seed,
        "presentation_label_order": list(winner.presentation_label_order),
        "model_fingerprint": optimization.model_fingerprint,
        "development": {"objective_name": optimization.objective, "objective": winner.objective,
                        "fingerprint": development_fingerprint},
        "search": {"fingerprint": search_fingerprint, "trial_name": winner.trial_name},
    }
    document["artifact_hash"] = _hash(document)
    return _canonical(document)


def load_artifact(serialized: str, task: DecisionTask, pool: Sequence[LabeledItem],
                  pool_revision: str) -> DeployableArtifact:
    try:
        document = json.loads(serialized, object_pairs_hook=_unique_object)
    except (TypeError, json.JSONDecodeError) as error:
        raise ArtifactValidationError("artifact must be valid JSON") from error
    _validate_document(document)
    supplied_hash = document["artifact_hash"]
    without_hash = {key: value for key, value in document.items() if key != "artifact_hash"}
    if supplied_hash != _hash(without_hash):
        raise ArtifactValidationError("artifact hash does not match content")
    if document["task_fingerprint"] != task.fingerprint:
        raise ArtifactValidationError("task fingerprint does not match artifact")
    if document["pool"]["revision"] != pool_revision:
        raise ArtifactValidationError("pool revision does not match artifact")
    actual_pool = _pool_document(task, pool)
    if document["pool"]["items"] != actual_pool:
        raise ArtifactValidationError("pool content does not match artifact")
    if document["candidate_pool_fingerprint"] != _pool_fingerprint(task, pool):
        raise ArtifactValidationError("candidate pool fingerprint does not match artifact")
    if set(document["presentation_label_order"]) != set(task.labels) or len(document["presentation_label_order"]) != len(task.labels):
        raise ArtifactValidationError("presentation label order does not match task")
    policy = _policy_from_document(document["policy"])
    budget = _budget_from_document(document["budget"])
    return DeployableArtifact(
        task, tuple(sorted(pool, key=lambda row: row.item.id)), pool_revision, policy, budget,
        document["display_order"], document["order_seed"], tuple(document["presentation_label_order"]),
        document["model_fingerprint"], document["development"]["objective"],
        document["development"]["fingerprint"], document["search"]["fingerprint"],
        document["search"]["trial_name"], supplied_hash,
    )


def _pool_document(task: DecisionTask, pool: Sequence[LabeledItem]) -> list[dict[str, str]]:
    records = []
    ids: set[str] = set()
    texts: set[str] = set()
    for row in pool:
        if row.source != "trusted":
            raise ArtifactValidationError("artifact pool must have trusted labels")
        label = task.validate_label(row.label)
        if label != row.label:
            raise ArtifactValidationError("artifact pool labels must be canonical")
        text = row.item.values.get(task.input_field)
        if not isinstance(text, str):
            raise ArtifactValidationError("artifact pool input must be text")
        if not isinstance(row.item.id, str) or not row.item.id.strip():
            raise ArtifactValidationError("artifact pool IDs must be non-empty strings")
        if row.item.id in ids:
            raise ArtifactValidationError("artifact pool has duplicate IDs")
        ids.add(row.item.id)
        normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
        if normalized in texts:
            raise ArtifactValidationError("artifact pool has duplicate normalized text")
        texts.add(normalized)
        context = json.dumps(dict(row.context), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        records.append({"id": row.item.id, "input_hash": _sha(normalized), "label": row.label,
                        "context_hash": _sha(context)})
    return sorted(records, key=lambda record: record["id"])


def _pool_fingerprint(task: DecisionTask, pool: Sequence[LabeledItem]) -> str:
    records = _pool_document(task, pool)
    return _sha(_canonical(sorted((record["id"], record["label"], record["input_hash"], record["context_hash"])
                                  for record in records)))


def _policy_document(metadata: PolicyMetadata, fingerprint: str) -> dict[str, Any]:
    return {"name": metadata.name, "version": metadata.version, "selection_mode": metadata.selection_mode,
            "configuration": dict(metadata.configuration), "fingerprint": fingerprint}


def _policy_from_document(value: Any) -> Any:
    _require_exact_keys(value, {"name", "version", "selection_mode", "configuration", "fingerprint"}, "policy")
    if (not all(isinstance(value[key], str) and value[key] for key in ("name", "version", "selection_mode", "fingerprint"))
            or not isinstance(value["configuration"], dict)):
        raise ArtifactValidationError("policy is invalid")
    return _policy_from_metadata(PolicyMetadata(value["name"], value["version"], value["selection_mode"],
                                                value["configuration"]), value["fingerprint"])


def _policy_from_metadata(metadata: PolicyMetadata, fingerprint: str) -> Any:
    if metadata.version != POLICY_VERSION:
        raise ArtifactValidationError("policy version is not supported")
    configs = {
        "random-balanced": lambda config: RandomBalanced(seed=config["seed"]),
        "prototype-balanced": lambda config: PrototypeBalanced(),
        "per-label-lexical-retrieval": lambda config: PerLabelLexicalRetrieval(),
        "fixed-example-list": FixedExampleList.from_configuration,
    }
    constructor = configs.get(metadata.name)
    if constructor is None:
        raise ArtifactValidationError("policy type is not supported")
    expected_config_keys = {"random-balanced": {"seed"},
                            "fixed-example-list": {"examples", "reserves"}}.get(metadata.name, set())
    if not isinstance(metadata.configuration, dict) or set(metadata.configuration) != expected_config_keys:
        raise ArtifactValidationError("policy configuration is not supported")
    if (metadata.name == "random-balanced"
            and (isinstance(metadata.configuration["seed"], bool)
                 or not isinstance(metadata.configuration["seed"], int))):
        raise ArtifactValidationError("policy configuration is invalid")
    try:
        policy = constructor(metadata.configuration)
    except (KeyError, TypeError, ValueError) as error:
        raise ArtifactValidationError("policy configuration is invalid") from error
    if policy.metadata != metadata or policy.fingerprint != fingerprint:
        raise ArtifactValidationError("policy metadata or fingerprint does not match supported policy")
    return policy


def _budget_document(budget: ContextBudget) -> dict[str, int | None]:
    return {"per_label": budget.per_label, "max_tokens": budget.max_tokens,
            "provider_token_limit": budget.provider_token_limit}


def _budget_from_document(value: Any) -> ContextBudget:
    _require_exact_keys(value, {"per_label", "max_tokens", "provider_token_limit"}, "budget")
    try:
        return ContextBudget(**value)
    except (TypeError, ValueError) as error:
        raise ArtifactValidationError("budget is invalid") from error


def _validate_document(value: Any) -> None:
    _require_exact_keys(value, _TOP_KEYS, "artifact")
    if isinstance(value["artifact_version"], bool) or value["artifact_version"] != ARTIFACT_VERSION:
        raise ArtifactValidationError("artifact version is not supported")
    if not _is_hex_digest(value["artifact_hash"]):
        raise ArtifactValidationError("artifact checksum is invalid")
    if (not _is_hex_digest(value["task_fingerprint"])
            or not _is_hex_digest(value["candidate_pool_fingerprint"])
            or not isinstance(value["model_fingerprint"], str) or not value["model_fingerprint"]):
        raise ArtifactValidationError("artifact fingerprints must be strings")
    _require_exact_keys(value["pool"], {"revision", "items"}, "pool")
    if (not isinstance(value["pool"]["revision"], str) or not value["pool"]["revision"]
            or not isinstance(value["pool"]["items"], list)):
        raise ArtifactValidationError("pool is invalid")
    for item in value["pool"]["items"]:
        _require_exact_keys(item, {"id", "input_hash", "label", "context_hash"}, "pool item")
        if not all(isinstance(item[key], str) and item[key] for key in item):
            raise ArtifactValidationError("pool item is invalid")
    _require_exact_keys(value["development"], {"objective_name", "objective", "fingerprint"}, "development")
    if (not isinstance(value["development"]["objective_name"], str)
            or value["development"]["objective_name"] not in {"accuracy", "macro-f1", "brier"}
            or isinstance(value["development"]["objective"], bool)
            or not isinstance(value["development"]["objective"], (int, float))
            or not math.isfinite(value["development"]["objective"])):
        raise ArtifactValidationError("development objective is invalid")
    if not _is_hex_digest(value["development"]["fingerprint"]):
        raise ArtifactValidationError("development fingerprint is invalid")
    _require_exact_keys(value["search"], {"fingerprint", "trial_name"}, "search")
    if not _is_hex_digest(value["search"]["fingerprint"]) or not isinstance(value["search"]["trial_name"], str) or not value["search"]["trial_name"]:
        raise ArtifactValidationError("search provenance is invalid")
    if (not isinstance(value["display_order"], str)
            or value["display_order"] not in {"canonical", "interleaved", "reversed", "shuffled"}):
        raise ArtifactValidationError("display order is invalid")
    if isinstance(value["order_seed"], bool) or not isinstance(value["order_seed"], int) or value["order_seed"] < 0:
        raise ArtifactValidationError("order seed is invalid")
    if not isinstance(value["presentation_label_order"], list) or not all(isinstance(label, str) for label in value["presentation_label_order"]):
        raise ArtifactValidationError("presentation label order is invalid")
    _policy_from_document(value["policy"])
    _budget_from_document(value["budget"])


def _require_exact_keys(value: Any, expected: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ArtifactValidationError(f"{name} has unexpected or missing fields")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash(value: dict[str, Any]) -> str:
    return _sha(_canonical(value))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_hex_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactValidationError(f"artifact JSON contains duplicate key {key!r}")
        result[key] = value
    return result
