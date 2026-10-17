"""The workspace application composes its reviewer adapter outside WebWorker."""
import ast
from pathlib import Path

import pytest

from .application_runtime import open_live_review_session
from .flywheel_test import FakeModel, agent
from .reviewer_store import Article


def test_a_live_review_session_is_composed_without_a_web_worker(tmp_path):
    article = Article("paper", "Title", "Abstract", "2026-10-07", ("cs.AI",))
    session = open_live_review_session(
        tmp_path / "run",
        {"seed": "fixture", "max_requests": 10,
         "selection_policy": {"primary": "f1", "positive_class": "include"}},
        (article,),
        model_factory=lambda _config: (FakeModel(), agent([])),
        event_sink=lambda _event: None,
    )
    assert session.reviewer.store.article("paper") == article
    assert session.reviewer.core.max_requests == 10
    session.close()


def test_closing_a_live_review_session_closes_the_core_and_review_store(tmp_path):
    session = open_live_review_session(
        tmp_path / "run", {"seed": "fixture", "max_requests": 10,
                             "selection_policy": {"primary": "f1", "positive_class": "include"}}, (),
        model_factory=lambda _config: (FakeModel(), agent([])), event_sink=lambda _event: None,
    )
    session.close()
    assert session.closed


def test_an_article_runtime_normalizes_web_run_configuration_without_a_worker(tmp_path):
    from .application_runtime import ArticleReviewRuntime
    runtime = ArticleReviewRuntime(tmp_path, sink_factory=lambda _run_id: lambda _event: None,
                                   model_factory=lambda _config: (FakeModel(), agent([])))
    config = runtime.normalize_run_config({}, ())
    assert config["max_requests"] == 500
    assert config["selection_policy"]["primary"] == "f1"
    assert config["selection_policy"]["positive_class"] == "include"
    assert config["max_optimizer_calls"] == 1000
    assert config["optimizer_transport"] == "openai"
    assert config["evaluation_protocol"] == "protected-feedback-v1"


def test_a_web_worker_can_use_an_injected_runtime_without_importing_article_types(tmp_path):
    from .web_store import WebStore
    from .web_worker import WebWorker

    class Runtime:
        def __init__(self):
            self.calls = []

        def normalize_run_config(self, config, items):
            self.calls.append((dict(config), tuple(items)))
            return {"runtime": "fake"}

        def open_session(self, run_id, config, items, current):  # pragma: no cover - no command is run here
            raise AssertionError("session should not open while creating a run")

    runtime = Runtime()
    worker = WebWorker(WebStore(tmp_path / "web.sqlite"), tmp_path / "runs", runtime=runtime, allow_live=True)
    run = worker.create_run("A generic run", {"caller": "value"})
    assert runtime.calls == [({"caller": "value"}, ())]
    assert run["config"] == {"runtime": "fake"}


def test_the_web_worker_defaults_to_the_article_runtime_and_does_not_import_reviewer_internals(tmp_path):
    from .application_runtime import ArticleReviewRuntime
    from .web_store import WebStore
    from .web_worker import WebWorker

    source = Path(__file__).with_name("web_worker.py").read_text()
    imports = [node.module for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.ImportFrom) and node.module]
    assert "web_store" not in imports
    assert not {"reviewer_store", "reviewer_flywheel", "reviewer_core", "flywheel"} & set(imports)
    assert isinstance(WebWorker(WebStore(tmp_path / "web.sqlite"), tmp_path / "runs").runtime, ArticleReviewRuntime)


def test_an_application_session_aborts_its_incomplete_cycle_without_exposing_core_cleanup(tmp_path):
    session = open_live_review_session(
        tmp_path / "run", {"seed": "fixture", "max_requests": 10,
                             "selection_policy": {"primary": "f1", "positive_class": "include"}}, (),
        model_factory=lambda _config: (FakeModel(), agent([])), event_sink=lambda _event: None,
    )
    session.abort(RuntimeError("interrupted"))
    assert session.current_cycle is None
    session.close()


def test_an_article_runtime_prepares_an_item_without_a_web_worker(tmp_path):
    from .application_runtime import ArticleReviewRuntime
    runtime = ArticleReviewRuntime(tmp_path, sink_factory=lambda _run_id: lambda _event: None,
                                   model_factory=lambda _config: (FakeModel(), agent([])))
    article = {"id": "paper", "title": "Title", "abstract": "Abstract", "submitted_at": "2026-10-07",
               "categories": ("cs.AI",)}
    config = runtime.normalize_run_config({}, (article,))
    session = runtime.open_session("run", config, (article,), None)
    command = runtime.execute(session, "prepare", {}, None, config)
    assert command.result["prediction"]["label"] == "exclude"
    assert command.updates[0].item_id == "paper"
    assert command.updates[0].prediction["presentation_id"]
    session.close()


def test_article_runtime_metrics_use_recorded_probability_vectors_and_calibration_provenance():
    from types import SimpleNamespace
    from .application_runtime import ArticleReviewRuntime
    events = [{"kind": "prediction", "event_id": 1, "target_id": "paper", "label": "include", "version": "head-v1",
               "probabilities": {"include": .6, "exclude": .4}, "confidence": .99, "fitted_head": True,
               "uncalibrated_probabilities": {"include": .9, "exclude": .1}, "calibration_temperature": 2.,
               "calibration_provenance": {"fit_on": "oof", "training_ids": ["trusted"]}},
              {"kind": "human-feedback", "event_id": 2, "action": "submitted",
               "feedback": {"item_id": "paper", "final_answer_value": "include"}}]
    emitted = []
    reviewer = SimpleNamespace(core=SimpleNamespace(
        initial=SimpleNamespace(task=SimpleNamespace(labels=("include", "exclude"))),
        events_of=lambda kinds: tuple(events), _emit=emitted.append))
    ArticleReviewRuntime._metrics(reviewer)
    curve = emitted[0]["metrics"]["calibration"]
    assert curve["ece"] == pytest.approx(.4)
    assert curve["samples"][0]["prediction_event_id"] == 1
    assert curve["samples"][0]["calibration_provenance"] == {"fit_on": "oof", "training_count": 1}
    assert curve["matched_head_comparison"]["raw"]["ece"] == pytest.approx(.1)


def test_protected_feedback_cannot_fire_article_runtime_stage_cadences():
    from types import SimpleNamespace
    from .application_runtime import ArticleReviewRuntime
    checks, improvements = [], []
    events = [{"kind": "human-feedback", "action": "submitted", "assignment": "train"},
              {"kind": "human-feedback", "action": "submitted", "assignment": "final_audit"}]
    reviewer = SimpleNamespace(
        core=SimpleNamespace(events_of=lambda _: events),
        feedback_trigger=lambda _: False,
        current_cycle=SimpleNamespace(check_trigger=lambda stage, **check: checks.append((stage, check))),
        improve=lambda **kwargs: improvements.append(kwargs))
    ArticleReviewRuntime._optimize(reviewer, {"optimize_every": 1})
    assert improvements == []
    assert all(not check["due"] for _, check in checks)
    assert all(check["details"]["feedback_count"] == 1 for _, check in checks)
