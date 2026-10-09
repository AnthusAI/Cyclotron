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
