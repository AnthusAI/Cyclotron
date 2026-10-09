#!/usr/bin/env python3
"""Extend the 400-item Wikinews editorial corpus without changing its first 400 items.

The base corpus (``uplifting-editorial-wikinews-400-semantic-v2``) was curated
from the 2020-2023 annual files of ``malteos/wikinews`` and labelled one item
per request by ``gpt-4.1-mini-2025-04-14`` under the rubric below. Those years
hold only 88 further usable items, so the extension also draws on the adjacent
2016-2019 files of the same pinned revision.

The extension keeps the curation rule (a high-relevance share, then a topical
round-robin, then a seeded shuffle) and the label procedure (same model, same
prompt and schema, temperature 0, one request per item, no retry). An item
whose call fails, whose decision factor contradicts its label, or whose
evidence quote is not literally in the excerpt is not repaired: it is set aside
and the next candidate in the frozen order takes its place. The output file is
the base file's exact bytes followed by the new items, so earlier results stay
comparable.

Labels are simulated editorial judgements for a disclosed marketing demo, never
historical newsroom decisions. The corpus text is CC BY 2.5 and is not
committed to this public repository; the manifest is.

Without ``--confirm-live`` the script plans and prints its worst-case cost and
makes no request. ``OPENAI_API_KEY`` comes from the environment.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import UTC
import hashlib
import json
import os
from pathlib import Path
import random
import re
import urllib.error
import urllib.request

MODEL = "gpt-4.1-mini-2025-04-14"
DATASET = "malteos/wikinews"
REVISION = "b4c2ec3857fcac203c40b8d61586e934ed07c128"
LICENSE = "CC BY 2.5 (Wikinews policy; articles dated through 2024-12-15)"
LICENSE_URL = "https://en.wikinews.org/wiki/Wikinews:Copyright"
BASE_CORPUS = "uplifting-editorial-wikinews-400-semantic-v2"
BASE_SELECTION_SEED = "uplifting-editorial-semantic-v2"
EXTENSION_SEED = "uplifting-editorial-semantic-v2-extension-1"
BASE_HIGH_RELEVANCE_SHARE = 210 / 400
PRICE = {"input_usd_per_mtok": 0.40, "output_usd_per_mtok": 1.60,
         "source": "OpenAI published API price for gpt-4.1-mini (the base corpus manifest used the same rates)"}
MAX_INPUT_TOKENS = 4000
MAX_OUTPUT_TOKENS = 1000

# Verbatim from the base corpus's label program (semantic_v2_label.py).
RUBRIC = """You are performing a SIMULATED human editorial decision for a local marketing-demo corpus. This is not a claim about a real publisher's judgment.

Question: Publish this item in an uplifting, general-interest briefing, or reject it?

Publish only if the supplied article excerpt makes a grounded reason for hope, curiosity, connection, or public benefit CENTRAL: a concrete beneficial outcome, meaningful achievement, demonstrated understanding, rights/access improvement, conservation/welfare result, or constructive response whose outcome is established in the text.
Reject promises, speculation, hype, routine awards or scores, celebrity interest, ordinary sports wins, and stories where harm remains central or the benefit is merely incidental. Sports can publish only for inclusion, recovery, access, solidarity, or a substantive public benefit. Animals can publish only for welfare, conservation, or discovery. Do not infer facts beyond the excerpt.

Return only JSON matching the provided schema. The evidence_span must be an exact, short contiguous quote from the provided ARTICLE EXCERPT that supports the decision; it cannot be the title. rationale must state the deciding factor and be 8-40 words."""

POSITIVE_FACTORS = {"benefit", "achievement", "understanding", "rights_access", "conservation_welfare", "constructive_response"}
NEGATIVE_FACTORS = {"insufficient_evidence", "harm_dominant", "routine_or_hype"}
SCHEMA = {
    "name": "editorial_label",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "label": {"type": "string", "enum": ["publish", "reject"]},
            "rationale": {"type": "string", "minLength": 8, "maxLength": 300},
            "evidence_span": {"type": "string", "minLength": 3, "maxLength": 300},
            "factor": {"type": "string", "enum": ["benefit", "achievement", "understanding", "rights_access", "conservation_welfare",
                                                  "constructive_response", "insufficient_evidence", "harm_dominant", "routine_or_hype"]},
        },
        "required": ["label", "rationale", "evidence_span", "factor"],
    },
}

POSITIVE = re.compile(r"\b(rescue[ds]?|saved|protect(?:s|ed|ion)?|conservation|endangered|habitat|renewable|clean energy|restor(?:e|ed|ation)|discover(?:s|ed|y)|research(?:ers)? (?:find|develop)|scientists? (?:find|develop)|breakthrough|successful(?:ly)?|cure[ds]?|treatment|aid|relief|donat(?:e|ed|ion)|volunteer|community|opens?|access|legaliz(?:e|ed|ation)|rights|equality|inclusive|disabilit|women|education|scholarship|reunite[ds]?|return(?:s|ed) to|reintroduc(?:e|ed|tion)|agreement|peace|ceasefire)\b", re.I)
HARM = re.compile(r"\b(killed|dies?|dead|attack|war|shoot(?:ing)?|crash|earthquake|flood|arrest(?:ed)?|indict(?:ed)?|sentenced|outbreak|covid|disaster|explosion)\b", re.I)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identifier(row: dict) -> str:
    return f"wikinews:{row['wiki_page_id']}:{row['wiki_revision_id']}"


def compact(text: str, limit: int = 4500) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rsplit(" ", 1)[0] + ("…" if len(text) > limit else "")


def estimate_tokens(text: str) -> int:
    return (len(text) + 2) // 3 + 24


def topic(categories) -> str:
    c = set(categories or [])
    if c & {"Sports", "Football (soccer)", "Tennis", "Cricket", "Rugby", "Basketball"}:
        return "sports"
    if c & {"Animal rights", "Animals", "Elephants", "Cats", "Dogs", "Wildlife"}:
        return "animals"
    if "Science and technology" in c:
        return "science"
    if c & {"Environment", "Climate change", "Ecology"}:
        return "environment"
    if c & {"Politics and conflicts", "Law", "Human rights"}:
        return "civic"
    return "general"


def relevance(row: dict) -> int:
    excerpt = compact(row["cleaned_text"], 1800)
    score = 3 * len(POSITIVE.findall(row["title"])) + len(POSITIVE.findall(excerpt))
    score -= 2 * len(HARM.findall(row["title"])) + len(HARM.findall(excerpt)) // 3
    if topic(row.get("categories")) in {"science", "environment", "civic"}:
        score += 1
    return score


def eligible(row: dict) -> bool:
    return bool(row.get("title") and row.get("cleaned_text") and len(row["cleaned_text"].split()) >= 60)


def curation_order(rows, count: int, seed: str) -> list[dict]:
    """The base corpus's selection rule, generalised to ``count``, then every remaining row.

    The first ``count`` rows are the selection; rows after them are the frozen
    replacement order for items that cannot be labelled.
    """
    rng = random.Random(seed)
    unique, seen = [], set()
    for row in rows:
        if identifier(row) not in seen:
            unique.append(row)
            seen.add(identifier(row))
    buckets = defaultdict(list)
    for row in unique:
        buckets[topic(row.get("categories"))].append(row)
    ranked = sorted(unique, key=lambda r: (relevance(r), r["title"]), reverse=True)
    selected = [r for r in ranked if relevance(r) >= 2][:round(count * BASE_HIGH_RELEVANCE_SHARE)]
    used = {identifier(r) for r in selected}
    for values in buckets.values():
        rng.shuffle(values)
    names, index = sorted(buckets), 0
    while len(selected) < count and any(buckets.values()):
        name = names[index % len(names)]
        index += 1
        while buckets[name] and identifier(buckets[name][-1]) in used:
            buckets[name].pop()
        if buckets[name]:
            item = buckets[name].pop()
            selected.append(item)
            used.add(identifier(item))
    if len(selected) < count:
        raise ValueError(f"pool holds only {len(selected)} eligible items for {count}")
    rng.shuffle(selected)
    reserve = [r for r in ranked if identifier(r) not in used]
    return selected + reserve


def verified_span(result: dict, excerpt: str) -> bool:
    """Reject incoherent factor/label pairs; report whether the quote is literal."""
    if result["label"] == "publish" and result["factor"] in NEGATIVE_FACTORS:
        raise ValueError("incoherent publish factor")
    if result["label"] == "reject" and result["factor"] in POSITIVE_FACTORS:
        raise ValueError("incoherent reject factor")

    def normalized(value: str) -> str:
        return re.sub(r"\s+", " ", value.replace("“", '"').replace("”", '"').replace("’", "'")).strip()
    return normalized(result["evidence_span"]) in normalized(excerpt)


def record(row: dict, result: dict, sequence: int, excerpt: str) -> dict:
    return {
        "id": identifier(row), "sequence": sequence,
        "occurred_at": row["article_timestamp"].astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "title": row["title"], "text": excerpt, "categories": row.get("categories") or [],
        "source_url": row["url"].replace("http://", "https://"), "wikinews_page_id": row["wiki_page_id"],
        "wikinews_revision_id": row["wiki_revision_id"], "source_file": row["source_file"],
        "license": LICENSE, "license_url": LICENSE_URL, "label_type": "simulated_semantic_editorial",
        "simulated_label": result["label"], "simulated_rationale": result["rationale"],
        "evidence_span": result["evidence_span"], "decision_factor": result["factor"],
        "evidence_span_verified": True, "curation_relevance_score": relevance(row),
        "provenance": {"dataset": DATASET, "revision": REVISION, "selection_seed": EXTENSION_SEED, "model": MODEL},
    }


def extended_bytes(base: bytes, records) -> bytes:
    """The base corpus bytes, unchanged, followed by one sorted-key JSON line per new record."""
    if base and not base.endswith(b"\n"):
        raise ValueError("base corpus must end with a newline")
    return base + "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records).encode()


def request_label(api_key: str, title: str, excerpt: str) -> tuple[dict, dict]:
    payload = {
        "model": MODEL, "temperature": 0, "max_tokens": MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_schema", "json_schema": SCHEMA},
        "messages": [{"role": "system", "content": RUBRIC},
                     {"role": "user", "content": f"TITLE: {title}\n\nARTICLE EXCERPT:\n{excerpt}"}],
    }
    request = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                                     method="POST")
    with urllib.request.urlopen(request, timeout=90) as response:
        body = json.loads(response.read())
    return json.loads(body["choices"][0]["message"]["content"]), body.get("usage", {})


def load_pool(source_dir: Path, years) -> tuple[list[dict], list[dict]]:
    import pyarrow.parquet as pq
    rows, files = [], []
    for year in years:
        path = source_dir / f"en-{year}.parquet"
        files.append({"file": path.name, "sha256": sha256_bytes(path.read_bytes())})
        for row in pq.read_table(path).to_pylist():
            if eligible(row):
                row["source_file"] = path.name
                rows.append(row)
    return rows, files


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--base-corpus", type=Path, required=True)
    parser.add_argument("--base-sha256", required=True)
    parser.add_argument("--bootstrap-corpus", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--target", type=int, default=1200)
    parser.add_argument("--years", default="2016,2017,2018,2019,2020,2021,2022,2023")
    parser.add_argument("--max-live-calls", type=int, default=1000)
    parser.add_argument("--budget-usd", type=float, default=5.0)
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args(argv)

    base = args.base_corpus.read_bytes()
    if sha256_bytes(base) != args.base_sha256:
        raise SystemExit("base corpus hash mismatch")
    base_rows = [json.loads(line) for line in base.decode().splitlines()]
    excluded = {r["id"] for r in base_rows} | {json.loads(line)["id"] for line in args.bootstrap_corpus.read_text().splitlines()}
    needed = args.target - len(base_rows)
    years = tuple(part.strip() for part in args.years.split(",") if part.strip())
    pool, source_files = load_pool(args.source_dir, years)
    pool = [r for r in pool if identifier(r) not in excluded]
    order = curation_order(pool, needed, EXTENSION_SEED)
    rate = (MAX_INPUT_TOKENS * PRICE["input_usd_per_mtok"] + MAX_OUTPUT_TOKENS * PRICE["output_usd_per_mtok"]) / 1e6
    reserved = args.max_live_calls * rate
    print(json.dumps({"preflight": "ok", "base_items": len(base_rows), "needed": needed, "pool": len(pool),
                      "reserve_candidates": len(order) - needed, "max_live_calls": args.max_live_calls,
                      "worst_case_usd": round(reserved, 4), "budget_usd": args.budget_usd}))
    if reserved > args.budget_usd:
        raise SystemExit("worst-case reservation exceeds the budget")
    if not args.confirm_live:
        return 0
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    cache_path, ledger_path = args.out_dir / "response-cache.jsonl", args.out_dir / "usage-ledger.jsonl"
    cache = {}
    if cache_path.exists():
        for line in cache_path.read_text().splitlines():
            entry = json.loads(line)
            cache[entry["id"]] = entry
    calls = sum(1 for _ in ledger_path.open()) if ledger_path.exists() else 0
    accepted, set_aside = [], []
    with cache_path.open("a") as cache_file, ledger_path.open("a") as ledger:
        for row in order:
            if len(accepted) == needed:
                break
            key, excerpt = identifier(row), compact(row["cleaned_text"])
            if estimate_tokens(RUBRIC + row["title"] + excerpt) > MAX_INPUT_TOKENS:
                set_aside.append({"id": key, "reason": "input estimate over cap"})
                continue
            entry = cache.get(key)
            if entry is None:
                if calls >= args.max_live_calls:
                    raise SystemExit("live-call ceiling reached")
                calls += 1
                try:
                    result, usage = request_label(api_key, row["title"], excerpt)
                    entry = {"id": key, "result": result, "usage": usage}
                except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                    entry = {"id": key, "error": type(exc).__name__}
                ledger.write(json.dumps({"id": key, "call": calls, "usage": entry.get("usage"),
                                         "status": "error" if "error" in entry else "complete"}) + "\n")
                cache_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                ledger.flush(); cache_file.flush()
                cache[key] = entry
            if "error" in entry:
                set_aside.append({"id": key, "reason": f"call failed ({entry['error']}); not retried"})
                continue
            try:
                literal = verified_span(entry["result"], excerpt)
            except ValueError as exc:
                set_aside.append({"id": key, "reason": str(exc)})
                continue
            if not literal:
                set_aside.append({"id": key, "reason": "evidence quote is not literal in the excerpt"})
                continue
            accepted.append(record(row, entry["result"], len(base_rows) + len(accepted) + 1, excerpt))
    if len(accepted) != needed:
        raise SystemExit(f"labelled {len(accepted)} of {needed}; pool exhausted")

    output = args.out_dir / f"editorial-uplift-wikinews-{args.target}-semantic-v2.jsonl"
    data = extended_bytes(base, accepted)
    output.write_bytes(data)
    usage = Counter()
    for entry in cache.values():
        for field in ("prompt_tokens", "completion_tokens"):
            usage[field] += int((entry.get("usage") or {}).get(field, 0))
    cost = (usage["prompt_tokens"] * PRICE["input_usd_per_mtok"] + usage["completion_tokens"] * PRICE["output_usd_per_mtok"]) / 1e6
    all_rows = base_rows + accepted
    manifest = {
        "schema_version": 4,
        "corpus": f"uplifting-editorial-wikinews-{args.target}-semantic-v2",
        "file": output.name,
        "item_count": len(all_rows),
        "actual_count": len(all_rows),
        "sha256": sha256_bytes(data),
        "item_set_sha256": sha256_bytes(data),
        "first_400": {"corpus": BASE_CORPUS, "sha256": args.base_sha256, "byte_identical_prefix": True,
                      "lines": len(base_rows)},
        "label_disclosure": "simulated_semantic_editorial_labels",
        "dataset": DATASET, "dataset_revision": REVISION, "license": LICENSE, "license_url": LICENSE_URL,
        "source_files": source_files,
        "extension": {
            "items": len(accepted), "selection_seed": EXTENSION_SEED,
            "curation_rule": "base rule generalised: the top round(count x 210/400) items with relevance >= 2, then a topical round-robin over a seeded shuffle, then a seeded shuffle of the selection; bootstrap and base items excluded",
            "years": list(years),
            "label_procedure": "one request per item, temperature 0, strict JSON schema, no retry; an item with a failed call, an incoherent factor, or a non-literal evidence quote is set aside for the next candidate in the frozen order",
            "model": MODEL, "label_prompt": RUBRIC, "label_prompt_sha256": sha256_bytes(RUBRIC.encode()),
            "label_schema": SCHEMA,
            "set_aside": set_aside, "live_calls": calls,
            "usage": dict(usage), "cost_usd": round(cost, 6), "price_basis": PRICE,
            "offline_review": "none; the base corpus's offline span review and 20-item spot check covered the first 400 only",
            "label_counts": dict(Counter(r["simulated_label"] for r in accepted)),
            "topic_counts": dict(Counter(topic(r["categories"]) for r in accepted)),
            "source_file_counts": dict(Counter(r["source_file"] for r in accepted)),
        },
        "label_counts": dict(Counter(r["simulated_label"] for r in all_rows)),
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"items": len(all_rows), "sha256": manifest["sha256"], "labels": manifest["label_counts"],
                      "set_aside": len(set_aside), "live_calls": calls, "cost_usd": round(cost, 4)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
