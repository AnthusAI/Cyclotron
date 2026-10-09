"""Offline fixture: reviewed steering promotes only measured development improvement."""
import json

from decision_flywheel.context import RandomBalanced
from decision_flywheel.feedback import Element, Feature, FeedbackItem, LABEL_SOURCE_FINAL, Cyclotron
from decision_flywheel.head import HeadRow, fit_learned_head
from decision_flywheel.models import DecisionTask
from decision_flywheel.steering import ScriptedMockManager, run_steering_round


HASH = "a" * 64


def test_an_accepted_offline_proposal_refits_full_candidate_features_and_improves_disjoint_development_accuracy():
    task = DecisionTask("review", ("approve", "reject"), "Choose.")
    rows = tuple(
        HeadRow(
            f"train-{index}",
            FeedbackItem(f"feedback-{index}", f"train-{index}", "review", final_answer_value=label,
                         label_source=LABEL_SOURCE_FINAL, selection_propensity=0.5),
            (Feature("signal", 0.0, "tone"), Feature("clarity", clarity, "clarity")),
        )
        for index, (label, clarity) in enumerate((("approve", 1.0), ("reject", -1.0)) * 3, 1)
    )
    development_rows = (("approve", {"signal": 0.0, "clarity": 1.0}),
                        ("reject", {"signal": 0.0, "clarity": -1.0}),
                        ("approve", {"signal": 0.0, "clarity": 1.0}),
                        ("reject", {"signal": 0.0, "clarity": -1.0}))
    policy = RandomBalanced(1)
    cyclotron = Cyclotron("review", 1, (Element("tone", "binary", ("signal",), HASH),), policy.fingerprint)

    def accuracy(head, values):
        return sum(head.predict(features) == label for label, features in values) / len(values)

    incumbent = fit_learned_head(
        task,
        tuple(HeadRow(row.item_id, row.feedback, (row.features[0],)) for row in rows),
        declared_features=("signal",), development_ids=("dev-1", "dev-2", "dev-3", "dev-4"),
        scoreboard_ids=("score",), cyclotron_fingerprint=cyclotron.fingerprint,
        policy_fingerprint=policy.fingerprint, context_artifact_fingerprint="c" * 64,
        source_model_provenance="offline", folds=2,
    )
    incumbent_accuracy = accuracy(incumbent, tuple((label, {"signal": values["signal"]})
                                                    for label, values in development_rows))

    def scripted_factory(manager):
        def parse(briefing):
            return json.loads(manager.next_reply())

        return parse

    def fit(candidate, candidate_policy, declared_features):
        return fit_learned_head(task, rows, declared_features=declared_features,
                                development_ids=("dev-1", "dev-2", "dev-3", "dev-4"),
                                scoreboard_ids=("score",), cyclotron_fingerprint=candidate.fingerprint,
                                policy_fingerprint=candidate_policy.fingerprint, context_artifact_fingerprint="c" * 64,
                                source_model_provenance="offline", folds=2)

    observed = []

    def evaluate(candidate, candidate_policy, refit):
        observed.append((candidate, candidate_policy, refit))
        assert refit.feature_names == ("signal", "clarity")
        assert refit.provenance.cyclotron_fingerprint == candidate.fingerprint
        assert refit.provenance.policy_fingerprint == candidate_policy.fingerprint
        return accuracy(refit, development_rows)

    outcome = run_steering_round(
        cyclotron, policy, analyst_factory=scripted_factory,
        mock_manager=ScriptedMockManager([json.dumps({"add_element": {"key": "clarity", "question_type": "binary", "features": ["clarity"]}})]),
        developer_ids=tuple(row.item_id for row in rows), developer_hashes=(HASH,), protected_ids=("score",),
        protected_hashes=(), human_accept=lambda _: True, development_objective=evaluate,
        incumbent_development_objective=incumbent_accuracy, numerical_fitter=fit,
    )
    assert outcome.promoted and outcome.numerical_refit.provenance.cyclotron_fingerprint == outcome.cyclotron.fingerprint
    assert outcome.numerical_refit.feature_names == ("signal", "clarity")
    assert observed == [(outcome.cyclotron, outcome.policy, outcome.numerical_refit)]
    assert incumbent_accuracy < accuracy(outcome.numerical_refit, development_rows) == 1.0
