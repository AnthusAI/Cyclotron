import asyncio
import hashlib

import pytest

from .context import PolicyMetadata
from .models import DecisionResult, DecisionTask, Item, LabeledItem, ModelCapabilities
from .optimizer import TrialSpec, search_context_policies


TASK = DecisionTask("topic", ("yes", "no"), "Choose one label.")
CANDIDATES = [
    LabeledItem(Item("yes-good", {"text": "yes good demonstration"}), "yes"),
    LabeledItem(Item("no-good", {"text": "no good demonstration"}), "no"),
    LabeledItem(Item("yes-bad", {"text": "yes bad demonstration"}), "yes"),
    LabeledItem(Item("no-bad", {"text": "no bad demonstration"}), "no"),
]
DEVELOPMENT = [
    LabeledItem(Item("dev-yes", {"text": "positive development"}), "yes"),
    LabeledItem(Item("dev-no", {"text": "negative development"}), "no"),
]


class MarkerPolicy:
    def __init__(self, marker):
        self.name = f"{marker}-policy"
        self.metadata = PolicyMetadata(self.name, "test-v1", "fixed-global", {"marker": marker})
        self.fingerprint = self.metadata.fingerprint
        self.marker = marker

    def select(self, task, target, candidates, *, per_label):
        return [
            candidate for label in task.labels for candidate in candidates
            if candidate.label == label and self.marker in candidate.item.id
        ][: per_label] + [
            candidate for label in task.labels[1:] for candidate in candidates
            if candidate.label == label and self.marker in candidate.item.id
        ][: per_label]


class ScriptedModel:
    name = "scripted-model-v1"
    fingerprint = "scripted-model-config-v1"
    capabilities = ModelCapabilities()

    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    async def decide(self, task, target, context):
        self.calls.append((target.id, tuple(example.item.id for example in context)))
        if self.fail:
            raise RuntimeError("scripted failure")
        is_good = all("good" in example.item.id for example in context)
        if is_good:
            return DecisionResult("yes" if target.id == "dev-yes" else "no")
        return DecisionResult("no")


def _trials():
    return (
        TrialSpec(MarkerPolicy("bad"), per_label=1, name="bad"),
        TrialSpec(MarkerPolicy("good"), per_label=1, name="good"),
    )


def test_optimizer_freezes_the_development_best_trial_with_a_declared_metric():
    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(), _trials(),
        max_model_calls=10, objective="macro-f1",
    ))

    assert result.objective == "macro-f1"
    assert result.winner.trial_name == "good"
    assert result.winner.objective == 1.0
    assert [trial.status for trial in result.trials] == ["completed", "completed"]
    assert result.model_calls_attempted == 4
    assert result.model_calls_succeeded == 4


def test_optimizer_replays_checkpointed_successes_without_duplicate_model_calls():
    checkpoint = {}
    first_model = ScriptedModel()
    first = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, first_model, _trials(),
        max_model_calls=10, checkpoint=checkpoint,
    ))
    replay_model = ScriptedModel()
    replay = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, replay_model, _trials(),
        max_model_calls=10, checkpoint=checkpoint,
    ))

    assert first.winner.trial_name == replay.winner.trial_name
    assert first.winner.objective == replay.winner.objective
    assert len(first_model.calls) == 4
    assert replay_model.calls == []
    assert replay.model_calls_attempted == 0
    assert all("positive development" not in str(value) for value in checkpoint.values())


def test_optimizer_emits_text_free_live_events_for_trials_requests_and_cache_reuse():
    events = []
    checkpoint = {}
    asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(), _trials(), max_model_calls=10,
        checkpoint=checkpoint, event_sink=events.append,
    ))
    replay_events = []
    asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(), _trials(), max_model_calls=10,
        checkpoint=checkpoint, event_sink=replay_events.append,
    ))

    assert [event.event_type for event in events][0] == "round-started"
    assert "decision-requested" in [event.event_type for event in events]
    assert [event.event_type for event in events][-1] == "round-completed"
    assert "decision-reused" in [event.event_type for event in replay_events]
    assert all("development" not in str(event).casefold() for event in events + replay_events)


def test_optimizer_counts_failed_attempts_and_does_not_freeze_incomplete_trials():
    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(fail=True), _trials(), max_model_calls=1,
    ))

    assert result.model_calls_attempted == 1
    assert result.model_calls_succeeded == 0
    assert result.winner is None
    assert result.trials[0].status == "incomplete"
    assert result.trials[1].status == "incomplete"
    assert result.trials[0].failure_count == 1


def test_optimizer_rejects_candidate_development_id_or_normalized_text_overlap():
    overlapping_id = [LabeledItem(Item("yes-good", {"text": "separate"}), "yes"), *DEVELOPMENT]
    overlapping_text = [
        LabeledItem(Item("other", {"text": " YES\nGOOD demonstration "}), "yes"),
        DEVELOPMENT[1],
    ]

    for development in (overlapping_id, overlapping_text):
        with pytest.raises(ValueError, match="overlap"):
            asyncio.run(search_context_policies(
                TASK, CANDIDATES, development, ScriptedModel(), _trials(), max_model_calls=10,
            ))


def test_optimizer_rejects_untrusted_and_protected_development_records_without_labels():
    untrusted = [LabeledItem(DEVELOPMENT[0].item, "yes", source="untrusted"), DEVELOPMENT[1]]
    protected_hash = hashlib.sha256("positive development".encode("utf-8")).hexdigest()

    with pytest.raises(ValueError, match="trusted"):
        asyncio.run(search_context_policies(
            TASK, CANDIDATES, untrusted, ScriptedModel(), _trials(), max_model_calls=10,
        ))
    with pytest.raises(ValueError, match="protected"):
        asyncio.run(search_context_policies(
            TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(), _trials(), max_model_calls=10,
            protected_text_hashes=(protected_hash,),
        ))


def test_checkpoint_key_changes_with_model_identity_not_the_policy_name_alone():
    checkpoint = {}
    asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(), (TrialSpec(MarkerPolicy("good"), 1),),
        max_model_calls=10, checkpoint=checkpoint, model_fingerprint="model-config-a",
    ))
    changed_model = ScriptedModel()
    asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, changed_model, (TrialSpec(MarkerPolicy("good"), 1),),
        max_model_calls=10, checkpoint=checkpoint, model_fingerprint="model-config-b",
    ))

    assert len(changed_model.calls) == len(DEVELOPMENT)


def test_optimizer_supports_a_zero_shot_trial_without_calling_its_selector():
    class NeverSelect:
        name = "zero-shot"
        metadata = PolicyMetadata(name, "test-v1", "fixed-global", {})
        fingerprint = metadata.fingerprint

        def select(self, *args, **kwargs):
            raise AssertionError("a zero-shot trial must not invoke selection")

    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(), (TrialSpec(NeverSelect(), 0),),
        max_model_calls=10,
    ))

    assert result.winner.status == "completed"
    assert result.winner.per_label == 0


def test_a_completed_trial_is_only_provisional_while_another_declared_trial_is_incomplete():
    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(), _trials(), max_model_calls=2,
    ))

    assert result.provisional_best.trial_name == "bad"
    assert result.winner is None
    assert result.trials[1].status == "incomplete"
    assert result.trials[1].failure_reasons == ("call-budget-exhausted",)


def test_a_later_trial_can_finish_from_cache_after_the_call_ceiling_is_exhausted():
    trials = (
        TrialSpec(MarkerPolicy("good"), 1, name="first"),
        TrialSpec(MarkerPolicy("good"), 1, name="second"),
    )
    model = ScriptedModel()

    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, model, trials, max_model_calls=2,
    ))

    assert len(model.calls) == 2
    assert [trial.status for trial in result.trials] == ["completed", "completed"]
    assert all(decision.from_checkpoint for decision in result.trials[1].decisions)
    assert result.winner is not None


def test_cumulative_call_accounting_prevents_a_resume_from_resetting_the_ceiling():
    accounting = {}
    first = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(fail=True), (TrialSpec(MarkerPolicy("good"), 1),),
        max_model_calls=1, call_accounting=accounting,
    ))
    resumed_model = ScriptedModel()
    resumed = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, resumed_model, (TrialSpec(MarkerPolicy("good"), 1),),
        max_model_calls=1, call_accounting=accounting,
    ))

    assert first.cumulative_model_calls_attempted == 1
    assert accounting["model_calls_attempted"] == 1
    assert accounting["search_fingerprint"] == first.search_fingerprint
    assert resumed.model_calls_attempted == 0
    assert resumed.cumulative_model_calls_attempted == 1
    assert resumed_model.calls == []


def test_trial_history_records_complete_budget_and_safe_failure_categories_only():
    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(fail=True),
        (TrialSpec(MarkerPolicy("good"), 1),), max_model_calls=10,
    ))
    trial = result.trials[0]

    assert trial.budget.per_label == 1
    assert trial.budget.max_tokens is None
    assert trial.failure_reasons == ("model-failure",)
    assert "scripted failure" not in str(trial)


def test_optimizer_result_binds_task_candidate_and_development_provenance_without_row_order():
    original = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(), (TrialSpec(MarkerPolicy("good"), 1),),
        max_model_calls=0,
    ))
    reordered_pool = asyncio.run(search_context_policies(
        TASK, list(reversed(CANDIDATES)), DEVELOPMENT, ScriptedModel(),
        (TrialSpec(MarkerPolicy("good"), 1),), max_model_calls=0,
    ))
    changed_task = asyncio.run(search_context_policies(
        DecisionTask("topic", ("yes", "no"), "Changed instructions."), CANDIDATES, DEVELOPMENT,
        ScriptedModel(), (TrialSpec(MarkerPolicy("good"), 1),), max_model_calls=0,
    ))
    changed_pool = [LabeledItem(Item("yes-good", {"text": "changed demonstration"}), "yes"), *CANDIDATES[1:]]
    changed_development = [LabeledItem(DEVELOPMENT[0].item, "no"), DEVELOPMENT[1]]
    pool_changed = asyncio.run(search_context_policies(
        TASK, changed_pool, DEVELOPMENT, ScriptedModel(), (TrialSpec(MarkerPolicy("good"), 1),),
        max_model_calls=0,
    ))
    development_changed = asyncio.run(search_context_policies(
        TASK, CANDIDATES, changed_development, ScriptedModel(), (TrialSpec(MarkerPolicy("good"), 1),),
        max_model_calls=0,
    ))

    assert original.candidate_pool_fingerprint == reordered_pool.candidate_pool_fingerprint
    assert original.search_fingerprint == reordered_pool.search_fingerprint
    assert original.task_fingerprint != changed_task.task_fingerprint
    assert original.candidate_pool_fingerprint != pool_changed.candidate_pool_fingerprint
    assert original.development_split_fingerprint != development_changed.development_split_fingerprint
    assert "good demonstration" not in str(original)


def test_cumulative_accounting_rejects_a_different_search_scope():
    accounting = {}
    asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(), (TrialSpec(MarkerPolicy("good"), 1),),
        max_model_calls=1, call_accounting=accounting,
    ))

    with pytest.raises(ValueError, match="search fingerprint"):
        asyncio.run(search_context_policies(
            TASK, [LabeledItem(Item("yes-good", {"text": "changed"}), "yes"), *CANDIDATES[1:]],
            DEVELOPMENT, ScriptedModel(),
            (TrialSpec(MarkerPolicy("good"), 1),), max_model_calls=1, call_accounting=accounting,
        ))


# ---- the Brier objective --------------------------------------------------------------------

class ProbabilityModel:
    """Confident and right with 'good' examples; unsure with 'bad' ones."""

    name = "probability-model-v1"
    fingerprint = "probability-model-config-v1"
    capabilities = ModelCapabilities(supports_probability_distributions=True)

    def __init__(self, *, omit_probabilities=False):
        self.calls = []
        self.omit_probabilities = omit_probabilities

    async def decide(self, task, target, context):
        self.calls.append(target.id)
        right = "yes" if target.id == "dev-yes" else "no"
        wrong = "no" if right == "yes" else "yes"
        if self.omit_probabilities:
            return DecisionResult(right)
        p = 0.9 if all("good" in example.item.id for example in context) else 0.6
        return DecisionResult(right, probabilities={right: p, wrong: 1 - p})


def test_the_brier_objective_scores_probabilities_and_prefers_the_lower_score():
    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ProbabilityModel(), _trials(), max_model_calls=10, objective="brier"))

    scores = {trial.trial_name: trial.objective for trial in result.trials}
    assert scores["good"] == pytest.approx(0.01)   # label-averaged: (0.1^2 + 0.1^2) / 2 per item
    assert scores["bad"] == pytest.approx(0.16)
    assert result.winner.trial_name == "good"
    assert result.winner.decisions[0].probabilities == {"yes": 0.9, "no": pytest.approx(0.1)}


def test_brier_checkpoint_entries_carry_probabilities_and_replay_without_new_calls():
    checkpoint = {}
    first = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ProbabilityModel(), _trials(), max_model_calls=10,
        objective="brier", checkpoint=checkpoint))
    assert all(set(entry) == {"label", "probabilities"} for entry in checkpoint.values())

    replay_model = ProbabilityModel()
    second = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, replay_model, _trials(), max_model_calls=10,
        objective="brier", checkpoint=checkpoint))
    assert replay_model.calls == []
    assert [t.objective for t in second.trials] == [t.objective for t in first.trials]


def test_accuracy_checkpoints_still_store_only_the_label():
    checkpoint = {}
    asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ProbabilityModel(), _trials(), max_model_calls=10, checkpoint=checkpoint))
    assert all(entry.keys() == {"label"} for entry in checkpoint.values())


def test_a_label_only_checkpoint_entry_is_re_asked_under_the_brier_objective():
    checkpoint = {}
    asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ProbabilityModel(), _trials(), max_model_calls=10, checkpoint=checkpoint))
    model = ProbabilityModel()
    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, model, _trials(), max_model_calls=10, objective="brier",
        checkpoint=checkpoint, missing_probabilities="incomplete"))
    assert len(model.calls) == 4 and result.winner.trial_name == "good"


def test_a_brier_trial_without_probabilities_is_incomplete_and_cannot_win():
    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ProbabilityModel(omit_probabilities=True), _trials(),
        max_model_calls=10, objective="brier", missing_probabilities="incomplete"))
    assert {trial.status for trial in result.trials} == {"incomplete"}
    assert result.trials[0].failure_reasons == ("missing-probabilities",)
    assert result.winner is None


def test_label_only_checkpoints_fall_back_to_accuracy_without_paid_rescoring():
    from dataclasses import asdict
    checkpoint = {}
    original = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(), _trials(), max_model_calls=10,
        checkpoint=checkpoint))
    model = ScriptedModel()
    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, model, _trials(), max_model_calls=0,
        objective="brier", checkpoint=checkpoint))
    assert model.calls == []
    assert result.objective == "accuracy"
    assert result.requested_objective == "brier"
    assert result.fallback_reason == "missing-probabilities"
    assert result.fallback_trials == ("bad", "good")
    assert [t.objective for t in result.trials] == [t.objective for t in original.trials]
    assert result.winner.trial_name == "good"
    assert all(d.from_checkpoint for trial in result.trials for d in trial.decisions)
    assert "requested_objective" in asdict(result)
    assert "requested_objective" not in asdict(original)


def test_one_missing_distribution_changes_the_whole_search_to_accuracy_not_mixed_scores():
    class MixedModel(ProbabilityModel):
        async def decide(self, task, target, context):
            if any("bad" in row.item.id for row in context):
                self.calls.append(target.id)
                return DecisionResult("no")
            return await super().decide(task, target, context)
    checkpoint = {}
    model = MixedModel()
    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, model, _trials(), max_model_calls=4,
        objective="brier", checkpoint=checkpoint))
    assert len(model.calls) == 4
    assert result.objective == "accuracy" and result.fallback_trials == ("bad",)
    assert {trial.trial_name:trial.objective for trial in result.trials} == {"bad":.5,"good":1.}
    assert result.winner.trial_name == "good"
    replay = MixedModel()
    repeated = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, replay, _trials(), max_model_calls=0,
        objective="brier", checkpoint=checkpoint))
    assert replay.calls == [] and repeated.fallback_reason == "missing-probabilities"
    assert [t.objective for t in repeated.trials] == [.5,1.]


def test_fallback_never_turns_an_exhausted_call_budget_into_a_complete_winner():
    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ScriptedModel(), _trials(), max_model_calls=1,
        objective="brier"))
    assert result.objective == "accuracy"
    assert result.winner is None
    assert result.model_calls_attempted == 1
    assert "call-budget-exhausted" in result.trials[0].failure_reasons


def test_multiclass_brier_uses_all_returned_class_probabilities():
    task = DecisionTask("topics", ("a","b","c"), "Choose a topic")
    development = [LabeledItem(Item("one",{"text":"First"}),"a"),
                   LabeledItem(Item("two",{"text":"Second"}),"b")]
    class ThreeClassModel(ProbabilityModel):
        async def decide(self, task, target, context):
            return DecisionResult("a",{"a":.7,"b":.2,"c":.1}) if target.id == "one" else DecisionResult("b",{"a":.1,"b":.6,"c":.3})
    candidates = [LabeledItem(Item("candidate",{"text":"Training example"}),"a")]
    result = asyncio.run(search_context_policies(task, candidates, development, ThreeClassModel(),
        [TrialSpec(MarkerPolicy("good"),0)], max_model_calls=2, objective="brier"))
    assert result.objective == "brier"
    assert result.winner.objective == pytest.approx((.14+.26)/6)


def test_call_accounting_cannot_be_reused_under_a_different_missing_probability_policy():
    checkpoint, accounting = {}, {}
    asyncio.run(search_context_policies(TASK, CANDIDATES, DEVELOPMENT, ProbabilityModel(), _trials(),
        max_model_calls=4, objective="brier", missing_probabilities="incomplete",
        checkpoint=checkpoint, call_accounting=accounting))
    model = ProbabilityModel()
    with pytest.raises(ValueError, match="search fingerprint"):
        asyncio.run(search_context_policies(TASK, CANDIDATES, DEVELOPMENT, model, _trials(),
            max_model_calls=4, objective="brier", checkpoint=checkpoint, call_accounting=accounting))
    assert model.calls == []


def test_an_unknown_missing_probability_policy_is_rejected_before_model_calls():
    model = ProbabilityModel()
    with pytest.raises(ValueError, match="missing_probabilities"):
        asyncio.run(search_context_policies(TASK, CANDIDATES, DEVELOPMENT, model, _trials(),
            max_model_calls=4, objective="brier", missing_probabilities="guess"))
    assert model.calls == []


@pytest.mark.parametrize("entry", [{"label": "yes", "probabilities": {"yes": 1.2, "no": -0.2}},
                                   {"label": "yes", "probabilities": {"yes": 1.0}},
                                   {"label": "yes", "extra": 1}])
def test_a_malformed_checkpoint_entry_is_refused(entry):
    checkpoint = {}
    asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, ProbabilityModel(), _trials(), max_model_calls=10,
        objective="brier", checkpoint=checkpoint))
    key = next(iter(checkpoint))
    checkpoint[key] = entry
    with pytest.raises(ValueError):
        asyncio.run(search_context_policies(
            TASK, CANDIDATES, DEVELOPMENT, ProbabilityModel(), _trials(), max_model_calls=10,
            objective="brier", checkpoint=checkpoint))
