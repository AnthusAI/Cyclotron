"""The 400-cycle editorial recording's routing metrics and model cost, pinned."""
import importlib.util
import json
from pathlib import Path

import pytest

from decision_flywheel.routing_metrics import Decision, routing
from decision_flywheel.run_usage import usage_by_window

ROOT = Path(__file__).parents[1]
RECORDING = json.loads((ROOT / "tests/fixtures/editorial-run-v1-all.json").read_text())


def _exporter():
    spec = importlib.util.spec_from_file_location("export_editorial_run_fixture", ROOT / "scripts/export_editorial_run_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _window(cycles, source):
    rows = []
    for label, confidence, publish, actual in cycles:
        if source == "decision_model":
            label = "publish" if publish >= .5 else "reject"
            confidence = max(publish, 1 - publish)
        rows.append(Decision(label, confidence, actual))
    return rows


def _rule(rows, threshold=.8):
    return next(row for row in routing(rows)["threshold_rule"] if row["threshold"] == threshold)


def test_the_rule_at_080_approves_everything_before_and_74_after():
    before = _rule(_window(RECORDING["cycles"][:100], "decision_model"))
    after = _rule(_window(RECORDING["cycles"][-100:], "cyclotron"))
    assert (before["approved"], before["approved_right"], before["whole_system_right"]) == (100, 52, 52)
    assert round(100 * before["promised"]) == 96
    assert (after["approved"], after["approved_right"], after["to_reviewers"], after["whole_system_right"]) == (74, 63, 26, 89)
    assert round(100 * after["promised"]) == round(100 * after["got"]) == 85


def test_the_least_confident_fifteen_percent_holds_few_of_the_last_windows_mistakes():
    capture = next(row for row in routing(_window(RECORDING["cycles"][-100:], "cyclotron"))["least_confident"]
                   if row["percent"] == 15)
    assert capture["mistakes"] == 18
    assert capture["share_of_mistakes"] == pytest.approx(.248, abs=.001)


def test_the_400_cycle_run_cost_under_a_dollar_at_list_prices():
    fields = RECORDING["event_fields"]
    events = []
    for row in RECORDING["events"]:
        event = dict(zip(fields[row[0]], row))
        if "prompt_tokens" in event:
            event["usage"] = {"prompt_tokens": event.pop("prompt_tokens"), "completion_tokens": event.pop("completion_tokens"),
                              "prompt_tokens_details": {"cached_tokens": event.pop("cached_tokens")}}
        events.append(event)
    prices = _exporter().PRICES["gpt-4.1-mini"]
    usage = usage_by_window(events, {"decision_model": prices, "optimizer": prices})
    total = usage["total"]
    assert {key: total["decision_model"][key] for key in ("requests", "input_tokens", "output_tokens")} == {
        "requests": 1152, "input_tokens": 998301, "output_tokens": 28792}
    assert {key: total["optimizer"][key] for key in ("requests", "input_tokens", "output_tokens")} == {
        "requests": 8, "input_tokens": 804912, "output_tokens": 5140}
    assert total["usd"] == pytest.approx(.7756, abs=1e-4)
    assert [window["decision_model"]["requests"] for window in usage["windows"]] == [157, 252, 643, 100]
    assert round(total["wall_clock_seconds"] / 60) == 56


def test_window_metrics_give_per_class_precision_recall_and_f1():
    metrics = _exporter()._confidence_metrics(
        [("publish", {"publish": .9, "reject": .1}, "publish"), ("publish", {"publish": .6, "reject": .4}, "reject"),
         ("reject", {"publish": .2, "reject": .8}, "reject")], ("publish", "reject"))
    assert metrics["accuracy"] == pytest.approx(2 / 3)
    assert metrics["per_class"]["publish"] == {"precision": .5, "recall": 1.0, "f1": pytest.approx(2 / 3), "predicted": 2, "actual": 1}
    assert metrics["mean_confidence"] == pytest.approx((.9 + .6 + .8) / 3)
