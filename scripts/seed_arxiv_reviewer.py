#!/usr/bin/env python3
"""Create a small, local review batch from a pinned Hugging Face arXiv snapshot.

The snapshot is far too large to download as a first-review batch.  This script
draws deterministic chunks through the Hub dataset-server, filters them to
recent computer-science abstracts, and records the exact Hub revision and query
parameters beside the local JSONL.  It does not commit or redistribute papers.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import random
import time
from typing import Any

import requests
from huggingface_hub import HfApi


DATASET = "librarian-bots/arxiv-metadata-snapshot"
ROWS_URL = "https://datasets-server.huggingface.co/rows"


def _rows(offset: int) -> list[dict[str, Any]]:
    """Fetch one bounded chunk, tolerating a brief dataset-server interruption."""
    last_error: requests.RequestException | None = None
    for attempt in range(3):
        try:
            response = requests.get(ROWS_URL, params={"dataset": DATASET, "config": "default", "split": "train",
                                                       "offset": offset, "length": 100}, timeout=60)
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("rows", [])
            if not isinstance(rows, list):
                raise RuntimeError("the Hub dataset-server returned an invalid rows payload")
            return rows
        except requests.RequestException as error:
            last_error = error
            if attempt < 2:
                time.sleep(.5 * (attempt + 1))
    raise RuntimeError("the Hub dataset-server was unavailable after three bounded attempts") from last_error


def _created(row: dict[str, Any]) -> str | None:
    versions = row.get("versions")
    if not isinstance(versions, list) or not versions or not isinstance(versions[0], dict):
        return None
    value = versions[0].get("created")
    if not isinstance(value, str) or len(value) < 12:
        return None
    # RFC 2822 dates end with a four-digit year; this avoids a date-parser dependency.
    year = value[-12:-8]
    months = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
              "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}
    parts = value.replace(",", "").split()
    if len(parts) < 4 or parts[2] not in months or not parts[1].isdigit() or not parts[3].isdigit():
        return None
    return f"{parts[3]}-{months[parts[2]]}-{int(parts[1]):02d}"


def _article(row: dict[str, Any], earliest: str) -> dict[str, Any] | None:
    identifier, title, abstract, categories, authors, journal_ref = (
        row.get(name) for name in ("id", "title", "abstract", "categories", "authors", "journal-ref")
    )
    submitted = _created(row)
    if (not all(isinstance(value, str) and value.strip() for value in (identifier, title, abstract, categories))
            or submitted is None or submitted < earliest):
        return None
    cs_categories = [value for value in categories.split() if value.startswith("cs.")]
    if not cs_categories:
        return None
    return {"id": f"arxiv:{identifier}", "title": " ".join(title.split()),
            "abstract": " ".join(abstract.split()), "submitted_at": submitted, "categories": cs_categories,
            "authors": " ".join(authors.split()) if isinstance(authors, str) and authors.strip() else "Not provided",
            "journal_ref": " ".join(journal_ref.split()) if isinstance(journal_ref, str) and journal_ref.strip() else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sample recent CS abstracts from the public Hugging Face arXiv snapshot")
    parser.add_argument("--output", type=Path, required=True, help="local JSONL review batch")
    parser.add_argument("--limit", type=int, default=250)
    parser.add_argument("--from-date", default="2025-01-01", help="inclusive ISO date")
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--max-chunks", type=int, default=200, help="bounded Hub dataset-server requests")
    args = parser.parse_args(argv)
    if args.limit < 1 or args.max_chunks < 1:
        parser.error("--limit and --max-chunks must be positive")
    try:
        date.fromisoformat(args.from_date)
    except ValueError:
        parser.error("--from-date must be an ISO date")
    info = HfApi().dataset_info(DATASET)
    split = next((part for part in (info.card_data.get("dataset_info", {}).get("splits", []) if info.card_data else [])
                  if part.get("name") == "train"), None)
    if not split or not isinstance(split.get("num_examples"), int):
        raise RuntimeError("the Hub dataset card did not provide the train split size")
    total = split["num_examples"]
    selected: dict[str, dict[str, Any]] = {}
    offsets: list[int] = []
    randomizer = random.Random(args.seed)
    for _ in range(args.max_chunks):
        if len(selected) >= args.limit:
            break
        offset = randomizer.randrange(0, total - 100)
        offsets.append(offset)
        for wrapped in _rows(offset):
            if isinstance(wrapped, dict) and isinstance(wrapped.get("row"), dict):
                article = _article(wrapped["row"], args.from_date)
                if article is not None:
                    selected[article["id"]] = article
                    if len(selected) >= args.limit:
                        break
    if len(selected) < args.limit:
        raise RuntimeError(f"only found {len(selected)} eligible records within {args.max_chunks} bounded chunks")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                                    for row in list(sorted(selected.values(), key=lambda row: row["id"]))[:args.limit]),
                           encoding="utf-8")
    manifest = {"dataset": DATASET, "revision": info.sha, "split": "train", "source": "Hub dataset-server rows API",
                "selection": {"limit": args.limit, "from_date": args.from_date, "seed": args.seed,
                              "chunk_length": 100, "offsets": offsets}, "output": args.output.name}
    args.output.with_suffix(args.output.suffix + ".manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.limit} article records and a pinned source manifest to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
