import asyncio
import json

from .demo import run_demo


def test_the_offline_walkthrough_repeats_artifact_and_reloaded_context_ids_without_source_pool_text(tmp_path):
    first = asyncio.run(run_demo(tmp_path / "first"))
    second = asyncio.run(run_demo(tmp_path / "second"))

    assert first["artifact_hash"] == second["artifact_hash"]
    assert first["reloaded_context_ids"] == second["reloaded_context_ids"]
    assert first["prediction"] == "approve"
    assert first["winner"] == "lexical-size-1"
    assert first["winner"] == first["provisional_best"]
    assert first["frozen_winner"] is True
    assert first["model_calls_attempted"] == 10
    assert first["synthetic"] is True
    assert first["data_origin"] == "synthetic-scripted-fixture"
    assert first["trial_objectives"]["lexical-size-1"] == 1.0
    assert first["trial_objectives"]["lexical-size-2"] < 1.0
    assert json.loads((tmp_path / "first" / "summary.json").read_text()) == first
    artifact = (tmp_path / "first" / "artifact.json").read_text()
    assert "approval unused archive" not in artifact
    assert "crisp approval signal" not in artifact


def test_the_offline_walkthrough_writes_no_credentials_or_target_source_text(tmp_path):
    result = asyncio.run(run_demo(tmp_path))
    saved = "\n".join(path.read_text() for path in tmp_path.iterdir())

    assert result["target_id"] == "unlabeled-target"
    assert "crisp unlabeled request" not in saved
    assert "api_key" not in saved.lower()
    assert "secret" not in saved.lower()
