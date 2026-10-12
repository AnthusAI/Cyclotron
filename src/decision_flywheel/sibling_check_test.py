import asyncio
from dataclasses import asdict
import hashlib
import json

import pytest

from .classifier_config import ClassifierConfig
from .models import DecisionTask
from .sibling_check import SiblingCheck, SiblingPolicy, install_sibling_checks, sibling_effect


def test_the_default_policy_refuses_any_sibling_regression():
    assert SiblingPolicy() == SiblingPolicy("refuse", 0.)


@pytest.mark.parametrize("options", [{"action": "warn"}, {"max_regression": -.1}, {"max_regression": float("nan")},
                                     {"max_regression": True}])
def test_an_undeclared_action_or_an_invalid_tolerance_is_refused(options):
    with pytest.raises(ValueError):
        SiblingPolicy(**options)


def test_a_drop_beyond_the_tolerance_is_a_regression_and_a_smaller_one_is_not():
    policy = SiblingPolicy(max_regression=.05)
    assert sibling_effect(policy, "f1", .8, .7)["status"] == "regressed"
    assert sibling_effect(policy, "f1", .8, .76)["status"] == "no-regression"
    assert sibling_effect(policy, "f1", .8, .9) == {"status": "no-regression", "metric": "f1", "incumbent_score": .8,
                                                    "candidate_score": .9, "change": pytest.approx(.1)}


class Wheel:
    def __init__(self, config):
        self.active = type("Active", (), {"config": config})()


def test_a_single_output_cyclotron_has_no_sibling_check_and_several_outputs_each_get_one():
    task = DecisionTask("a", ("yes", "no"), "A?")
    one = {"a": Wheel(ClassifierConfig(task))}
    install_sibling_checks(one, None, lambda _: ((), (), ()))
    assert one["a"].sibling_check is None
    two = {"a": Wheel(ClassifierConfig(task)), "b": Wheel(ClassifierConfig(task))}
    install_sibling_checks(two, None, lambda _: ((), (), ()), SiblingPolicy("record"))
    assert two["a"].sibling_check.siblings() == ["b"] and two["b"].sibling_check.siblings() == ["a"]
    assert two["a"].sibling_check.policy.action == "record"


def test_a_candidate_that_leaves_the_shared_request_unchanged_costs_no_sibling_evaluation():
    config = ClassifierConfig(DecisionTask("a", ("yes", "no"), "A?"), rubric="Same.")

    def partitions(_):
        raise AssertionError("an unchanged context needs no sibling evidence")

    check = SiblingCheck({"a": Wheel(config), "b": Wheel(config)}, None, "a", partitions, SiblingPolicy())
    effects = asyncio.run(check.evaluate(config, None))
    assert effects["context_changed"] is False and effects["siblings"] == {} and not effects["refused"]


def test_the_sibling_policy_does_not_change_an_existing_store_identity():
    from .embedded_cyclotron import ClassifierSpec, CyclotronDefinition
    spec = ClassifierSpec("relevant", ("include", "exclude"), "Relevant?")
    definition = CyclotronDefinition("newsroom", (spec,))
    legacy = {key: value for key, value in asdict(definition).items() if key != "sibling_policy"}
    expected = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert definition.fingerprint == expected
    assert CyclotronDefinition("newsroom", (spec,), sibling_policy=SiblingPolicy("record")).fingerprint == expected
    with pytest.raises(ValueError, match="SiblingPolicy"):
        CyclotronDefinition("newsroom", (spec,), sibling_policy="record")
