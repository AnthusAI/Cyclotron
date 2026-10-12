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


PINNED_DEFAULT_SHA256 = "ade6f56cbc9ca116c8fe4ebbfee06f8c97d72e1e5b05df205550b68715597f65"


def _relabeled(root, count, **overrides):
    """Turn ``_recording``'s manifest into the relabeled form, hashed by operational_sha256."""
    import hashlib
    manifest = json.loads((root / "manifest.json").read_text())
    manifest.pop("item_set_sha256", None)
    manifest.update({"corpus": "editorial-relabel-v3-1", "operational_sha256": hashlib.sha256((root / "corpus.jsonl").read_bytes()).hexdigest(),
                     "label_provenance": "Labels are simulated; two blind passes agreed.", **overrides})
    (root / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def _main(arguments, output, *extra):
    assert _exporter().main(arguments + ["--output", str(output), *extra]) == 0
    return json.loads(output.read_text())


def test_without_window_size_the_output_is_unchanged_from_the_100_cycle_exporter(tmp_path):
    import hashlib
    arguments = _recording(tmp_path, 200)
    output = tmp_path / "default.json"
    fixture = _main(arguments, output)
    assert "window_size" not in fixture and "label_provenance" not in fixture
    assert set(fixture["scenarios"]["all"]["evaluator_only_windows"]) == {"window_size", "label_source", "first_100", "last_100"}
    # Pinned from the exporter as it was before --window-size existed.
    assert hashlib.sha256(output.read_text().rstrip("\n").encode()).hexdigest() == PINNED_DEFAULT_SHA256
    explicit = _main(arguments, tmp_path / "explicit.json", "--window-size", "100")
    assert explicit.pop("window_size") == 100
    assert explicit == fixture


def test_window_size_sets_windows_routing_and_evaluator_only_blocks(tmp_path):
    arguments = _recording(tmp_path, 300)
    fixture = _main(arguments, tmp_path / "w.json", "--cycles", "280", "--window-size", "70")
    scenario = fixture["scenarios"]["all"]
    assert fixture["window_size"] == 70 and fixture["cycle_cut"] == {"cycles": 280, "recorded_cycles": 300}
    assert [(w["first_cycle"], w["last_cycle"]) for w in scenario["windows"]] == [(1, 70), (71, 140), (141, 210), (211, 280)]
    assert all(w["cyclotron"]["count"] == w["decision_model"]["count"] == 70 for w in scenario["windows"])
    assert all(w["routing"]["cyclotron"] for w in scenario["windows"])
    assert [w["model_calls"]["decision_model"] for w in scenario["windows"]] == [70] * 4
    blocks = scenario["evaluator_only_windows"]
    assert blocks["window_size"] == 70 and "first_100" not in blocks
    assert blocks["first_70"]["count"] == blocks["last_70"]["count"] == 70
    assert blocks["first_70"]["accuracy"] == pytest.approx(53 / 70)  # 17 of cycles 1-70 are multiples of four, and wrong
    assert blocks["last_70"]["accuracy"] == 1.0
    assert scenario["usage_total"]["decision_model"]["requests"] == 280


@pytest.mark.parametrize("cycles, size", [("280", "100"), ("100", "70"), ("0", "70"), ("350", "70")])
def test_cycles_must_be_a_multiple_of_the_window_size(tmp_path, cycles, size):
    with pytest.raises(SystemExit, match="--cycles"):
        _exporter().main(_recording(tmp_path, 300) + ["--output", str(tmp_path / "x.json"), "--cycles", cycles, "--window-size", size])


def test_window_size_needs_schema_2(tmp_path):
    with pytest.raises(SystemExit, match="--window-size"):
        _exporter().main(_recording(tmp_path, 300) + ["--output", str(tmp_path / "x.json"), "--schema-version", "1", "--window-size", "70"])


def test_a_relabeled_manifest_is_accepted_and_carries_provenance_and_the_operational_hash(tmp_path):
    arguments = _recording(tmp_path, 287)
    manifest = _relabeled(tmp_path, 287)
    fixture = _main(arguments, tmp_path / "v3.json", "--cycles", "280", "--window-size", "70")
    assert fixture["label_provenance"] == manifest["label_provenance"]
    assert fixture["attribution"]["corpus_sha256"] == manifest["operational_sha256"]
    assert fixture["attribution"]["dataset"] == "wikinews" and fixture["attribution"]["revision"] == "r1"
    assert fixture["cycle_cut"] == {"cycles": 280, "recorded_cycles": 287}


def test_a_relabeled_manifest_with_the_wrong_hash_count_or_fields_is_refused(tmp_path):
    arguments = _recording(tmp_path, 200)
    _relabeled(tmp_path, 200, operational_sha256="f" * 64)
    with pytest.raises(SystemExit, match="operational_sha256"):
        _exporter().main(arguments + ["--output", str(tmp_path / "x.json")])
    _relabeled(tmp_path, 200, actual_count=199)
    with pytest.raises(SystemExit, match="actual_count"):
        _exporter().main(arguments + ["--output", str(tmp_path / "x.json")])
    _relabeled(tmp_path, 200, license_url="")
    with pytest.raises(SystemExit, match="license_url"):
        _exporter().main(arguments + ["--output", str(tmp_path / "x.json")])


def test_the_old_manifest_form_keeps_its_checks_and_message(tmp_path):
    arguments = _recording(tmp_path, 200)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    (tmp_path / "manifest.json").write_text(json.dumps({**manifest, "label_disclosure": "other"}))
    with pytest.raises(SystemExit, match="not a reviewed simulated-label editorial corpus of the expected length"):
        _exporter().main(arguments + ["--output", str(tmp_path / "x.json")])
    # An old-form manifest is not hash-checked against the corpus file: its item_set_sha256 is a set hash.
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert _exporter().main(arguments + ["--output", str(tmp_path / "x.json")]) == 0
    with pytest.raises(SystemExit, match="not a reviewed simulated-label"):
        _exporter().main(arguments + ["--output", str(tmp_path / "x.json"), "--schema-version", "1"])


def test_luna_is_priced_per_request_with_the_above_272k_tier():
    luna = _exporter().PRICES["gpt-6-luna"]
    assert (luna["input_usd_per_mtok"], luna["output_usd_per_mtok"], luna["cached_input_usd_per_mtok"]) == (.10, .50, .01)
    assert (luna["above_input_tokens"], luna["above_input_usd_per_mtok"], luna["above_output_usd_per_mtok"]) == (272000, .20, .75)
    assert luna["source"] == "LiteLLM model price table (standard tier), read 2026-10-11"
    events = [{"kind": "optimizer-response", "cycle_number": 1, "usage": {"prompt_tokens": 100_000, "completion_tokens": 1000,
                                                                            "prompt_tokens_details": {"cached_tokens": 50_000}}},
              {"kind": "optimizer-response", "cycle_number": 1, "usage": {"prompt_tokens": 300_000, "completion_tokens": 2000}}]
    prices = {"decision_model": _exporter().PRICES["jev"], "optimizer": luna}
    total = usage_by_window(events, prices, window=1)["total"]["optimizer"]
    assert total["tier_requests"] == 1 and total["input_tokens"] == 400_000
    expected = (50_000 * .10 + 50_000 * .01 + 1000 * .50 + 300_000 * .20 + 2000 * .75) / 1e6
    assert total["usd"] == pytest.approx(expected, abs=1e-6)
    # Below the tier nothing changes: all requests at the standard rate.
    flat = usage_by_window(events[:1], prices, window=1)["total"]["optimizer"]
    assert flat["tier_requests"] == 0 and flat["usd"] == pytest.approx((50_000 * .10 + 50_000 * .01 + 1000 * .50) / 1e6, abs=1e-6)


def test_the_fixture_names_the_luna_optimizer_in_its_price_basis(tmp_path):
    fixture = _main(_recording(tmp_path, 100), tmp_path / "p.json", "--decision-model-price", "jev", "--optimizer-price", "gpt-6-luna")
    basis = fixture["attribution"]["price_basis"]
    assert basis["optimizer"]["model"] == "gpt-6-luna" and basis["optimizer"]["output_usd_per_mtok"] == .50
    assert basis["decision_model"]["model"] == "jev"
