"""A local Rich terminal for collecting careful article-inclusion feedback.

This is intentionally a human-review surface, not a model runner.  It displays
only source metadata and records human events through :mod:`reviewer_store`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Callable, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from .adapters.jev import JevAdapter, JevConfiguration
from .artifacts import load_artifact
from .example_list import plan_example_list_round
from .reviewer_core import reviewer_item, reviewer_labeled_items, reviewer_task
from .reviewer_predictor import LabeledArticle, ReviewerPrediction, predict_article
from .reviewer_store import Article, PredictionMetrics, ReviewStore


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
    table.add_row("Prediction agreement", agreement)
    table.add_row("Eligible labels", str(summary["train_labels"]))
    table.add_row("Model refreshes", str(metrics.model_refreshes))
    return table


def _core_flywheel_status(report: dict[str, object] | None) -> Panel:
    if report is None:
        message = "No measured Jev flywheel run yet. Run `make run-flywheel` to evaluate the existing core on eligible feedback."
    else:
        calls = report["calls"]
        scores = report["scores"]
        formatted = " · ".join(
            f"{name}: {values.get('accuracy', 0):.0%} accuracy / {values.get('brier', 0):.3f} Brier"
            for name, values in sorted(scores.items()) if isinstance(values, dict)
        )
        message = (f"Last measured Jev run — {report['reason']}\n"
                   f"Winner: {report['winner']} · {calls.get('attempted', 0)} Jev requests. {formatted}")
    return Panel(message, title="Measured Decision Flywheel", border_style="green", padding=(0, 1))


def _article_panel(article: Article, prediction: ReviewerPrediction) -> Panel:
    header = Text(article.title, style="bold white")
    metadata = Text(f"Submitted {article.submitted_at}  •  {' · '.join(article.categories)}", style="cyan")
    authors = Text(f"Authors: {article.authors}", style="cyan")
    publication = Text(f"Published as: {article.journal_ref}\n", style="cyan") if article.journal_ref else Text()
    label = "INCLUDE" if prediction.label == "include" else "EXCLUDE"
    source = (f"Measured Jev policy ({prediction.kind.removeprefix('jev:')})"
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
                       predict: Callable[[Article], ReviewerPrediction] | None = None) -> None:
    """Run the review loop and preserve every prediction shown before a vote."""
    console = console or Console()
    console.print("[bold]Knowledge-base article reviewer[/bold]")
    console.print("Your choices and each displayed prediction are logged locally.\n")
    while True:
        article = store.next_unreviewed()
        console.clear()
        summary = store.summary()
        metrics = store.prediction_metrics()
        console.print(_summary_table(summary, metrics))
        console.print(_core_flywheel_status(flywheel_report))
        labels = tuple(LabeledArticle(store.article(label.article_id), label.label) for label in store.learning_labels())
        if article is None:
            console.print(Panel("There are no unreviewed articles in this batch.", border_style="green"))
            return
        prediction = predict(article) if predict is not None else predict_article(article, labels)
        shown = store.record_prediction(article.id, prediction.label, prediction.confidence, prediction.kind,
                                        prediction.fingerprint, prediction.training_label_count)
        console.print(_article_panel(article, prediction))
        action = Prompt.ask("Action", choices=("i", "e", "s", "b", "q", "I", "E", "S", "B", "Q"),
                            show_choices=False).lower()
        if action == "q":
            console.print("Review session saved locally.")
            return
        if action == "b":
            restored = store.undo_last_vote()
            if restored is None:
                console.print("Nothing to undo.")
                Prompt.ask("Press Enter to continue", default="")
            else:
                console.print(f"Undid the last action; {restored.id} is back in the queue.")
                Prompt.ask("Press Enter to continue", default="")
            continue
        if action == "s":
            store.record_skip(article.id)
            continue
        label = "include" if action == "i" else "exclude"
        comment = _optional_comment(console)
        store.record_vote(article.id, label, comment=comment, presentation_id=shown.id)


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
    parser.add_argument("--live-jev", action="store_true",
                        help="serve predictions through the selected, measured Jev artifact")
    parser.add_argument("--confirm-live", action="store_true",
                        help="required with --live-jev: authorizes paid prediction calls")
    parser.add_argument("--max-live-requests", type=int, default=50)
    args = parser.parse_args(argv)
    articles = load_articles_jsonl(args.articles) if args.articles else ()
    with ReviewStore(args.database, study_seed=args.study_seed, rolling_audit_rate=args.rolling_audit_rate,
                     final_audit_rate=args.final_audit_rate) as store:
        if articles:
            imported = store.import_articles(articles)
            Console().print(f"Imported {imported} new article(s).")
        if not store.articles():
            parser.error("supply --articles for a new review database")
        report_path = args.flywheel_report or args.database.parent / "reviewer-flywheel.json"
        report = load_flywheel_report(report_path)
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
            adapter = JevAdapter.from_environment(configuration=JevConfiguration(model="jev-latest"))
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

        run_review_session(store, flywheel_report=report, predict=live_predict)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
