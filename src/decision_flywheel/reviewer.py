"""A local Rich terminal for collecting careful article-inclusion feedback.

This is intentionally a human-review surface, not a model runner.  It displays
only source metadata and records human events through :mod:`reviewer_store`.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from .reviewer_predictor import LabeledArticle, ReviewerPrediction, predict_article
from .reviewer_store import Article, ReviewStore


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


def _summary_table(summary: dict[str, int]) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="bold cyan")
    table.add_column(justify="right")
    for label, key in (("Articles", "articles"), ("Include", "include"), ("Exclude", "exclude"),
                       ("Skipped", "skip"), ("Remaining", "unreviewed")):
        table.add_row(label, str(summary[key]))
    return table


def _article_panel(article: Article, prediction: ReviewerPrediction) -> Panel:
    header = Text(article.title, style="bold white")
    metadata = Text(f"Submitted {article.submitted_at}  •  {' · '.join(article.categories)}", style="cyan")
    authors = Text(f"Authors: {article.authors}", style="cyan")
    publication = Text(f"Published as: {article.journal_ref}\n", style="cyan") if article.journal_ref else Text()
    label = "INCLUDE" if prediction.label == "include" else "EXCLUDE"
    prediction_text = Text.assemble(
        "Current system prediction: ",
        (label, "bold green" if prediction.label == "include" else "bold red"),
        f" — {prediction.confidence:.0%} confidence\n",
        ("Cold-start prior" if prediction.kind == "cold_start_prior" else "Local learning baseline"),
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


def run_review_session(store: ReviewStore, console: Console | None = None) -> None:
    """Run the review loop and preserve every prediction shown before a vote."""
    console = console or Console()
    console.print("[bold]Knowledge-base article reviewer[/bold]")
    console.print("Your choices and each displayed prediction are logged locally.\n")
    while True:
        article = store.next_unreviewed()
        console.clear()
        console.print(_summary_table(store.summary()))
        if article is None:
            console.print(Panel("There are no unreviewed articles in this batch.", border_style="green"))
            return
        labels = tuple(LabeledArticle(store.article(label.article_id), label.label) for label in store.learning_labels())
        prediction = predict_article(article, labels)
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
    args = parser.parse_args(argv)
    articles = load_articles_jsonl(args.articles) if args.articles else ()
    with ReviewStore(args.database, study_seed=args.study_seed, rolling_audit_rate=args.rolling_audit_rate,
                     final_audit_rate=args.final_audit_rate) as store:
        if articles:
            imported = store.import_articles(articles)
            Console().print(f"Imported {imported} new article(s).")
        if not store.articles():
            parser.error("supply --articles for a new review database")
        run_review_session(store)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
