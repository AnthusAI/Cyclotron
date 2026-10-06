"""Recovery authorization cannot quietly expand a previously approved budget."""
import importlib.util
import json
from pathlib import Path

import pytest


def test_recovery_preflight_uses_remaining_budget_and_never_constructs_a_paid_client(tmp_path, monkeypatch):
    source = tmp_path / "original"
    source.mkdir()
    results = source / "results.json"
    results.write_text(json.dumps({"protocol": {"request_upper_bound": 373}, "jev_attempts": 353,
                                  "result": {"trials": [{"feature_id": "factor", "error_type": "ValueError"}]}}))
    original = results.read_bytes()
    spec = importlib.util.spec_from_file_location("recovery_script", Path(__file__).parents[1] / "scripts/recover_arxiv_feature_trial.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not instantiate a paid client")
    monkeypatch.setattr(module.JevAdapter, "from_environment", forbidden)
    output = tmp_path / "recovered"
    assert module.main(["--source", str(source), "--output", str(output), "--max-requests", "20"]) == 0
    with pytest.raises(SystemExit):
        module.main(["--source", str(source), "--output", str(output), "--max-requests", "21"])
    assert results.read_bytes() == original
    assert not output.exists()
