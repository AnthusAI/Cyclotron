import asyncio
import json

import pytest

from .artifacts import ArtifactValidationError, create_artifact, load_artifact
from .budget import ContextBudget
from .context import PerLabelLexicalRetrieval, PrototypeBalanced, RandomBalanced
from .models import DecisionResult, DecisionTask, Item, LabeledItem, ModelCapabilities
from .optimizer import TrialSpec, search_context_policies


TASK = DecisionTask("topic", ("yes", "no"), "Classify only target.")
POOL = [
    LabeledItem(Item("yes-1", {"text": "yes demonstration", "api_key": "never-persist"}), "yes"),
    LabeledItem(Item("no-1", {"text": "no demonstration", "password": "never-persist"}), "no"),
]
DEVELOPMENT = [
    LabeledItem(Item("dev-yes", {"text": "yes development"}), "yes"),
    LabeledItem(Item("dev-no", {"text": "no development"}), "no"),
]


class ScriptedModel:
    capabilities = ModelCapabilities()

    async def decide(self, task, target, context):
        self.calls = getattr(self, "calls", []) + [(target.id, tuple(item.item.id for item in context))]
        return DecisionResult("yes" if target.id.endswith("yes") else "no")


def _frozen_result():
    return asyncio.run(search_context_policies(
        TASK, POOL, DEVELOPMENT, ScriptedModel(), (TrialSpec(RandomBalanced(seed=5), 1),),
        max_model_calls=2, model_fingerprint="fake-model-v1",
    ))


def test_a_frozen_winner_round_trips_without_text_and_reproduces_context_ids_for_a_new_target():
    serialized = create_artifact(TASK, POOL, "pool-revision-1", _frozen_result())
    artifact = load_artifact(serialized, TASK, list(reversed(POOL)), "pool-revision-1")

    plan = artifact.context_for(Item("new-target", {"text": "unlabeled target"}))
    document = json.loads(serialized)

    assert plan.example_ids == ("no-1", "yes-1")
    assert document["pool"]["items"] == [
        {"id": "no-1", "input_hash": document["pool"]["items"][0]["input_hash"],
         "context_hash": document["pool"]["items"][0]["context_hash"], "label": "no"},
        {"id": "yes-1", "input_hash": document["pool"]["items"][1]["input_hash"],
         "context_hash": document["pool"]["items"][1]["context_hash"], "label": "yes"},
    ]
    assert "demonstration" not in serialized
    assert "never-persist" not in serialized


def test_an_artifact_apply_uses_only_the_injected_model_and_reproduced_context():
    artifact = load_artifact(create_artifact(TASK, POOL, "r1", _frozen_result()), TASK, POOL, "r1")
    model = ScriptedModel()

    result = asyncio.run(artifact.apply(TASK, Item("new-yes", {"text": "new target"}), model,
                                        model_fingerprint="fake-model-v1"))

    assert result.label == "yes"
    assert model.calls == [("new-yes", ("no-1", "yes-1"))]


def test_artifact_apply_validates_a_generic_model_result_against_the_artifact_task():
    class InvalidModel:
        async def decide(self, task, target, context):
            return DecisionResult("outside")

    artifact = load_artifact(create_artifact(TASK, POOL, "r1", _frozen_result()), TASK, POOL, "r1")

    with pytest.raises(ValueError, match="not one of"):
        asyncio.run(artifact.apply(TASK, Item("new", {"text": "new"}), InvalidModel(),
                                   model_fingerprint="fake-model-v1"))


def test_an_artifact_rejects_tampering_unknown_fields_and_incompatible_task_pool_or_revision():
    serialized = create_artifact(TASK, POOL, "r1", _frozen_result())
    changed = json.loads(serialized); changed["model_fingerprint"] = "attacker"
    extra = json.loads(serialized); extra["credential"] = "no"

    for value in (json.dumps(changed), json.dumps(extra)):
        with pytest.raises(ArtifactValidationError):
            load_artifact(value, TASK, POOL, "r1")
    with pytest.raises(ArtifactValidationError, match="task fingerprint"):
        load_artifact(serialized, DecisionTask("other", ("yes", "no"), "Classify only target."), POOL, "r1")
    with pytest.raises(ArtifactValidationError, match="pool revision"):
        load_artifact(serialized, TASK, POOL, "other")
    changed_pool = [LabeledItem(Item("yes-1", {"text": "changed"}), "yes"), POOL[1]]
    with pytest.raises(ArtifactValidationError, match="pool content"):
        load_artifact(serialized, TASK, changed_pool, "r1")


def test_only_a_fully_frozen_winner_can_be_persisted_not_a_provisional_best():
    result = asyncio.run(search_context_policies(
        TASK, POOL, DEVELOPMENT, ScriptedModel(),
        (TrialSpec(RandomBalanced(seed=1), 1), TrialSpec(RandomBalanced(seed=2), 0)),
        max_model_calls=2, model_fingerprint="fake-model-v1",
    ))

    assert result.provisional_best is not None and result.winner is None
    with pytest.raises(ArtifactValidationError, match="frozen winner"):
        create_artifact(TASK, POOL, "r1", result)


def test_artifact_identity_is_pool_order_invariant_but_rejects_policy_version_changes():
    result = _frozen_result()
    forward = create_artifact(TASK, POOL, "r1", result)
    backward = create_artifact(TASK, list(reversed(POOL)), "r1", result)
    changed = json.loads(forward)
    changed["policy"]["version"] = "future"
    changed["artifact_hash"] = "0" * 64

    assert forward == backward
    with pytest.raises(ArtifactValidationError):
        load_artifact(json.dumps(changed), TASK, POOL, "r1")


@pytest.mark.parametrize("mutate", [
    lambda document: document.update(artifact_version=True),
    lambda document: document.update(artifact_hash="g" * 64),
    lambda document: document.update(task_fingerprint=""),
    lambda document: document["development"].update(objective=1.1),
])
def test_strict_artifacts_reject_malformed_versions_hashes_fingerprints_and_scores(mutate):
    document = json.loads(create_artifact(TASK, POOL, "r1", _frozen_result()))
    mutate(document)

    with pytest.raises(ArtifactValidationError):
        load_artifact(json.dumps(document), TASK, POOL, "r1")


def test_strict_artifacts_reject_duplicate_json_keys_and_duplicate_normalized_pool_text():
    serialized = create_artifact(TASK, POOL, "r1", _frozen_result())
    duplicate_key = serialized[:-1] + ',"model_fingerprint":"second"}'
    duplicate_text_pool = [
        LabeledItem(Item("yes-1", {"text": "same"}), "yes"),
        LabeledItem(Item("no-1", {"text": " SAME "}), "no"),
    ]

    with pytest.raises(ArtifactValidationError, match="duplicate key"):
        load_artifact(duplicate_key, TASK, POOL, "r1")
    with pytest.raises(ArtifactValidationError, match="duplicate normalized text"):
        create_artifact(TASK, duplicate_text_pool, "r1", _frozen_result())


def test_artifact_refuses_an_optimizer_winner_from_a_different_task_or_candidate_pool():
    result = _frozen_result()
    other_task = DecisionTask("other", ("yes", "no"), "Classify only target.")
    changed_pool = [LabeledItem(Item("yes-1", {"text": "changed"}), "yes"), POOL[1]]

    with pytest.raises(ArtifactValidationError, match="optimizer task"):
        create_artifact(other_task, POOL, "r1", result)
    with pytest.raises(ArtifactValidationError, match="optimizer candidate pool"):
        create_artifact(TASK, changed_pool, "r1", result)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=5), PrototypeBalanced(), PerLabelLexicalRetrieval()])
def test_every_supported_builtin_policy_round_trips_from_frozen_optimizer_provenance(policy):
    result = asyncio.run(search_context_policies(
        TASK, POOL, DEVELOPMENT, ScriptedModel(), (TrialSpec(policy, 1),),
        max_model_calls=2, model_fingerprint="fake-model-v1",
    ))

    artifact = load_artifact(create_artifact(TASK, POOL, "r1", result), TASK, POOL, "r1")

    assert artifact.policy.metadata == policy.metadata


def test_artifact_checksum_identity_changes_with_budget_seed_order_model_and_task():
    base = _frozen_result()
    variants = [
        asyncio.run(search_context_policies(TASK, POOL, DEVELOPMENT, ScriptedModel(),
                                            (TrialSpec(RandomBalanced(seed=5), 1,
                                                       budget=ContextBudget(per_label=1, max_tokens=100)),),
                                            max_model_calls=2, model_fingerprint="fake-model-v1")),
        asyncio.run(search_context_policies(TASK, POOL, DEVELOPMENT, ScriptedModel(),
                                            (TrialSpec(RandomBalanced(seed=8), 1),), max_model_calls=2,
                                            model_fingerprint="fake-model-v1")),
        asyncio.run(search_context_policies(TASK, POOL, DEVELOPMENT, ScriptedModel(),
                                            (TrialSpec(RandomBalanced(seed=5), 1),), max_model_calls=2,
                                            model_fingerprint="fake-model-v1", display_order="reversed", order_seed=9)),
        asyncio.run(search_context_policies(TASK, POOL, DEVELOPMENT, ScriptedModel(),
                                            (TrialSpec(RandomBalanced(seed=5), 1),),
                                            max_model_calls=2, model_fingerprint="other-model")),
    ]

    fingerprints = {json.loads(create_artifact(TASK, POOL, "r1", base))["artifact_hash"]}
    fingerprints.update(json.loads(create_artifact(TASK, POOL, "r1", result))["artifact_hash"] for result in variants)

    other_task = DecisionTask("other", ("yes", "no"), "Classify only target.")
    other_result = asyncio.run(search_context_policies(
        other_task, POOL, DEVELOPMENT, ScriptedModel(), (TrialSpec(RandomBalanced(seed=5), 1),),
        max_model_calls=2, model_fingerprint="fake-model-v1",
    ))
    fingerprints.add(json.loads(create_artifact(other_task, POOL, "r1", other_result))["artifact_hash"])

    assert len(fingerprints) == 6


def test_a_fixed_example_list_winner_scored_by_brier_round_trips_and_keeps_its_reserve_rule():
    from .context import FixedExampleList

    pool = POOL + [
        LabeledItem(Item("yes-2", {"text": "yes reserve demonstration"}), "yes"),
        LabeledItem(Item("no-2", {"text": "no reserve demonstration"}), "no"),
    ]
    fixed = FixedExampleList.from_items(TASK, pool[:2], pool[2:])

    class ProbabilityModel(ScriptedModel):
        capabilities = ModelCapabilities(supports_probability_distributions=True)

        async def decide(self, task, target, context):
            label = "yes" if target.id.endswith("yes") else "no"
            return DecisionResult(label, probabilities={label: 0.8, ("no" if label == "yes" else "yes"): 0.2})

    result = asyncio.run(search_context_policies(
        TASK, pool, DEVELOPMENT, ProbabilityModel(), (TrialSpec(fixed, 1),),
        max_model_calls=2, model_fingerprint="fake-model-v1", objective="brier"))
    serialized = create_artifact(TASK, pool, "r1", result)
    artifact = load_artifact(serialized, TASK, pool, "r1")

    assert artifact.policy == fixed
    assert json.loads(serialized)["development"]["objective_name"] == "brier"
    assert artifact.context_for(Item("new", {"text": "new target"})).example_ids == ("no-1", "yes-1")
    assert artifact.context_for(pool[0].item).example_ids == ("no-1", "yes-2")
    assert "demonstration" not in serialized
