"""A local Rich terminal for collecting careful article-inclusion feedback.

This is intentionally a human-review surface, not a model runner.  It displays
only source metadata and records human events through :mod:`reviewer_store`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Callable, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from .adapters.jev import JevAdapter, JevConfiguration
from .adapters.workspace import decision_adapter
from .decision_provider_settings import normalize_decision_settings
from .credential_redaction import credential_values
from .artifacts import load_artifact
from .example_list import plan_example_list_round
from .events import FlywheelEvent, JsonlEventStream
from .reviewer_core import reviewer_item, reviewer_labeled_items, reviewer_task
from .reviewer_predictor import LabeledArticle, ReviewerPrediction, predict_article
from .reviewer_store import Article, PredictionMetrics, ReviewStore
from .run_ledger import FlywheelStatus, JsonlRunLedger, feedback_fingerprint
from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .optimizer_agent import OptimizerAgent
from .reviewer_flywheel import ReviewerFlywheel


def load_articles_jsonl(path: str | Path) -> tuple[Article, ...]:
    """Load the deliberately small, explicit interchange format for the reviewer."""
    articles = []
    seen = set()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"cannot read article JSONL: {path}") from error
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            required = {"id", "title", "abstract", "submitted_at", "categories"}
            optional = {"authors", "journal_ref"}
            if not isinstance(value, dict) or not required <= set(value) or not set(value) <= required | optional:
                raise ValueError("needs title, abstract, date, categories, and optional authors or journal_ref")
            categories = value["categories"]
            if not isinstance(categories, list):
                raise ValueError("categories must be a JSON array")
            article = Article(value["id"], value["title"], value["abstract"], value["submitted_at"],
                              tuple(categories), value.get("authors", "Not provided"), value.get("journal_ref"))
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ValueError(f"article JSONL line {number} is invalid: {error}") from error
        if article.id in seen:
            raise ValueError(f"article JSONL line {number} repeats ID {article.id!r}")
        seen.add(article.id)
        articles.append(article)
    if not articles:
        raise ValueError("article JSONL contains no records")
    return tuple(articles)


def load_flywheel_report(path: str | Path) -> dict[str, object] | None:
    """Load the compact, text-free status record emitted by the core runner."""
    report_path = Path(path)
    if not report_path.exists():
        return None
    try:
        value = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("flywheel report is unreadable") from error
    required = {"version", "model", "winner", "promoted", "reason", "calls", "scores"}
    if not isinstance(value, dict) or not required <= set(value):
        raise ValueError("flywheel report has an unexpected shape")
    if value["version"] != 1 or not isinstance(value["model"], str) or not isinstance(value["winner"], str):
        raise ValueError("flywheel report has invalid metadata")
    if not isinstance(value["promoted"], bool) or not isinstance(value["reason"], str):
        raise ValueError("flywheel report has invalid outcome")
    if not isinstance(value["calls"], dict) or not isinstance(value["scores"], dict):
        raise ValueError("flywheel report has invalid results")
    return value


def _summary_table(summary: dict[str, int], metrics: PredictionMetrics) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="bold cyan")
    table.add_column(justify="right")
    for label, key in (("Articles", "articles"), ("Include", "include"), ("Exclude", "exclude"),
                       ("Skipped", "skip"), ("Remaining", "unreviewed")):
        table.add_row(label, str(summary[key]))
    agreement = "—" if metrics.accuracy is None else f"{metrics.correct_votes}/{metrics.scored_votes} ({metrics.accuracy:.0%})"
    table.add_row("Historical prediction agreement", agreement)
    table.add_row("Eligible labels", str(summary["train_labels"]))
    table.add_row("Prediction versions seen", str(metrics.model_refreshes))
    return table


def _core_flywheel_status(report: dict[str, object] | None, *, current_training_labels: int,
                           ledger_status: FlywheelStatus | None = None,
                           events: tuple[FlywheelEvent, ...] = ()) -> Panel:
    """Render the measured core policy and whether new feedback has outgrown it."""
    if report is None:
        message = "No measured Jev flywheel run yet. Run `make run-flywheel` to evaluate the existing core on eligible feedback."
    else:
        calls = report["calls"]
        scores = report["scores"]
        feedback_value = report.get("training_feedback", {})
        feedback = feedback_value if isinstance(feedback_value, dict) else {}
        formatted = " · ".join(
            f"{name}: {values.get('accuracy', 0):.0%} accuracy / {values.get('brier', 0):.3f} Brier"
            for name, values in sorted(scores.items()) if isinstance(values, dict)
        )
        used_labels = feedback.get("total", 0)
        if not isinstance(used_labels, int) or isinstance(used_labels, bool):
            used_labels = 0
        new_labels = max(0, current_training_labels - used_labels)
        freshness = ("Policy is current for eligible labels."
                     if new_labels == 0 else
                     f"Policy is stale: {new_labels} new eligible labels await the next measured core round.")
        round_info = report.get("round", {})
        if isinstance(round_info, dict):
            candidate_count = round_info.get("candidate_count")
            development_count = round_info.get("development_count")
            source = round_info.get("incumbent_source")
            round_line = (f"Round design: {candidate_count} candidate labels and {development_count} held-aside "
                          f"development labels; incumbent source: {source}."
                          if isinstance(candidate_count, int) and isinstance(development_count, int)
                          and isinstance(source, str) else "")
        else:
            round_line = ""
        trial_lines = []
        trials = report.get("trials", [])
        if isinstance(trials, list):
            for trial in trials:
                if not isinstance(trial, dict):
                    continue
                name, status = trial.get("name"), trial.get("status")
                decisions, cached = trial.get("decision_count"), trial.get("from_cache")
                if (isinstance(name, str) and isinstance(status, str) and isinstance(decisions, int)
                        and isinstance(cached, int)):
                    trial_lines.append(f"{name}: {status}, {decisions} decisions, {cached} cached")
        activity_line = f"Measured trials: {' · '.join(trial_lines)}." if trial_lines else ""
        ledger_line = ""
        if ledger_status is not None:
            if ledger_status.phase == "current":
                ledger_line = (f"Run ledger: {ledger_status.completed_rounds} measured round(s); "
                               "the frozen policy matches current eligible feedback.")
            elif ledger_status.phase == "stale":
                ledger_line = (f"Run ledger: {ledger_status.completed_rounds} measured round(s); "
                               "new feedback requires another measured round.")
            if ledger_status.latest is not None:
                features = ledger_status.latest.active_features
                feature_line = ("Active decision elements: "
                                + (", ".join(feature.key for feature in features)
                                   if features else "none; this policy currently uses context only.") )
            else:
                feature_line = ""
        else:
            feature_line = ""
        event_line = ""
        if events:
            recent = events[-5:]
            event_line = "Recent optimizer activity: " + " → ".join(
                f"{event.trial_name or 'round'} {event.event_type}" for event in recent
            ) + "."
        message = (f"Last measured Jev run — {report['reason']}\n"
                   f"Winner: {report['winner']} · {calls.get('attempted', 0)} new Jev requests. {formatted}\n"
                   f"Feedback used: {feedback.get('total', 0)} eligible labels, "
                   f"{feedback.get('comments', 0)} comments, "
                   f"{feedback.get('hard_jev_corrections', 0)} wrong-Jev corrections for hard-swap.\n"
                   + (f"{round_line}\n" if round_line else "")
                   + (f"{activity_line}\n" if activity_line else "")
                   + (f"{ledger_line}\n" if ledger_line else "")
                   + (f"{feature_line}\n" if feature_line else "")
                   + (f"{event_line}\n" if event_line else "")
                   + freshness)
    return Panel(message, title="Measured Decision Flywheel", border_style="green", padding=(0, 1))


def _live_flywheel_status(client) -> Panel:
    status = client.status()
    latest = status["latest"] or {}
    head = "Fitted ML head" if status["fitted_head"] else "Jev main decision only — waiting for a fitted head"
    balance = ", ".join(f"{count} {label}" for label, count in status["by_label"].items())
    lines = [f"Active version: {status['version'][:12]} · {head}",
             f"Training: {status['training_count']} ({balance}) · Development: {status['development_count']}",
             f"Rubric: {status['rubric'] or '(not yet inferred)'}",
             f"Validation: {status.get('validation_status','not recorded')}",
             f"Classification tasks: {', '.join(status['tasks']) or '(main decision only)'}",
             f"Fixed examples: {', '.join(status['example_ids']) or '(none)'}",
             f"ML features: {', '.join(status['features']) or '(head not fitted)'}",
             f"Latest activity: {latest.get('kind', 'not run')} — {latest.get('reason', '')}",
             f"Jev requests this session: {status['requests']}/{status['ceiling']}"]
    lines.append(f"Evaluation weighting: {status.get('evaluation_weighting', 'not recorded')} · "
                 f"Training weighting: {status.get('training_class_weighting', 'not recorded')}")
    context = status.get("optimizer_context", {})
    lines.append(f"Human explanation context: {len(context.get('human_explanations', []))} statement(s) · inspect with O/F")
    if context.get("evaluation_context_exposed"):
        lines.append("Evaluation warning: protected-role explanations are optimizer guidance; old holdouts are not independent.")
    if latest.get("development_counts") is not None:
        lines.append(f"Development counts: {latest['development_counts']} · "
                     f"minimum per class: {latest.get('minimum_development_per_class')}")
    if latest.get("promotion_metric"):
        lines.append(f"Promotion metric: {latest['promotion_metric']}")
    trained = latest.get("selected") or latest.get("classifier_training", {}).get("selected")
    if trained:
        lines.append("Selected numerical candidate: " + trained["feature_set"] + " · " + trained["training_class_weighting"])
        latest = {**latest, "incumbent": trained["incumbent"], "candidate": trained["candidate"]}
    for name in ("incumbent", "candidate"):
        if name in latest:
            score = latest[name]
            lines.append(f"{name}: {score['accuracy']:.1%} development agreement · {score['brier']:.4f} Brier")
            if score.get("balanced_accuracy") is not None:
                lines.append(f"{name}: {score['balanced_accuracy']:.1%} balanced accuracy · "
                             f"{score['balanced_brier']:.4f} equal-class Brier")
            for label, group in score.get("per_class", {}).items():
                recall = f"{group['recall']:.1%}" if group['recall'] is not None else "not measured"
                interval = group.get("recall_interval_95")
                uncertainty = f" · Wilson 95% {interval[0]:.0%}–{interval[1]:.0%}" if interval else ""
                lines.append(f"  {name}/{label}: n={group['count']} · recall {recall}{uncertainty}")
    lines.append(f"Recorded optimizer requests/replies: {status.get('optimizer_requests_recorded', 0)}/"
                 f"{status.get('optimizer_responses_recorded', 0)}")
    lines.append("O optimizer transcript · F active configuration · J Jev requests · G configured stage · M train classifier · R retry")
    bank = status.get("feature_bank", ())
    lines.append(f"Scheduled stage: {status.get('optimization_stage', 'legacy')} · "
                 f"question backfill limit: {status.get('retrospective_limit', 200)}")
    if latest.get("rankings") is not None:
        lines.append(f"Matched human-feedback window: {latest.get('count', 0)} · {latest.get('by_class', {})}")
        for rank in latest["rankings"]:
            metric = rank.get("cross_validated")
            lines.append(f"  {rank['question']['name']}: " +
                         (f"OOF agreement {metric['accuracy']:.1%}, balanced {metric['balanced_accuracy']:.1%}, n={metric['count']}"
                          if metric else "insufficient per-class coverage for OOF mapping"))
    lines.append(f"Feature bank: {len(bank)} questions · "
                 f"{sum(entry['state'] == 'deployed' for entry in bank)} deployed · H for definitions and trial signal")
    lines.append("H feature bank and historical hypotheses (inspection only)")
    lines.append("N discover/backfill questions and train · X example selection · M retrain retained questions")
    return Panel(Text("\n".join(lines)), title="Live Decision Flywheel", border_style="green")


def _question_rankings_text(report) -> Text:
    lines = ["Retrospective OOF feature ranking — not deployed accuracy",
             f"Matched window: {report.get('count', 0)} human labels · {report.get('by_class', {})}"]
    for rank in report.get("rankings", ()):
        metric = rank.get("cross_validated")
        lines.append(rank["question"]["name"] + ": " +
                     (f"agreement {metric['accuracy']:.1%} · balanced {metric['balanced_accuracy']:.1%} · n={metric['count']}"
                      if metric else "insufficient class coverage for OOF mapping"))
    return Text("\n".join(lines))


def _optimizer_transcript(events) -> Text:
    request_index = next((index for index in range(len(events) - 1, -1, -1)
                          if events[index]['kind'] == 'optimizer-request'), None)
    request = events[request_index] if request_index is not None else None
    response = next((event for event in reversed(events[request_index + 1:])
                     if event['kind'] == 'optimizer-response'
                     and event.get('briefing_fingerprint') == request.get('briefing_fingerprint')), None) if request else None
    lines = ["Latest actual optimizer transcript (private local record)"]
    if request:
        for message in request["messages"]:
            lines.extend([f"\n{message['role'].upper()}:", message["content"]])
    else:
        lines.append("No optimizer request yet; collect enough eligible labels first.")
    if response:
        lines.extend(["\nRESPONSE:", response["content"], "\nTOOL CALLS:",
                      json.dumps(response.get("tool_calls", []), indent=2),
                      f"Model: {response.get('model')} · Usage: {response.get('usage')}"])
    elif request:
        lines.append("\nNo response recorded for this request; inspect the latest activity for failure or progress.")
    return Text("\n".join(lines))


def _optimizer_request_context(event) -> Text:
    payload = json.loads(event["messages"][-1]["content"])
    explanations = payload.get("human_explanations", [])
    lines = ["Exact human_explanations field sent with this request:"]
    lines.extend(f"{i}. {text}" for i, text in enumerate(explanations, 1))
    if not explanations:
        lines.append("(empty)")
    lines.append(f"Eligible labeled items: {len(payload.get('feedback', []))}")
    lines.append(f"Request fingerprint: {event['briefing_fingerprint']}")
    lines.append("O shows the complete actual system/user messages, reply and tool calls.")
    return Text("\n".join(lines))


def _decision_transcript(events) -> Text:
    request = next((event for event in reversed(events) if event["kind"] == "decision-request"), None)
    if not request:
        return Text("No provider-bound Jev request has been recorded yet.")
    response = next((event for event in reversed(events) if event["kind"] == "decision-response"
                     and event.get("target_id") == request.get("target_id")), None)
    return Text("Latest actual Jev request (private local record)\n" + json.dumps(request, indent=2, ensure_ascii=False)
                + "\n\nReturned structured answer\n" + json.dumps(response, indent=2, ensure_ascii=False))


def _article_panel(article: Article, prediction: ReviewerPrediction | None) -> Panel:
    header = Text(article.title, style="bold white")
    metadata = Text(f"Submitted {article.submitted_at}  •  {' · '.join(article.categories)}", style="cyan")
    authors = Text(f"Authors: {article.authors}", style="cyan")
    publication = Text(f"Published as: {article.journal_ref}\n", style="cyan") if article.journal_ref else Text()
    if prediction is None:
        body = Text.assemble(header, "\n", metadata, "\n", authors, "\n", publication,
                             "\nCurrent system prediction unavailable. No substitute classifier is being shown.\n\n",
                             article.abstract)
        return Panel(body, title="Article review", border_style="yellow", padding=(1, 2))
    label = "INCLUDE" if prediction.label == "include" else "EXCLUDE"
    source = ("Decision Flywheel: trained ML head over decision-model features" if prediction.kind in {"decision:flywheel-head","jev:flywheel-head"} else
              "Decision Flywheel warm-up: main decision, no fitted head yet" if prediction.kind in {"decision:flywheel-warmup","jev:flywheel-warmup"} else
              f"Measured Jev policy ({prediction.kind.removeprefix('jev:')})"
              if prediction.kind.startswith("jev:") else
              "Cold-start fallback" if prediction.kind == "cold_start_prior" else
              "Local fallback — not the measured Jev policy")
    prediction_text = Text.assemble(
        "Current system prediction: ",
        (label, "bold green" if prediction.label == "include" else "bold red"),
        f" — {prediction.confidence:.0%} confidence\n",
        source,
        f" · {prediction.training_label_count} eligible human label(s)",
    )
    body = Text.assemble(header, "\n", metadata, "\n", authors, "\n", publication, "\n", prediction_text,
                         "\n\n", article.abstract)
    return Panel(body, title="Article review",
                 subtitle="I include + optional comment · E exclude + optional comment · S skip · B undo · Q quit",
                 border_style="blue", padding=(1, 2))


def _optional_comment(console: Console) -> str | None:
    value = Prompt.ask("Optional comment", default="").strip()
    return value or None


def run_review_session(store: ReviewStore, console: Console | None = None,
                       flywheel_report: dict[str, object] | None = None,
                       run_ledger: JsonlRunLedger | None = None,
                       event_stream: JsonlEventStream | None = None,
                       predict: Callable[[Article], ReviewerPrediction] | None = None,
                       flywheel: ReviewerFlywheel | None = None, optimize_every: int = 5) -> None:
    """Run the review loop and preserve every prediction shown before a vote."""
    console = console or Console()
    if type(optimize_every) is not int or optimize_every < 1:
        raise ValueError("optimize_every must be positive")
    console.print("[bold]Knowledge-base article reviewer[/bold]")
    console.print("Your choices and each displayed prediction are logged locally.\n")
    if flywheel:
        flywheel.reconcile()
    while True:
        article = store.next_unreviewed()
        console.clear()
        summary = store.summary()
        metrics = store.prediction_metrics(assignments=("train", "rolling_audit"))
        core_labels = reviewer_labeled_items(store.learning_feedback(), store.article)
        ledger_status = (run_ledger.status(feedback_fingerprint(reviewer_task(), core_labels))
                         if run_ledger is not None else None)
        console.print(_summary_table(summary, metrics))
        if flywheel:
            current_metrics = store.prediction_metrics(assignments=("rolling_audit",),
                                                       predictor_fingerprint=flywheel.status()["version"])
            current_score = (f"{current_metrics.correct_votes}/{current_metrics.scored_votes} "
                             f"({current_metrics.accuracy:.0%})" if current_metrics.accuracy is not None else
                             "not measured yet — no reviewed rolling-audit predictions for this version")
            console.print(Text("Current version rolling-audit agreement: " + current_score))
            console.print(_live_flywheel_status(flywheel))
        else:
            console.print(_core_flywheel_status(flywheel_report, current_training_labels=summary["train_labels"],
                                             ledger_status=ledger_status,
                                             events=event_stream.history() if event_stream is not None else ()))
        labels = tuple(LabeledArticle(store.article(label.article_id), label.label) for label in store.learning_labels())
        if article is None:
            console.print(Panel("There are no unreviewed articles in this batch.", border_style="green"))
            return
        shown = None
        try:
            prediction = (flywheel.predict(article) if flywheel else
                          predict(article) if predict is not None else predict_article(article, labels))
            shown = store.record_prediction(article.id, prediction.label, prediction.confidence, prediction.kind,
                                            prediction.fingerprint, prediction.training_label_count)
        except Exception as error:
            if flywheel is None:
                raise
            prediction = None
            console.print(Text(f"Prediction unavailable ({type(error).__name__}); feedback can still be saved."))
        console.print(_article_panel(article, prediction))
        while True:
            choices = ("i", "e", "s", "b", "q", "o", "f", "j", "g", "r", "h", "n", "x", "m") if flywheel else ("i", "e", "s", "b", "q")
            action = Prompt.ask("Action", choices=choices + tuple(key.upper() for key in choices),
                                show_choices=False).lower()
            if flywheel and action in ("o", "f", "j"):
                content = (_optimizer_transcript(flywheel.history()) if action == "o" else
                           _decision_transcript(flywheel.history()) if action == "j" else
                           Text(json.dumps(flywheel.status(), indent=2, ensure_ascii=False)))
                console.print(content)
                Prompt.ask("Press Enter to return to this article", default="")
                continue
            if flywheel and action == "g":
                flywheel.improve(trigger="reviewer-G")
                console.print(_live_flywheel_status(flywheel))
                continue
            if flywheel and action in ("n", "x", "m"):
                flywheel.improve(stage={"n": "questions", "x": "examples", "m": "classifier"}[action], trigger="reviewer-" + action.upper())
                console.print(_live_flywheel_status(flywheel))
                continue
            if flywheel and action == "r":
                answer = Prompt.ask("Retry the interrupted optimizer round within this session's paid ceilings? "
                                    "Only do this when no other reviewer session is running",
                                    choices=("yes", "no"), default="no")
                if answer == "yes":
                    flywheel.improve(retry_interrupted=True)
                console.print(_live_flywheel_status(flywheel))
                continue
            if flywheel and action == "h":
                hypotheses = flywheel.hypotheses()
                if action == "h":
                    console.print(Panel(Text(json.dumps(flywheel.feature_bank(), indent=2, ensure_ascii=False)),
                                        title="Feature bank: exploration is not deployment"))
                console.print(Text(json.dumps(hypotheses, indent=2, ensure_ascii=False)))
                Prompt.ask("Press Enter to return to this article", default="")
                continue
            break
        if action == "q":
            if flywheel:
                flywheel.finish_cycle()
            console.print("Review session saved locally.")
            return
        if action == "b":
            restored = store.undo_last_vote()
            if restored is None:
                console.print("Nothing to undo.")
                Prompt.ask("Press Enter to continue", default="")
            else:
                console.print(f"Undid the last action; {restored.id} is back in the queue.")
                if flywheel:
                    flywheel.record_review_event(store.events_for(restored.id)[-1])
                    flywheel.reconcile()
                Prompt.ask("Press Enter to continue", default="")
            if flywheel:
                flywheel.finish_cycle()
            continue
        if action == "s":
            store.record_skip(article.id)
            if flywheel:
                flywheel.core._emit({'kind':'review-skipped','target_id':article.id})
                flywheel.finish_cycle()
            continue
        label = "include" if action == "i" else "exclude"
        comment = _optional_comment(console)
        vote = store.record_vote(article.id, label, comment=comment, presentation_id=shown.id if shown else None)
        if flywheel:
            flywheel.record_review_event(vote)
            if flywheel.feedback_trigger(optimize_every):
                flywheel.improve(trigger="label-transitions" if getattr(flywheel,'rubric_trigger',None) and flywheel.stage=='rubric' else "feedback-cadence")
            flywheel.finish_cycle()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Rich reviewer for title-and-abstract article decisions")
    parser.add_argument("--database", type=Path, default=Path("reviewer.sqlite3"),
                        help="local SQLite review-event database")
    parser.add_argument("--articles", type=Path, help="JSONL source to import before opening the reviewer")
    parser.add_argument("--study-seed", default="arxiv-review-v1", help="stable assignment seed for a new database")
    parser.add_argument("--rolling-audit-rate", type=float, default=.2)
    parser.add_argument("--final-audit-rate", type=float, default=.1)
    parser.add_argument("--flywheel-report", type=Path,
                        help="text-free report from scripts/run_reviewer_flywheel.py")
    parser.add_argument("--flywheel-ledger", type=Path,
                        help="text-free reusable flywheel run ledger")
    parser.add_argument("--flywheel-events", type=Path,
                        help="text-free live optimizer event stream")
    parser.add_argument("--live-jev", action="store_true",
                        help="serve predictions through the selected, measured Jev artifact")
    parser.add_argument("--live-flywheel", action="store_true",
                        help="predict and optimize through the reusable feedback/rubric/features/head loop")
    parser.add_argument("--runtime-database", type=Path,
                        help="private persistent flywheel state and actual request/reply transcripts")
    parser.add_argument("--optimizer-model", default="gpt-6-luna")
    parser.add_argument("--optimizer-transport", choices=("openai", "litellm"), default="openai")
    parser.add_argument("--decisions-provider", choices=("jev", "kev", "laya"), default="jev",
                        help="decision adapter for the integrated flywheel; Laya requires its optional local model")
    parser.add_argument("--decisions-model", help="explicit model identifier; otherwise use the selected provider's default")
    parser.add_argument("--evaluation-weighting", choices=("natural", "equal_class"), default="equal_class")
    from .selection_policy import add_selection_arguments, selection_from_arguments
    add_selection_arguments(parser)
    parser.add_argument("--training-class-weighting", choices=("natural", "equal_class"), default="natural")
    parser.add_argument("--min-evaluation-per-class", type=int, default=2)
    parser.add_argument("--max-optimizer-calls", type=int, default=10)
    parser.add_argument("--optimization-stage", choices=("rubric", "examples", "questions", "classifier"), default="rubric")
    parser.add_argument('--rubric-changes-every',type=int,default=2,
                        help='optimize rubric after this many human-label transitions')
    parser.add_argument("--retrospective-limit", type=int, default=200)
    parser.add_argument("--stage-min-evaluation-per-class", type=int, default=20)
    parser.add_argument("--optimize-every", type=int, default=10,
                        help="vote cadence for non-rubric stages; rubric uses --rubric-changes-every; G requests a round sooner")
    parser.add_argument("--confirm-live", action="store_true",
                        help="required for live modes: authorizes the explicit decision and optimizer ceilings")
    parser.add_argument("--max-live-requests", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        selection_policy = selection_from_arguments(args)
    except ValueError as error:
        parser.error(str(error))
    if args.live_jev and args.decisions_provider!='jev':
        parser.error('legacy Jev artifact mode requires the Jev provider; use --live-flywheel for other providers')
    try:
        decision_settings=normalize_decision_settings({'decisions_provider':args.decisions_provider,
            **({'decisions_model':args.decisions_model} if args.decisions_model is not None else {})})
    except ValueError as error:
        parser.error(str(error))
    args.decisions_model=decision_settings['decisions_model']
    if args.live_flywheel or args.live_jev:
        if not args.confirm_live:
            parser.error("refusing paid model calls without --confirm-live")
        if min(args.max_live_requests, args.max_optimizer_calls, args.optimize_every, args.min_evaluation_per_class,
               args.retrospective_limit, args.stage_min_evaluation_per_class,args.rubric_changes_every) < 1:
            parser.error("request ceilings and optimization interval must be positive")
    if args.live_flywheel and args.live_jev:
        parser.error("choose the integrated flywheel or legacy artifact serving, not both")
    articles = load_articles_jsonl(args.articles) if args.articles else ()
    with ReviewStore(args.database, study_seed=args.study_seed, rolling_audit_rate=args.rolling_audit_rate,
                     final_audit_rate=args.final_audit_rate) as store:
        if articles:
            imported = store.import_articles(articles)
            Console().print(f"Imported {imported} new article(s).")
        if not store.articles():
            parser.error("supply --articles for a new review database")
        if args.live_flywheel:
            from .adapters.optimizer_transport import optimizer_transport
            adapter = decision_adapter(decision_settings)
            transport = optimizer_transport(vars(args))
            console = Console()
            console.print(Text(f"Authorized live mode: at most {args.max_live_requests} decision requests and "
                               f"{args.max_optimizer_calls} optimizer requests in this session. "
                               f"Decisions: {args.decisions_provider}/{args.decisions_model}. "
                               f"Optimizer: {args.optimizer_transport}/{args.optimizer_model}."))
            def observe(event):
                kind = event["kind"]
                if kind == "decision-request":
                    console.print(Text(f"Decision request: {event['target_id']} · {len(event['questions'])} question(s)"))
                elif kind == "optimizer-request":
                    console.print(Panel(_optimizer_request_context(event), title="Actual optimizer request context"))
                elif kind == "step-started":
                    console.print(Text(f"Step: {event['step_stage']} · trigger: {event['trigger']} · {event['step_id']}"))
                elif kind in {"step-paused", "step-failed", "step-completed"}:
                    console.print(Text(f"Step {event['step_stage']}: {kind} · {event.get('reason', event.get('status', event.get('error_type', '')))}"))
                elif kind == "fit-started":
                    console.print(Text(f"ML fit started: {event['training_count']} labels · {len(event['features'])} features"))
                elif kind == "fit-completed":
                    console.print(Text(f"ML fit completed: {event['training_count']} labels · calibration: {event['calibration']}"))
                elif kind == "question-backfill-progress":
                    if event["completed"] % 10 == 0 or event["completed"] == event["total"]:
                        console.print(Text(f"Question backfill: {event['completed']}/{event['total']}"))
                elif kind == "question-ranking-completed":
                    console.print(Panel(_question_rankings_text(event), title="Question alignment"))
                elif kind == "optimization-stage-completed" and "rankings" in event:
                    pass  # The preceding ranking event already displayed this result.
                elif kind == "optimizer-response":
                    try:
                        rationale = json.loads(event["content"]).get("rationale", "(no stated rationale)")
                    except (ValueError, AttributeError):
                        rationale = "Malformed reply; inspect with O."
                    console.print(Panel(Text(str(rationale)), title="Optimizer's stated rationale"))
                    console.print(Text("Optimizer tool calls: " + json.dumps(event.get("tool_calls", []), ensure_ascii=False)))
                elif kind in {"optimizer-request", "fit-started", "fit-completed", "candidate-evaluated",
                              "promoted", "candidate-rejected", "round-failed", "waiting-for-labels",
                              "classifier-invalidated", "round-interrupted", "round-retry-authorized", "hypothesis-retry",
                              "hypothesis-discovered", "discovery-no-change", "control-trial-started",
                              "control-trial-completed", "control-trial-deferred", "control-cycle-completed",
                              "feature-discovered", "feature-diagnostics", "optimization-stage-started",
                              "optimization-stage-completed", "optimization-stage-failed", "question-backfill-started", "question-backfill-progress",
                              "question-ranking-completed", "classifier-training-started", "classifier-training-completed"}:
                    console.print(Text(f"Flywheel: {kind} · " + json.dumps(
                        {key: value for key, value in event.items() if key not in {"messages", "created_at", "kind"}},
                        ensure_ascii=False)))
            runtime = DecisionFlywheel(args.runtime_database or args.database.parent / "reviewer-runtime.sqlite3",
                ClassifierConfig(reviewer_task()), adapter, OptimizerAgent(transport), observer=observe,
                max_requests=args.max_live_requests,
                evaluation_weighting=args.evaluation_weighting,
                selection_policy=selection_policy,
                training_class_weighting=args.training_class_weighting,
                min_evaluation_per_class=args.min_evaluation_per_class,
                redact=credential_values(os.environ))
            try:
                run_review_session(store, console, flywheel=ReviewerFlywheel(store, runtime,
                                   stage=args.optimization_stage, retrospective_limit=args.retrospective_limit,
                                   min_stage_evaluation_per_class=args.stage_min_evaluation_per_class,
                                   rubric_changes_every=args.rubric_changes_every),
                                   optimize_every=args.optimize_every)
            finally:
                runtime.close()
            return 0
        report_path = args.flywheel_report or args.database.parent / "reviewer-flywheel.json"
        report = load_flywheel_report(report_path)
        ledger_path = args.flywheel_ledger or args.database.parent / "reviewer-flywheel-runs.jsonl"
        ledger = JsonlRunLedger(ledger_path)
        event_path = args.flywheel_events or args.database.parent / "reviewer-flywheel-events.jsonl"
        event_stream = JsonlEventStream(event_path)
        live_predict = None
        if args.live_jev:
            if not args.confirm_live:
                parser.error("refusing paid Jev predictions without --confirm-live")
            if args.max_live_requests < 1:
                parser.error("--max-live-requests must be positive")
            if report is None or not isinstance(report.get("artifact"), dict):
                parser.error("run the measured flywheel first; no live artifact report is available")
            artifact_info = report["artifact"]
            artifact_path = Path(str(artifact_info.get("path", "")))
            pool_revision = artifact_info.get("pool_revision")
            if not artifact_path.is_file() or not isinstance(pool_revision, str):
                parser.error("the flywheel artifact is missing or invalid")
            core_labels = reviewer_labeled_items(store.learning_feedback(), store.article)
            round_plan = plan_example_list_round(reviewer_task(), core_labels, per_label=2, dev_max=6)
            artifact = load_artifact(artifact_path.read_text(encoding="utf-8"), reviewer_task(),
                                     round_plan.candidates, pool_revision)
            adapter = JevAdapter.from_environment(configuration=JevConfiguration(model=args.decisions_model))
            request_count = 0

            def live_predict(article: Article) -> ReviewerPrediction:
                nonlocal request_count
                if request_count >= args.max_live_requests:
                    raise RuntimeError("the live Jev session request ceiling has been reached")
                request_count += 1
                result = asyncio.run(artifact.apply(reviewer_task(), reviewer_item(article), adapter,
                                                    model_fingerprint=adapter.model_identity))
                confidence = result.confidence
                if confidence is None and result.probabilities:
                    confidence = max(result.probabilities.values())
                return ReviewerPrediction(result.label, confidence if confidence is not None else .5,
                                          f"jev:{report['winner']}", artifact.artifact_hash,
                                          len(core_labels))

        run_review_session(store, flywheel_report=report, run_ledger=ledger, event_stream=event_stream,
                           predict=live_predict)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
