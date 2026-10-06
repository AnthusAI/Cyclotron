"""Specs for the terminal article-review entry point."""
from __future__ import annotations

import json

import pytest

from .reviewer import _article_panel, _core_flywheel_status, load_articles_jsonl, load_flywheel_report
from .reviewer_predictor import ReviewerPrediction
from .reviewer_store import Article
from .events import FlywheelEvent
from .run_ledger import FlywheelRound, FlywheelStatus


def test_cli_exposes_provider_neutral_decision_selection(capsys):
    from .reviewer import main
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "--decisions-provider" in help_text
    assert "--decisions-model" in help_text
    assert "--jev-model" not in help_text


def test_historical_prediction_agreement_is_not_presented_as_current_model_accuracy():
    from .reviewer import _summary_table
    from .reviewer_store import PredictionMetrics
    table = _summary_table(dict(articles=10, include=2, exclude=3, skip=0, unreviewed=5, train_labels=4),
                           PredictionMetrics(5, 3, .6, 2, 4))
    assert "Historical prediction agreement" in table.columns[0]._cells
    assert "Model refreshes" not in table.columns[0]._cells


def test_live_status_shows_rubric_features_class_balance_and_measured_promotion():
    from .reviewer import _live_flywheel_status
    class Client:
        def status(self):
            return {"version": "a" * 64, "rubric": "Recent practical work", "tasks": ["practical"],
                    "example_ids": ["one", "two"], "fitted_head": True,
                    "features": ["decision/include", "practical/yes"], "training_count": 12,
                    "development_count": 4, "by_label": {"include": 5, "exclude": 7},
                    "requests": 20, "ceiling": 100, "latest": {"kind": "promoted", "reason": "lower Brier",
                    "candidate": {"accuracy": 1, "brier": .1}, "incumbent": {"accuracy": .5, "brier": .5}}}
    panel = _live_flywheel_status(Client())
    text = panel.renderable.plain
    assert "Recent practical work" in text
    assert "practical/yes" in text
    assert "5 include" in text
    assert "promoted" in text


def test_live_status_shows_balanced_scores_and_counts_for_arbitrary_classes():
    from .reviewer import _live_flywheel_status
    class Client:
        def status(self):
            return {"version": "a"*64, "rubric": "", "tasks": [], "example_ids": [], "fitted_head": True,
                    "features": ["decision/red"], "training_count": 20, "development_count": 10,
                    "by_label": {}, "requests": 0, "ceiling": 100,
                    "evaluation_weighting": "equal_class", "training_class_weighting": "natural",
                    "latest": {"kind": "promoted", "promotion_metric": "balanced_brier", "candidate": {
                        "accuracy": .8, "brier": .4, "balanced_accuracy": 1/3, "balanced_brier": 4/3,
                        "per_class": {"blue": {"count": 1, "recall": 0., "recall_interval_95": [0., .79]}}}}}
    text = _live_flywheel_status(Client()).renderable.plain
    assert "balanced accuracy" in text
    assert "blue: n=1" in text
    assert "balanced_brier" in text


def test_question_ranking_summary_shows_actual_counts_not_the_full_fold_id_matrix():
    from .reviewer import _question_rankings_text
    report = {"count": 60, "by_class": {"include": 6, "exclude": 54}, "rankings": [
        {"question": {"name": "scope"}, "cross_validated": {"accuracy": .8, "balanced_accuracy": .7, "count": 60},
         "fold_evidence": [{"fit_ids": ["do-not-dump-this-list"]}]}]}
    text = _question_rankings_text(report).plain
    assert "60" in text and "include" in text and "54" in text
    assert "scope" in text and "70.0%" in text
    assert "not deployed accuracy" in text
    assert "do-not-dump-this-list" not in text


def test_optimizer_transcript_displays_actual_messages_replies_and_tool_calls_literally():
    from .reviewer import _optimizer_transcript
    events = ({"kind": "optimizer-request", "messages": [{"role": "user", "content": "[red]my feedback"}]},
              {"kind": "optimizer-response", "content": '{"rationale":"time matters"}',
               "tool_calls": [{"name": "inspect"}], "model": "fake", "usage": {"total_tokens": 9}})
    text = _optimizer_transcript(events).plain
    assert "[red]my feedback" in text
    assert "time matters" in text
    assert "inspect" in text


def test_an_unanswered_optimizer_request_does_not_display_an_older_reply_as_its_response():
    from .reviewer import _optimizer_transcript
    events = ({"kind": "optimizer-request", "briefing_fingerprint": "old",
               "messages": [{"role": "user", "content": "old request"}]},
              {"kind": "optimizer-response", "briefing_fingerprint": "old", "content": "old reply"},
              {"kind": "optimizer-request", "briefing_fingerprint": "new",
               "messages": [{"role": "user", "content": "new feedback"}]})
    text = _optimizer_transcript(events).plain
    assert "new feedback" in text
    assert "old reply" not in text
    assert "No response recorded for this request" in text


def test_a_failed_prediction_is_not_replaced_by_an_unlabeled_local_classifier():
    panel = _article_panel(Article("one", "Title", "Abstract", "2026-10-05", ("cs.AI",)), None)
    assert "unavailable" in panel.renderable.plain


def test_voting_runs_the_core_and_inspection_does_not_create_extra_votes(tmp_path, monkeypatch):
    from io import StringIO
    from rich.console import Console
    from .reviewer import run_review_session
    from .reviewer_predictor import ReviewerPrediction
    from .reviewer_store import ReviewStore
    class Client:
        improves = 0
        retries = []
        def improve(self, *, retry_interrupted=False):
            self.improves += 1
            if retry_interrupted:
                self.retries.append(True)
        def reconcile(self):
            pass
        def predict(self, article):
            return ReviewerPrediction("include", .7, "jev:flywheel-head", "a"*64, 6)
        def status(self):
            return {"version": "a"*64, "rubric": "Practical", "tasks": ["practical"], "example_ids": [],
                    "fitted_head": True, "features": ["practical/yes"], "training_count": 6,
                    "development_count": 2, "by_label": {"include": 3, "exclude": 3},
                    "requests": 0, "ceiling": 100, "latest": None}
        def history(self):
            return ({"kind": "optimizer-request", "messages": [{"role": "user", "content": "my evidence"}]},
                    {"kind": "optimizer-response", "content": '{"rationale":"practical"}', "model": "fake"},
                    {"kind": "decision-request", "target_id": "one", "state": {"rubric": "Practical"},
                     "questions": {"decision": {"criteria": {"include": None}}}})
    answers = iter(["r", "no", "r", "yes", "o", "", "j", "", "i", "My critical explanation", "q"])
    monkeypatch.setattr("decision_flywheel.reviewer.Prompt.ask", lambda *a, **k: next(answers))
    output = StringIO()
    client = Client()
    with ReviewStore(tmp_path / "review.sqlite", study_seed="fixture") as store:
        store.import_articles((Article("one", "One", "Abstract one", "2026-10-05", ("cs.AI",)),
                               Article("two", "Two", "Abstract two", "2026-10-05", ("cs.AI",))))
        run_review_session(store, Console(file=output, width=100), flywheel=client, optimize_every=1)
        assert store.current_label("one") == "include"
        assert store.events_for("one")[0].comment == "My critical explanation"
        assert len(store.events_for("one")) == 1
        assert client.improves >= 1
        assert client.retries == [True]
    assert "my evidence" in output.getvalue()
    assert "Current version rolling-audit agreement: not measured yet" in output.getvalue()
    assert "criteria" in output.getvalue()


def test_live_flywheel_requires_confirmation_before_constructing_clients(monkeypatch):
    from .reviewer import main
    def forbidden(*args, **kwargs):
        pytest.fail("live client constructed without approval")
    monkeypatch.setattr("decision_flywheel.reviewer.JevAdapter.from_environment", forbidden)
    with pytest.raises(SystemExit):
        main(["--live-flywheel"])


def test_jsonl_import_accepts_only_title_abstract_records_with_explicit_metadata(tmp_path):
    path = tmp_path / "articles.jsonl"
    path.write_text(json.dumps({"id": "arxiv-1", "title": "A title", "abstract": "An abstract",
                                "submitted_at": "2026-10-05", "categories": ["cs.AI"],
                                "authors": "Ada Lovelace", "journal_ref": "Journal of Decisions (2026)"}) + "\n",
                    encoding="utf-8")

    articles = load_articles_jsonl(path)

    assert [(article.id, article.categories, article.authors, article.journal_ref) for article in articles] == [
        ("arxiv-1", ("cs.AI",), "Ada Lovelace", "Journal of Decisions (2026)")
    ]


def test_jsonl_import_rejects_source_records_without_the_full_reviewer_payload(tmp_path):
    path = tmp_path / "articles.jsonl"
    path.write_text('{"id": "arxiv-1", "title": "A title"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="line 1"):
        load_articles_jsonl(path)


def test_flywheel_report_loader_accepts_only_the_text_free_summary_shape(tmp_path):
    path = tmp_path / "flywheel.json"
    path.write_text(json.dumps({"version": 1, "model": "jev:jev-latest", "winner": "incumbent",
                                "promoted": False, "reason": "kept incumbent",
                                "calls": {"attempted": 12, "succeeded": 12},
                                "scores": {"incumbent": {"accuracy": .67, "brier": .33}}}), encoding="utf-8")

    report = load_flywheel_report(path)

    assert report["winner"] == "incumbent"


def test_article_panel_identifies_a_jev_prediction_as_the_measured_policy():
    article = Article("arxiv-1", "A title", "An abstract", "2026-10-05", ("cs.AI",))

    panel = _article_panel(article, ReviewerPrediction("include", .8, "jev:incumbent", "a" * 64, 12))

    assert "Measured Jev policy (incumbent)" in panel.renderable.plain


def test_core_status_exposes_how_many_eligible_labels_have_arrived_since_the_frozen_run():
    report = {"version": 1, "model": "jev:jev-latest", "winner": "incumbent", "promoted": False,
              "reason": "kept incumbent", "calls": {"attempted": 12},
              "scores": {"incumbent": {"accuracy": .67, "brier": .33}},
              "training_feedback": {"total": 34, "comments": 2, "hard_jev_corrections": 0}}

    panel = _core_flywheel_status(report, current_training_labels=37)

    assert "3 new eligible labels" in str(panel.renderable)


def test_core_status_exposes_the_real_trials_and_the_held_aside_development_slice():
    report = {"version": 1, "model": "jev:jev-latest", "winner": "hard-swap", "promoted": True,
              "reason": "promoted hard-swap: brier gain 0.120000", "calls": {"attempted": 12},
              "scores": {"incumbent": {"accuracy": .50, "brier": .42},
                         "hard-swap": {"accuracy": .67, "brier": .30}},
              "training_feedback": {"total": 20, "comments": 3, "hard_jev_corrections": 4},
              "round": {"incumbent_source": "prototype-seed", "candidate_count": 14,
                        "development_count": 6},
              "trials": [{"name": "incumbent", "status": "completed", "decision_count": 6,
                          "from_cache": 0, "example_count": 4, "reserve_count": 2,
                          "failure_reasons": [], "objective": .42},
                         {"name": "hard-swap", "status": "completed", "decision_count": 6,
                          "from_cache": 1, "example_count": 4, "reserve_count": 2,
                          "failure_reasons": [], "objective": .30}]}

    panel = _core_flywheel_status(report, current_training_labels=20)

    rendered = str(panel.renderable)
    assert "14 candidate labels and 6 held-aside development labels" in rendered
    assert "incumbent: completed, 6 decisions, 0 cached" in rendered
    assert "hard-swap: completed, 6 decisions, 1 cached" in rendered


def test_core_status_exposes_the_active_feature_names_from_the_generic_run_ledger():
    round_ = FlywheelRound(
        task_fingerprint="a" * 64, input_fingerprint="b" * 64, active_policy_fingerprint="c" * 64,
        model_fingerprint="jev:example", feedback_count=12, candidate_count=8, development_count=4,
        objective_name="brier", winner="incumbent", promoted=False, outcome="incumbent-retained",
        calls_attempted=8, calls_succeeded=8, trials=(),
    )
    report = {"version": 1, "model": "jev:example", "winner": "incumbent", "promoted": False,
              "reason": "kept incumbent", "calls": {}, "scores": {}}

    panel = _core_flywheel_status(report, current_training_labels=12,
                                  ledger_status=FlywheelStatus("current", round_, 1))

    assert "Active decision elements: none; this policy currently uses context only." in str(panel.renderable)


def test_core_status_exposes_recent_text_free_optimizer_activity():
    report = {"version": 1, "model": "jev:example", "winner": "incumbent", "promoted": False,
              "reason": "kept incumbent", "calls": {}, "scores": {}}
    events = (FlywheelEvent("trial-started", "incumbent", None, 0, 0),
              FlywheelEvent("decision-reused", "incumbent", "a" * 64, 0, 0),
              FlywheelEvent("trial-completed", "incumbent", None, 0, 0))

    panel = _core_flywheel_status(report, current_training_labels=12, events=events)

    assert "Recent optimizer activity: incumbent trial-started → incumbent decision-reused" in str(panel.renderable)
