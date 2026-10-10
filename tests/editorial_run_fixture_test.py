"""The 400-cycle editorial recording's routing metrics and model cost, pinned."""
import importlib.util
import json
import sqlite3
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


def _recording(root, count):
    """A minimal recorded run of ``count`` cycles: corpus, manifest, results and runtime events."""
    labels = ("publish", "reject")
    corpus = [{"id": f"item-{n}", "title": f"Story {n}", "source_url": f"https://example.org/{n}", "license": "CC BY 2.5",
               "license_url": "https://example.org/license", "simulated_label": labels[n % 2]} for n in range(1, count + 1)]
    (root / "corpus.jsonl").write_text("".join(json.dumps(record) + "\n" for record in corpus))
    (root / "manifest.json").write_text(json.dumps({
        "actual_count": count, "label_disclosure": "simulated_semantic_editorial_labels", "dataset": "wikinews",
        "dataset_revision": "r1", "license": "CC BY 2.5", "license_url": "https://example.org/license", "item_set_sha256": "0" * 64}))
    run = root / "all"
    run.mkdir()
    metrics = {"count": 1, "accuracy": 1.0, "brier": 0.0, "calibration": {"ece": 0.0}, "per_class": {}, "confusion_matrix": {}}
    cycles, events = [], []
    for n, record in enumerate(corpus, start=1):
        actual = record["simulated_label"]
        predicted = labels[actual == "publish"] if n <= count // 2 and n % 4 == 0 else actual
        cycles.append({"cycle_number": n, "item_id": record["id"], "predicted_label": predicted, "human_label": actual,
                       "feedback_selected": True, "feedback_propensity": 1.0, "role": "training", "metrics": metrics,
                       "outcomes": [], "requests": 1})
        minute = f"2026-10-09T{10 + n // 60:02d}:{n % 60:02d}"
        events += [
            {"kind": "cycle-started", "cycle_number": n, "created_at": f"{minute}:00+00:00"},
            {"kind": "prediction", "cycle_number": n, "cycle_item_id": record["id"], "label": predicted, "confidence": .8,
             "probabilities": {predicted: .8, labels[predicted == "publish"]: .2}},
            {"kind": "decision-response", "cycle_number": n, "usage": {"prompt_tokens": 1000, "completion_tokens": 10}},
            {"kind": "human-feedback", "action": "submitted", "cycle_number": n,
             "feedback": {"item_id": record["id"], "final_answer_value": actual, "edit_comment_value": "why"}},
            {"kind": "cycle-completed", "cycle_number": n, "created_at": f"{minute}:30+00:00"},
        ]
    (run / "results.json").write_text(json.dumps({
        "cycles": cycles, "disclosure": {"operational_items": count}, "protocol": {"task": "t" * 64},
        "feedback_policy": {"mode": "all"}}))
    connection = sqlite3.connect(run / "runtime.sqlite3")
    connection.execute("CREATE TABLE runtime_events (id INTEGER PRIMARY KEY, payload TEXT)")
    connection.executemany("INSERT INTO runtime_events (payload) VALUES (?)", [(json.dumps(event),) for event in events])
    connection.commit()
    connection.close()
    return ["--corpus", str(root / "corpus.jsonl"), "--corpus-manifest", str(root / "manifest.json"), "--scenario", f"all={run}"]


def test_a_cycle_cut_exports_only_the_first_cycles_with_their_own_usage_and_windows(tmp_path):
    arguments = _recording(tmp_path, 300)
    output = tmp_path / "cut.json"
    assert _exporter().main(arguments + ["--output", str(output), "--cycles", "200"]) == 0
    fixture = json.loads(output.read_text())
    scenario = fixture["scenarios"]["all"]
    assert fixture["cycle_cut"] == {"cycles": 200, "recorded_cycles": 300}
    assert [cycle["n"] for cycle in scenario["cycles"]] == list(range(1, 201))
    assert [(window["first_cycle"], window["last_cycle"]) for window in scenario["windows"]] == [(1, 100), (101, 200)]
    assert scenario["usage_total"]["decision_model"]["requests"] == 200
    assert scenario["usage_total"]["decision_model"]["input_tokens"] == 200_000
    assert scenario["usage_total"]["wall_clock_seconds"] == (200 // 60 * 60 + 200 % 60 - 1) * 60 + 30
    # The evaluator-only windows compare the cut's first and last 100: one in four is wrong through cycle 150.
    windows = scenario["evaluator_only_windows"]
    assert windows["first_100"]["accuracy"] == pytest.approx(.75)
    assert windows["last_100"]["accuracy"] == pytest.approx(.88)
    whole = tmp_path / "whole.json"
    _exporter().main(arguments + ["--output", str(whole)])
    assert "cycle_cut" not in json.loads(whole.read_text())
    assert json.loads(whole.read_text())["scenarios"]["all"]["evaluator_only_windows"]["last_100"]["accuracy"] == 1.0


@pytest.mark.parametrize("cycles", ["150", "400", "50"])
def test_a_cycle_cut_must_be_whole_windows_within_the_recording(tmp_path, cycles):
    with pytest.raises(SystemExit, match="--cycles"):
        _exporter().main(_recording(tmp_path, 300) + ["--output", str(tmp_path / "x.json"), "--cycles", cycles])


def test_a_cut_may_come_from_a_recording_that_stopped_early(tmp_path):
    arguments = _recording(tmp_path, 300)
    results = tmp_path / "all" / "results.json"
    stopped = json.loads(results.read_text())
    stopped["cycles"] = stopped["cycles"][:250]
    results.write_text(json.dumps(stopped))
    output = tmp_path / "cut.json"
    assert _exporter().main(arguments + ["--output", str(output), "--cycles", "200"]) == 0
    assert len(json.loads(output.read_text())["scenarios"]["all"]["cycles"]) == 200
    with pytest.raises(SystemExit, match="at least 300"):
        _exporter().main(arguments + ["--output", str(output), "--cycles", "300"])
    with pytest.raises(SystemExit, match="exactly 300"):
        _exporter().main(arguments + ["--output", str(output)])
