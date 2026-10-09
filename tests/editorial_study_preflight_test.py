"""The editorial study records its protocol before any live call."""
import hashlib
import importlib.util
import json
from pathlib import Path


def _study():
    path = Path(__file__).parents[1] / "scripts/run_editorial_prequential_study.py"
    spec = importlib.util.spec_from_file_location("editorial_study", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _corpus(path, prefix, count):
    lines = [json.dumps({"id": f"{prefix}-{i}", "text": f"story {i}",
                         "simulated_label": "publish" if i % 2 else "reject"}) for i in range(count)]
    path.write_text("\n".join(lines) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_dry_run_records_the_rubric_trigger_and_spends_nothing(tmp_path):
    operational, bootstrap = tmp_path / "operational.jsonl", tmp_path / "bootstrap.jsonl"
    operational_sha, bootstrap_sha = _corpus(operational, "op", 400), _corpus(bootstrap, "boot", 12)
    output = tmp_path / "study"
    _study().main(["--operational-corpus", str(operational), "--operational-sha256", operational_sha,
                   "--bootstrap-corpus", str(bootstrap), "--bootstrap-sha256", bootstrap_sha,
                   "--output", str(output), "--rubric-trigger-basis", "revealed_feedback_count",
                   "--rubric-changes-every", "25", "--max-rubric-optimizations", "3"])
    protocol = json.loads((output / "protocol.json").read_text())
    assert protocol["rubric_trigger"] == {"basis": "revealed_feedback_count", "every": 25, "max_attempts": 3}
    ledger = json.loads((output / "global_budget_ledger.json").read_text())
    assert ledger["consumed"] == {"decision_requests": 0, "optimizer_requests": 0}


def test_a_longer_corpus_from_an_empty_rubric_is_declared_in_the_protocol(tmp_path):
    operational, bootstrap = tmp_path / "operational.jsonl", tmp_path / "bootstrap.jsonl"
    operational_sha, bootstrap_sha = _corpus(operational, "op", 1200), _corpus(bootstrap, "boot", 12)
    output = tmp_path / "study"
    _study().main(["--operational-corpus", str(operational), "--operational-sha256", operational_sha,
                   "--bootstrap-corpus", str(bootstrap), "--bootstrap-sha256", bootstrap_sha,
                   "--output", str(output), "--operational-items", "1200", "--baseline-rubric", "none",
                   "--modes", "all"])
    protocol = json.loads((output / "protocol.json").read_text())
    assert protocol["operational_items"] == 1200 and protocol["modes"] == ["all"]
    assert protocol["baseline_rubric"] == "" and protocol["baseline_provenance"].startswith("none")
    assert protocol["decision_model"] == protocol["optimizer_model"] == "gpt-4.1-mini"


def test_the_declared_length_must_match_the_corpus(tmp_path):
    import pytest
    operational, bootstrap = tmp_path / "operational.jsonl", tmp_path / "bootstrap.jsonl"
    operational_sha, bootstrap_sha = _corpus(operational, "op", 400), _corpus(bootstrap, "boot", 12)
    with pytest.raises(ValueError, match="exactly 1200"):
        _study().main(["--operational-corpus", str(operational), "--operational-sha256", operational_sha,
                       "--bootstrap-corpus", str(bootstrap), "--bootstrap-sha256", bootstrap_sha,
                       "--output", str(tmp_path / "study"), "--operational-items", "1200"])


def test_a_rubric_gate_configuration_is_declared_and_spreads_development_through_the_stream(tmp_path):
    operational, bootstrap = tmp_path / "operational.jsonl", tmp_path / "bootstrap.jsonl"
    operational_sha, bootstrap_sha = _corpus(operational, "op", 400), _corpus(bootstrap, "boot", 12)
    output = tmp_path / "study"
    _study().main(["--operational-corpus", str(operational), "--operational-sha256", operational_sha,
                   "--bootstrap-corpus", str(bootstrap), "--bootstrap-sha256", bootstrap_sha,
                   "--output", str(output), "--development-rate", "0.25", "--provisional-allowance", "0.05"])
    gate = json.loads((output / "protocol.json").read_text())["rubric_gate"]
    assert gate["evaluation_policy"]["initial_recency_allowance"] == 0.05
    assert gate["evaluation_policy"]["recency_decay_per_class"] == 20
    assert 70 <= gate["development_items"] <= 130
    study = _study()
    rows = study.rows(operational)
    plan = study.build_plan(study.DecisionTask("editorial", ("publish", "reject"), "q"), rows, "seed", 0.25)
    first_200 = {row.item.id for row in rows[:200]}
    fixed = study.build_plan(study.DecisionTask("editorial", ("publish", "reject"), "q"), rows, "seed")
    early = lambda p: min(sum(row.label == label and row.item.id in first_200 for row in p.development) for label in ("publish", "reject"))
    # A fixed 20 per class from the whole corpus has only about half its development labels by mid-run.
    assert early(fixed) <= 12 < 15 <= early(plan)


def test_seeded_answers_copy_only_completed_exact_requests(tmp_path):
    import sqlite3
    source = tmp_path / "source.sqlite3"
    db = sqlite3.connect(source)
    db.execute("CREATE TABLE runtime_answers (key TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT)")
    db.executemany("INSERT INTO runtime_answers VALUES (?,?,?)", [("a", "complete", "{}"), ("b", "pending", None), ("c", "failed", None)])
    db.commit(); db.close()
    target = tmp_path / "run" / "runtime.sqlite3"
    _study().seed_answers(target, source)
    assert sqlite3.connect(target).execute("SELECT key,status FROM runtime_answers").fetchall() == [("a", "complete")]


def test_the_decision_model_and_provider_are_declared_separately_from_the_optimizer(tmp_path):
    operational, bootstrap = tmp_path / "operational.jsonl", tmp_path / "bootstrap.jsonl"
    operational_sha, bootstrap_sha = _corpus(operational, "op", 400), _corpus(bootstrap, "boot", 12)
    output = tmp_path / "study"
    _study().main(["--operational-corpus", str(operational), "--operational-sha256", operational_sha,
                   "--bootstrap-corpus", str(bootstrap), "--bootstrap-sha256", bootstrap_sha,
                   "--output", str(output), "--decision-provider", "jev", "--decision-model", "jev-1.13.0"])
    protocol = json.loads((output / "protocol.json").read_text())
    assert (protocol["decision_provider"], protocol["decision_model"], protocol["optimizer_model"]) == ("jev", "jev-1.13.0", "gpt-4.1-mini")
