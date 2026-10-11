"""The replicate harness, driven by a fake study script (no live calls, no keys)."""
import importlib.util
import json
import sqlite3
import statistics
import sys
from pathlib import Path

import pytest

FAKE = r'''
import argparse, json, sqlite3, sys
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--output", type=Path, required=True)
p.add_argument("--seed", required=True)
p.add_argument("--fake-config", type=Path, required=True)
p.add_argument("--max-decision-calls", type=int, default=6000)
p.add_argument("--max-optimizer-calls", type=int, default=60)
p.add_argument("--confirm-live", action="store_true")
a = p.parse_args()
cfg = json.loads(a.fake_config.read_text())
c = cfg.get(a.seed, cfg.get("default", {}))
out = a.output
out.mkdir(parents=True, exist_ok=False)
cycles_n = c.get("cycles", 280)
(out / "protocol.json").write_text(json.dumps({"operational_items": c.get("expected", cycles_n)}))
mode = out / "all"; mode.mkdir()
w = 70
cyc, events = [], []
for i in range(cycles_n):
    k, pos = divmod(i, w)[0], i % w
    human = "reject" if pos % 5 < 3 else "publish"
    other = "publish" if human == "reject" else "reject"
    accs = c.get("cyclotron", [50, 80, 90, 90])
    dacc = c.get("decision", [50, 50, 50, 50])
    ok = pos < round(accs[min(k, len(accs) - 1)] * w / 100)
    dok = pos < round(dacc[min(k, len(dacc) - 1)] * w / 100)
    cyc.append({"cycle_number": i + 1, "human_label": human, "predicted_label": human if ok else other})
    events.append({"kind": "cycle-started", "cycle_number": i + 1})
    events.append({"kind": "prediction", "cycle_number": i + 1, "label": human if ok else other,
                   "decision_model_label": human if dok else other,
                   "fitted_head": i + 1 >= c.get("head_from", 10**9)})
    for act in c.get("activations", []):
        if act == i + 1:
            events.append({"kind": "classifier-activated", "cycle_number": act})
if not c.get("no_results"):
    (mode / "results.json").write_text(json.dumps({"cycles": cyc}))
con = sqlite3.connect(mode / "runtime.sqlite3")
con.execute("create table runtime_events (id integer primary key, payload text not null)")
for e in events:
    con.execute("insert into runtime_events (payload) values (?)", (json.dumps(e),))
con.commit(); con.close()
(out / "global_budget_ledger.json").write_text(json.dumps({"consumed": {
    "decision_requests": c.get("jev", 100), "optimizer_requests": c.get("opt", 5)}}))
sys.exit(c.get("exit", 0))
'''


def _mod():
    path = Path(__file__).parents[1] / "scripts/replicate_study.py"
    spec = importlib.util.spec_from_file_location("replicate_study", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["replicate_study"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rig(tmp_path):
    fake = tmp_path / "fake_study.py"
    fake.write_text(FAKE)
    config = tmp_path / "config.json"

    def setup(cfg, base_extra=""):
        config.write_text(json.dumps(cfg))
        base = tmp_path / "base.txt"
        base.write_text(f"# comment\n--fake-config {config}\n--confirm-live\n{base_extra}\n")
        return base

    def run(base, *extra, out="out"):
        mod = _mod()
        code = mod.main(["--base-args-file", str(base), "--study-script", str(fake),
                         "--out-dir", str(tmp_path / out), "--poll-seconds", "0.01", *extra])
        return code, tmp_path / out
    return setup, run, None


def test_base_args_text_and_json_and_forbidden_flags(tmp_path):
    mod = _mod()
    f = tmp_path / "a.txt"
    f.write_text("--model 'gpt 6'  # note\n--confirm-live\n")
    assert mod.load_base_args(f) == ["--model", "gpt 6", "--confirm-live"]
    j = tmp_path / "a.json"
    j.write_text(json.dumps(["--x", "1"]))
    assert mod.load_base_args(j) == ["--x", "1"]
    with pytest.raises(ValueError):
        mod.check_base_args(["--seed=abc"])
    with pytest.raises(ValueError):
        mod.check_base_args(["--output", "x"])


def test_default_and_explicit_seeds():
    mod = _mod()
    assert mod.replicate_seeds(2, None, "base") == ["base:rep-1", "base:rep-2"]
    assert mod.replicate_seeds(9, ["a", "b", "c"], "base") == ["a", "b", "c"]


def test_dry_run_prints_exact_commands_and_runs_nothing(rig, capsys, tmp_path):
    setup, run, _ = rig
    base = setup({})
    code, out = run(base, "--replicates", "2", "--base-seed", "S", "--dry-run")
    lines = capsys.readouterr().out.strip().splitlines()
    assert code == 0 and len(lines) == 2
    assert "--seed S:rep-1" in lines[0] and f"{out}/rep-1" in lines[0] and "--confirm-live" in lines[0]
    assert not out.exists()


def test_summary_math_against_hand_computation(rig):
    setup, run, _ = rig
    cfg = {"default": {"cyclotron": [40, 80, 90, 100], "decision": [50, 60, 70, 80],
                       "activations": [90, 150], "head_from": 211}}
    code, out = run(setup(cfg), "--replicates", "1", "--parallel", "1")
    assert code == 0
    r = json.loads((out / "replicates-summary.json").read_text())["replicates"]["rep-1"]
    assert r["status"] == "complete" and r["cycles"] == 280 and r["windows"] == 4
    assert r["cyclotron_by_window"] == pytest.approx([40, 80, 90, 100])
    assert r["decision_model_by_window"] == pytest.approx([50, 60, 70, 80])
    assert r["majority_baseline_by_window"] == pytest.approx([60, 60, 60, 60])
    assert r["first_activation_cycle"] == 90 and r["rubric_activations"] == 2
    assert r["fitted_head_used"] is True and r["fitted_head_share"] == pytest.approx(70 / 280)
    assert r["post_warmup_accuracy"] == pytest.approx((80 + 90 + 100) / 3)
    assert r["final_window_accuracy"] == 100 and r["learning_curve_area"] == pytest.approx(77.5)
    assert r["decision_requests"] == 100 and r["optimizer_requests"] == 5
    assert (out / "replicates-summary.md").read_text().startswith("# Replicate summary")


def test_windows_cover_only_whole_windows(rig):
    setup, run, _ = rig
    code, out = run(setup({"default": {"cycles": 200, "expected": 200}}), "--replicates", "1")
    r = json.loads((out / "replicates-summary.json").read_text())["replicates"]["rep-1"]
    assert r["windows"] == 2 and len(r["cyclotron_by_window"]) == 2


def test_aggregate_median_bootstrap_and_reliability():
    mod = _mod()
    reps = {f"rep-{i}": {"status": "complete", "final_window_accuracy": f, "post_warmup_accuracy": q,
                         "majority_baseline_by_window": [60.0]}
            for i, (f, q) in enumerate([(95, 90), (81, 85), (60, 70), (90, 88)], 1)}
    reps["rep-5"] = {"status": "incomplete"}
    agg = mod.aggregate(reps)
    fw = agg["final_window_accuracy"]
    assert fw["median"] == statistics.median([95, 81, 60, 90]) == 85.5
    assert (fw["min"], fw["max"], fw["n"]) == (60, 95, 4)
    assert fw["bootstrap_90_median"] == mod.bootstrap_median_interval([95, 81, 60, 90])
    assert 60 <= fw["bootstrap_90_median"][0] <= fw["bootstrap_90_median"][1] <= 95
    assert agg["reliability"]["share_final_beats_baseline_by_5"] == 0.75
    assert agg["reliability"]["min_post_warmup_accuracy"] == 70
    assert agg["incomplete"] == ["rep-5"] and agg["complete"] == 4 and agg["total"] == 5


def test_bootstrap_is_deterministic_and_degenerate_cases():
    mod = _mod()
    v = [1, 5, 9, 13, 20]
    assert mod.bootstrap_median_interval(v) == mod.bootstrap_median_interval(v)
    assert mod.bootstrap_median_interval([7, 7, 7]) == [7, 7]
    assert mod.bootstrap_median_interval([]) is None


def test_seeds_reach_the_study_and_replicates_run_in_parallel(rig):
    setup, run, _ = rig
    cfg = {"s1": {"cyclotron": [50, 80, 90, 95]}, "s2": {"cyclotron": [50, 80, 90, 80]}, "s3": {}}
    code, out = run(setup(cfg), "--seeds", "s1,s2,s3", "--parallel", "3")
    s = json.loads((out / "replicates-summary.json").read_text())
    assert code == 0 and s["seeds"] == ["s1", "s2", "s3"]
    assert s["replicates"]["rep-1"]["final_window_accuracy"] == pytest.approx(100 * 66 / 70)
    assert s["replicates"]["rep-2"]["final_window_accuracy"] == pytest.approx(80)
    assert s["aggregate"]["final_window_accuracy"]["max"] == pytest.approx(100 * 66 / 70)


def test_global_cap_guard_stops_launching(rig):
    setup, run, _ = rig
    # per-run optimizer cap 10, global 25: at most two replicates may be launched (3x10 > 25)
    base = setup({"default": {"opt": 10}}, "--max-optimizer-calls 10")
    code, out = run(base, "--replicates", "4", "--parallel", "1", "--max-total-optimizer-calls", "25")
    s = json.loads((out / "replicates-summary.json").read_text())
    assert [s["replicates"][f"rep-{i}"]["status"] for i in (1, 2, 3, 4)] == \
        ["complete", "complete", "incomplete", "incomplete"]
    assert any("global cap guard" in n for n in s["launch_notes"])
    assert not (out / "rep-3").exists()


def test_guard_uses_consumed_calls_not_just_caps(rig):
    setup, run, _ = rig
    # per-run decision cap 100 but each run consumes 10: four sequential runs fit under 60 + margin
    base = setup({"default": {"jev": 10}}, "--max-decision-calls 100")
    code, out = run(base, "--replicates", "3", "--parallel", "1", "--max-total-decision-calls", "120")
    s = json.loads((out / "replicates-summary.json").read_text())
    assert all(s["replicates"][f"rep-{i}"]["status"] == "complete" for i in (1, 2, 3))


def test_guard_counts_running_replicates_at_their_caps():
    mod = _mod()
    g = mod.Guard(250, None, 100, 1)
    assert g.allows([], 1)[0]            # 2 x 100 <= 250
    assert not g.allows([], 2)[0]        # 3 x 100 > 250
    assert g.allows([{"decision_requests": 20}], 1)[0]
    assert not g.allows([{"decision_requests": 60}], 1)[0]


def test_nonzero_exit_stops_further_launches_and_returns_nonzero(rig):
    setup, run, _ = rig
    code, out = run(setup({"s1": {"exit": 3}, "default": {}}), "--seeds", "s1,s2,s3", "--parallel", "1")
    s = json.loads((out / "replicates-summary.json").read_text())
    assert code == 1
    assert s["replicates"]["rep-1"]["exit"] == 3
    assert s["replicates"]["rep-2"]["exit"] == "not launched"
    assert any("exited 3" in n for n in s["launch_notes"])


def test_partial_replicate_is_incomplete_and_excluded(rig):
    setup, run, _ = rig
    cfg = {"s1": {}, "s2": {"cycles": 140, "expected": 280}, "s3": {"no_results": True}}
    code, out = run(setup(cfg), "--seeds", "s1,s2,s3", "--parallel", "1")
    s = json.loads((out / "replicates-summary.json").read_text())
    assert s["replicates"]["rep-2"]["status"] == "incomplete" and "140 of 280" in s["replicates"]["rep-2"]["reason"]
    assert s["replicates"]["rep-3"]["status"] == "incomplete"
    assert s["aggregate"]["complete"] == 1 and s["aggregate"]["total"] == 3


def test_resume_skips_complete_and_does_not_relaunch_partial(rig):
    setup, run, _ = rig
    cfg = {"s1": {}, "s2": {"cycles": 140, "expected": 280}}
    base = setup(cfg)
    run(base, "--seeds", "s1,s2")
    marker = Path(run(base, "--seeds", "s1,s2", "--resume")[1]) / "rep-1" / "protocol.json"
    before = marker.stat().st_mtime_ns
    code, out = run(base, "--seeds", "s1,s2,s3", "--resume")
    s = json.loads((out / "replicates-summary.json").read_text())
    assert marker.stat().st_mtime_ns == before
    assert s["replicates"]["rep-1"]["exit"].startswith("skipped")
    assert s["replicates"]["rep-2"]["status"] == "incomplete"
    assert s["replicates"]["rep-3"]["status"] == "complete"
    assert any("rep-2" in n and "not relaunched" in n for n in s["launch_notes"])


def test_existing_output_without_resume_refuses(rig):
    setup, run, _ = rig
    base = setup({})
    run(base, "--replicates", "1")
    with pytest.raises(SystemExit):
        run(base, "--replicates", "1")


def test_environment_is_inherited_not_written(rig, monkeypatch):
    setup, run, _ = rig
    monkeypatch.setenv("FAKE_SECRET_KEY", "sk-should-never-appear")
    code, out = run(setup({}), "--replicates", "1")
    for f in out.rglob("*"):
        if f.is_file() and f.suffix != ".sqlite3":
            assert "sk-should-never-appear" not in f.read_text()


def write_partial_run(root, reviewed_only=True, protocol_corpus=None):
    """A finished 140-story run, window 70, where stories 1..140 have corpus labels, the model is right on the
    first 35 of each window, the decision model on the first 14, and only some stories have a human label."""
    rep = root / "rep"
    mode = rep / "reject_half"
    mode.mkdir(parents=True)
    corpus = [{"id": f"s{i}", "simulated_label": "reject" if i % 10 < 8 else "publish"} for i in range(140)]
    (root / "corpus.jsonl").write_text("".join(json.dumps(r) + "\n" for r in corpus))
    protocol = {"operational_items": 140}
    if protocol_corpus:
        protocol["operational_corpus"] = str(protocol_corpus)
    (rep / "protocol.json").write_text(json.dumps(protocol))
    (rep / "global_budget_ledger.json").write_text(json.dumps({"consumed": {"decision_requests": 1, "optimizer_requests": 1}}))
    cycles, events = [], []
    for i, row in enumerate(corpus):
        truth = row["simulated_label"]
        other = "publish" if truth == "reject" else "reject"
        pos = i % 70
        reviewed = i % 3 == 0 and truth == "reject"  # reviewed stories are all "reject"
        cycles.append({"cycle_number": i + 1, "item_id": row["id"], "predicted_label": truth if pos < 35 else other,
                       "human_label": truth if (reviewed or not reviewed_only) else None})
        events.append({"kind": "prediction", "cycle_number": i + 1,
                       "decision_model_label": truth if pos < 14 else other})
    (mode / "results.json").write_text(json.dumps({"cycles": cycles}))
    con = sqlite3.connect(mode / "runtime.sqlite3")
    con.execute("create table runtime_events (id integer primary key, payload text not null)")
    con.executemany("insert into runtime_events (payload) values (?)", [(json.dumps(e),) for e in events])
    con.commit()
    con.close()
    return rep, root / "corpus.jsonl"


def test_partial_review_uses_corpus_labels_for_accuracy_and_baseline(tmp_path):
    mod = _mod()
    rep, corpus = write_partial_run(tmp_path)
    s = mod.summarise_replicate(rep, 70, corpus=str(corpus))
    assert s["cyclotron_by_window"] == pytest.approx([50, 50])
    assert s["decision_model_by_window"] == pytest.approx([20, 20])
    assert s["majority_baseline_by_window"] == pytest.approx([80, 80])
    assert s["labels"].startswith("corpus reference labels")
    # The corpus recorded in protocol.json wins over the base args, which may be absent.
    rep2, corpus2 = write_partial_run(tmp_path / "two", protocol_corpus=tmp_path / "corpus.jsonl")
    s2 = mod.summarise_replicate(rep2, 70)
    assert s2["majority_baseline_by_window"] == pytest.approx([80, 80]) and str(tmp_path / "corpus.jsonl") in s2["labels"]


def test_without_a_corpus_recorded_labels_are_used_and_the_summary_says_so(tmp_path):
    mod = _mod()
    rep, _ = write_partial_run(tmp_path)
    s = mod.summarise_replicate(rep, 70)
    assert s["labels"] == "recorded (reviewed stories only)"
    assert s["majority_baseline_by_window"] != pytest.approx([80, 80])  # the old, unreliable number
    assert "Labels: recorded (reviewed stories only)." in mod.render_markdown(
        {"window_size": 70, "replicates": {"rep-1": {"status": "complete", **s}}, "aggregate": mod.aggregate({"rep-1": s})})
    assert mod.corpus_arg(["--a", "1", "--operational-corpus", "c.jsonl"]) == "c.jsonl"
    assert mod.corpus_arg(["--operational-corpus=d.jsonl"]) == "d.jsonl"
    assert mod.corpus_arg(["--a"]) is None


def test_full_review_is_identical_with_or_without_the_corpus(tmp_path):
    mod = _mod()
    rep, corpus = write_partial_run(tmp_path, reviewed_only=False)
    without = mod.summarise_replicate(rep, 70)
    with_corpus = mod.summarise_replicate(rep, 70, corpus=str(corpus))
    assert without["labels"] == "recorded" and with_corpus["labels"].startswith("corpus")
    for key in ("cyclotron_by_window", "decision_model_by_window", "majority_baseline_by_window",
                "post_warmup_accuracy", "final_window_accuracy", "learning_curve_area"):
        assert with_corpus[key] == without[key]
    assert without["majority_baseline_by_window"] == pytest.approx([80, 80])
