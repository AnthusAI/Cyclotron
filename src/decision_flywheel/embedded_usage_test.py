"""An application can state what its cyclotron's model calls cost, per window of decisions."""

import pytest

from .batched_classification import BatchedAnswers
from .embedded_cyclotron import Cyclotron, CyclotronDefinition
from .embedded_cyclotron_test import RELEVANT, item, run
from .models import DecisionResult
from .optimizer_agent import OptimizerAgent, OptimizerReply

PRICES = {
    "decision_model": {"input_usd_per_mtok": 0.042, "output_usd_per_mtok": 0.0},
    "optimizer": {"input_usd_per_mtok": 0.40, "cached_input_usd_per_mtok": 0.10, "output_usd_per_mtok": 1.60},
}


class Model:
    model_identity = "metered"

    def __init__(self):
        self.calls = 0

    async def classify_many(self, configs, target, training, **kwargs):
        self.calls += 1
        p = 0.8 if "yes" in target.values["text"] else 0.3
        return BatchedAnswers(
            {
                cid: {"decision": DecisionResult("include" if p > 0.5 else "exclude", {"include": p, "exclude": 1 - p})}
                for cid in configs
            },
            "metered",
            {"input_tokens": 1000, "output_tokens": 10},
            1,
        )


def test_paid_decision_requests_are_counted_per_window_and_cached_answers_are_free(tmp_path):
    definition = CyclotronDefinition("papyrus-relevance", (RELEVANT,), seed="usage-seed")
    model = Model()
    with Cyclotron.open(tmp_path / "c", definition, model, review_program=None) as cyclotron:
        for index in range(3):
            run(cyclotron.decide(item(f"i-{index}", f"yes {index}")))
        run(cyclotron.decide(item("i-0", "yes 0")))  # unchanged: no model call
        usage = cyclotron.usage(PRICES, window=2)
        log = cyclotron.decision_log()
    assert model.calls == 3
    assert [w["decision_model"]["requests"] for w in usage["windows"]] == [2, 1]
    assert [(w["first_cycle"], w["last_cycle"]) for w in usage["windows"]] == [(1, 2), (3, 3)]
    assert usage["total"]["decision_model"] == {
        "requests": 3,
        "input_tokens": 3000,
        "cached_input_tokens": 0,
        "output_tokens": 30,
        "usd": pytest.approx(3000 * 0.042 / 1e6),
    }
    assert usage["total"]["optimizer"]["requests"] == 0
    assert [row["n"] for row in log] == [1, 2, 3] and log[0]["reviews"] == {}
    assert log[0]["item"] == {"text": "yes 0"}


def test_optimizer_calls_count_toward_the_decision_whose_review_drove_them(tmp_path):
    optimizer = OptimizerAgent(
        lambda _: OptimizerReply(
            '{"rationale":"Editors include yes items","rubric":"Include items that say yes."}',
            "fake-optimizer",
            usage={"prompt_tokens": 2000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 500}},
        )
    )
    definition = CyclotronDefinition(
        "papyrus-relevance", (RELEVANT,), seed="spec-seed", optimize_every=1000, rubric_changes_every=1
    )
    with Cyclotron.open(tmp_path / "c", definition, Model(), optimizer, max_requests=500, review_program=None) as cyclotron:
        for index in range(6):
            decision = run(cyclotron.decide(item(f"learn-{index}", ("yes " if index % 2 else "no ") + str(index))))
            run(cyclotron.review(decision.decision_id, "include" if index % 2 else "exclude", explanation=f"why {index}"))
        usage = cyclotron.usage(PRICES)
        log = cyclotron.decision_log()
        cyclotron.snapshot(tmp_path / "store.tar.gz")
    optimizer_calls = usage["total"]["optimizer"]
    assert optimizer_calls["requests"] >= 1
    assert optimizer_calls["input_tokens"] == 2000 * optimizer_calls["requests"]
    assert optimizer_calls["cached_input_tokens"] == 500 * optimizer_calls["requests"]
    expected = optimizer_calls["requests"] * (1500 * 0.40 + 500 * 0.10 + 50 * 1.60) / 1e6
    assert optimizer_calls["usd"] == pytest.approx(expected)
    assert usage["total"]["decision_model"]["requests"] >= 6  # learning may re-ask about training items
    assert usage["windows"][0]["wall_clock_seconds"] >= 0
    assert log[1]["reviews"]["relevant"]["explanation"] == "why 1"
    Cyclotron.restore(tmp_path / "store.tar.gz", tmp_path / "restored")
    with Cyclotron.open(tmp_path / "restored", definition, Model(), review_program=None) as restored:
        assert restored.usage(PRICES)["total"] == usage["total"]


def test_usage_needs_both_prices(tmp_path):
    with Cyclotron.open(tmp_path / "c", CyclotronDefinition("x", (RELEVANT,)), Model()) as cyclotron:
        with pytest.raises(ValueError, match="prices"):
            cyclotron.usage({"decision_model": PRICES["decision_model"]})
        assert cyclotron.usage(PRICES)["windows"] == []


def _two_outputs(**options):
    from .embedded_cyclotron import ClassifierSpec

    timely = ClassifierSpec("timely", ("include", "exclude"), "Is this timely?", positive_label="include")
    return CyclotronDefinition("newsroom", (RELEVANT, timely), seed="usage-seed", **options)


def _sums_to_total(usage):
    outputs = usage["outputs"]["by_output"].values()
    for model in ("decision_model", "optimizer"):
        for key in ("requests", "input_tokens", "cached_input_tokens", "output_tokens"):
            assert sum(o[model][key] for o in outputs) == pytest.approx(usage["total"][model][key])
    assert sum(o["usd"] for o in outputs) == pytest.approx(usage["total"]["usd"], abs=1e-5)


def test_decision_model_usage_is_split_by_each_outputs_share_of_the_request(tmp_path):
    long_rubric = "Include sources about defending AI systems. " * 40
    with Cyclotron.open(
        tmp_path / "c", _two_outputs(), Model(), review_program=None, seed_rubrics={"relevant": long_rubric}
    ) as cyclotron:
        for index in range(3):
            run(cyclotron.decide(item(f"i-{index}", f"yes {index}")))
        usage = cyclotron.usage(PRICES)
    by_output = usage["outputs"]["by_output"]
    assert "estimate" in usage["outputs"] and "share of the shared request bytes" in usage["outputs"]["estimate"]
    assert by_output["relevant"]["decision_model"]["input_tokens"] > 2 * by_output["timely"]["decision_model"]["input_tokens"]
    _sums_to_total(usage)


def test_a_single_output_is_charged_the_whole_total(tmp_path):
    with Cyclotron.open(tmp_path / "c", CyclotronDefinition("x", (RELEVANT,)), Model(), review_program=None) as cyclotron:
        run(cyclotron.decide(item("i-0", "yes 0")))
        usage = cyclotron.usage(PRICES)
    assert usage["outputs"]["by_output"]["relevant"]["decision_model"] == {**usage["total"]["decision_model"]}
    _sums_to_total(usage)


def test_optimizer_usage_belongs_to_the_output_whose_learning_made_the_calls(tmp_path):
    calls = []

    def complete(messages):
        calls.append(messages)
        return OptimizerReply(
            '{"rationale":"Editors include yes items","rubric":"Include items that say yes."}',
            "fake-optimizer",
            usage={"prompt_tokens": 2000, "completion_tokens": 50},
        )

    optimizer = OptimizerAgent(complete)
    with Cyclotron.open(
        tmp_path / "c",
        _two_outputs(optimize_every=1000, rubric_changes_every=1),
        Model(),
        optimizer,
        max_requests=500,
        review_program=None,
    ) as cyclotron:
        for index in range(12):
            decision = run(cyclotron.decide(item(f"learn-{index}", ("yes " if index % 2 else "no ") + str(index))))
            run(cyclotron.review(decision.decision_id, "include" if index % 2 else "exclude", classifier="relevant"))
        usage = cyclotron.usage(PRICES)
    by_output = usage["outputs"]["by_output"]
    assert by_output["relevant"]["optimizer"]["requests"] >= 1 and by_output["timely"]["optimizer"]["requests"] == 0
    # Each call is counted once, not once per output that shares the optimizer.
    assert usage["total"]["optimizer"]["requests"] == len(calls)
    _sums_to_total(usage)


def test_a_request_recorded_before_shares_were_kept_is_split_evenly(tmp_path):
    with Cyclotron.open(tmp_path / "c", _two_outputs(), Model(), review_program=None) as cyclotron:
        run(cyclotron.decide(item("i-0", "yes 0")))
        with cyclotron.db:
            cyclotron.db.execute("DELETE FROM usage_shares")
        usage = cyclotron.usage(PRICES)
    by_output = usage["outputs"]["by_output"]
    assert by_output["relevant"]["decision_model"]["input_tokens"] == by_output["timely"]["decision_model"]["input_tokens"] == 500
    _sums_to_total(usage)


def test_deferred_learning_usage_is_counted(tmp_path):
    optimizer = OptimizerAgent(
        lambda _: OptimizerReply(
            '{"rationale":"Editors include yes items","rubric":"Include items that say yes."}',
            "fake-optimizer",
            usage={"prompt_tokens": 2000, "completion_tokens": 50},
        )
    )
    with Cyclotron.open(
        tmp_path / "c",
        _two_outputs(optimize_every=1000, rubric_changes_every=1),
        Model(),
        optimizer,
        max_requests=500,
        review_program=None,
        learning="deferred",
    ) as cyclotron:
        for index in range(12):
            decision = run(cyclotron.decide(item(f"learn-{index}", ("yes " if index % 2 else "no ") + str(index))))
            run(cyclotron.review(decision.decision_id, "include" if index % 2 else "exclude", classifier="relevant"))
        before = cyclotron.usage(PRICES)["total"]["optimizer"]["requests"]
        run(cyclotron.learn())
        usage = cyclotron.usage(PRICES)
    assert before == 0 and usage["total"]["optimizer"]["requests"] >= 1
    assert usage["outputs"]["by_output"]["relevant"]["optimizer"]["requests"] == usage["total"]["optimizer"]["requests"]
    _sums_to_total(usage)
