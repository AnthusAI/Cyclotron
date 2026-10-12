"""The recording catalog, driven by synthetic run directories (no live calls, no keys)."""
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("recordings", ROOT / "scripts/recordings.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["recordings"] = module
    spec.loader.exec_module(module)
    return module


R = _mod()
LABELS = ("publish", "reject")


def make_run(root, name="run", count=8, rubric="", activation=3, decision_ok=4, mode="all", reviewed=None, hide_unreviewed=False):
    """A synthetic recorded run (protocol, results, events) plus a relabeled corpus and manifest."""
    root.mkdir(parents=True, exist_ok=True)
    corpus = [{"id": f"item-{n}", "title": f"Story {n}", "source_url": f"https://example.org/{n}", "license": "CC BY 2.5",
               "license_url": "https://example.org/license", "simulated_label": "reject" if n % 4 else "publish"}
              for n in range(1, count + 1)]
    (root / "corpus.jsonl").write_text("".join(json.dumps(r) + "\n" for r in corpus))
    manifest = {"corpus": "fake-corpus", "actual_count": count, "label_disclosure": "simulated_semantic_editorial_labels",
                "dataset": "wikinews", "dataset_revision": "r1", "license": "CC BY 2.5", "license_url": "https://example.org/license",
                "operational_sha256": hashlib.sha256((root / "corpus.jsonl").read_bytes()).hexdigest(),
                "label_provenance": "Labels are simulated; two blind passes agreed."}
    (root / "manifest.json").write_text(json.dumps(manifest))
    run = root / name
    mode_name, mode = mode, run / mode
    mode.mkdir(parents=True)
    protocol = {"operational_items": count, "decision_model": "jev-1.13.0", "decision_provider": "jev",
                "optimizer_model": "gpt-6-luna", "baseline_rubric": rubric,
                "operational_sha256": manifest["operational_sha256"],
                "selection_policy": {"primary": "accuracy"}, "rubric_trigger": {"basis": "label_transitions", "every": 20, "max_attempts": None},
                "idea_screening": {"enabled": False}, "modes": [mode_name], "decision_cap_total": 100, "optimizer_cap_total": 5}
    (run / "protocol.json").write_text(json.dumps(protocol))
    metrics = {"count": 1, "accuracy": 1.0, "brier": 0.0, "calibration": {"ece": 0.0}, "per_class": {}, "confusion_matrix": {}}
    cycles, events = [], []
    for n, record in enumerate(corpus, start=1):
        actual = record["simulated_label"]
        other = LABELS[actual == "publish"]
        predicted = actual if n > 4 else other   # wrong in the first window, right after
        dm = actual if n <= decision_ok else other
        cycles.append({"cycle_number": n, "item_id": record["id"], "predicted_label": predicted, "human_label": None if hide_unreviewed and reviewed is not None and n not in reviewed else actual,
                       "feedback_selected": reviewed is None or n in reviewed, "feedback_propensity": 1.0, "role": "training", "metrics": metrics,
                       "outcomes": [], "requests": 1})
        minute = f"2026-10-09T{10 + n // 60:02d}:{n % 60:02d}"
        events += [
            {"kind": "cycle-started", "cycle_number": n, "created_at": f"{minute}:00+00:00"},
            {"kind": "prediction", "cycle_number": n, "cycle_item_id": record["id"], "label": predicted, "confidence": .8,
             "probabilities": {predicted: .8, LABELS[predicted == "publish"]: .2}, "decision_model_label": dm,
             "decision_model_probabilities": {dm: .7, LABELS[dm == "publish"]: .3}},
            {"kind": "decision-response", "cycle_number": n, "usage": {"input_tokens": 1000, "output_tokens": 10}},
            *([{"kind": "human-feedback", "action": "submitted", "cycle_number": n,
                "feedback": {"item_id": record["id"], "final_answer_value": actual, "edit_comment_value": "why"}}]
              if reviewed is None or n in reviewed else []),
            {"kind": "cycle-completed", "cycle_number": n, "created_at": f"{minute}:30+00:00"},
        ]
        if n == activation:
            events.append({"kind": "optimizer-response", "cycle_number": n, "usage": {"input_tokens": 2000, "output_tokens": 100}})
            events.append({"kind": "classifier-activated", "cycle_number": n})
    (mode / "results.json").write_text(json.dumps({
        "cycles": cycles, "disclosure": {"operational_items": count}, "protocol": {"task": "t" * 64},
        "feedback_policy": {"mode": mode_name}}))
    con = sqlite3.connect(mode / "runtime.sqlite3")
    con.execute("CREATE TABLE runtime_events (id INTEGER PRIMARY KEY, payload TEXT)")
    con.executemany("INSERT INTO runtime_events (payload) VALUES (?)", [(json.dumps(e),) for e in events])
    con.commit()
    con.close()
    return run, root / "manifest.json", root / "corpus.jsonl"


def cli(*argv):
    return R.main(list(argv))


def add(tmp_path, rid="first", catalog=None, run=None, **kw):
    kw.setdefault("extra", [])
    catalog = catalog or tmp_path / "catalog"
    if run is None:
        run = make_run(tmp_path / "src", name=kw.pop("name", "run"))
    run_dir, manifest, corpus = run
    args = ["add", str(run_dir), "--id", rid, "--title", "A title", "--summary", "A summary.",
            "--notice", "It starts blank.", "--notice", "The first rubric lifts it.", "--tag", "blank-start", "--tag", "jev",
            "--corpus-manifest", str(manifest), "--corpus", str(corpus), "--window-size", "4",
            "--engine-commit", "abc123", "--catalog", str(catalog), *kw.get("extra", [])]
    cli(*args)
    return catalog


def rec(catalog, rid="first"):
    return json.loads((Path(catalog) / rid / "recording.json").read_text())


def test_add_builds_a_valid_draft_with_results_cost_and_setup(tmp_path):
    catalog = add(tmp_path)
    r = rec(catalog)
    assert (r["schema"], r["status"], r["featured"], r["fixture"]) == ("cyclotron-recording/v1", "draft", False, "fixture.json")
    assert r["engine"] == {"commit": "abc123"} and r["tags"] == ["blank-start", "jev"]
    assert r["setup"]["starting_rubric"] == "blank" and "starting_rubric_text" not in r["setup"]
    assert r["setup"]["parameters"]["rubric_changes_every"] == 20 and r["setup"]["parameters"]["idea_screening"] is None
    corpus_sha = hashlib.sha256((tmp_path / "src" / "corpus.jsonl").read_bytes()).hexdigest()
    assert r["setup"]["corpus"] == {"id": "fake-corpus", "stories": 8, "sha256": corpus_sha,
                                    "label_provenance": "Labels are simulated; two blind passes agreed."}
    assert r["setup"]["window_size"] == 4 and r["setup"]["decision_provider"] == "jev"
    w = r["results"]["windows"]
    assert [(x["first_cycle"], x["last_cycle"]) for x in w] == [(1, 4), (5, 8)]
    assert (w[0]["cyclotron_accuracy"], w[1]["cyclotron_accuracy"]) == (0.0, 1.0)
    assert w[0]["decision_model_accuracy"] == 1.0 and w[1]["decision_model_accuracy"] == 0.0
    assert w[0]["majority_baseline"] == 0.75
    assert r["results"]["first_rubric_cycle"] == 3 and r["results"]["final_window_accuracy"] == 1.0
    assert r["results"]["post_warm_up_accuracy"] == 1.0
    assert r["cost"]["decision_requests"] == 8 and r["cost"]["optimizer_requests"] == 1
    assert r["cost"]["usd_total"] == pytest.approx(r["cost"]["decision_model_usd"] + r["cost"]["optimizer_usd"], abs=1e-6)
    assert "gpt-6-luna" in r["cost"]["price_basis"] and "jev" in r["cost"]["price_basis"]
    assert r["source"]["replicate_of"] is None and Path(r["source"]["run_dir"]).is_dir()
    fixture = catalog / "first" / "fixture.json"
    assert r["fixture_sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest()
    assert json.loads(fixture.read_text())["schema_version"] == 2


def test_add_refuses_a_duplicate_id_unless_replace(tmp_path):
    run = make_run(tmp_path / "src")
    catalog = add(tmp_path, run=run)
    with pytest.raises(SystemExit, match="already exists"):
        add(tmp_path, catalog=catalog, run=run)
    add(tmp_path, catalog=catalog, run=run, extra=["--replace", "--replicate-of", "set-a"])
    assert rec(catalog)["source"]["replicate_of"] == "set-a"
    with pytest.raises(SystemExit, match="kebab"):
        add(tmp_path, rid="Not Kebab", catalog=catalog, run=run)


def test_starting_rubric_blank_curated_and_custom(tmp_path):
    curated = R.study_baseline_rubric()
    assert R.starting_rubric({"baseline_rubric": ""}) == ("blank", None)
    assert R.starting_rubric({}) == ("blank", None)
    assert R.starting_rubric({"baseline_rubric": curated}) == ("curated", curated)
    assert R.starting_rubric({"baseline_rubric": "Publish good news."}) == ("custom", "Publish good news.")
    run = make_run(tmp_path / "src", rubric="Publish good news.")
    r = rec(add(tmp_path, run=run))
    assert r["setup"]["starting_rubric"] == "custom" and r["setup"]["starting_rubric_text"] == "Publish good news."


def test_add_validates_required_fields(tmp_path):
    with pytest.raises(SystemExit, match="notice must be a list|title is required"):
        run, manifest, corpus = make_run(tmp_path / "src")
        cli("add", str(run), "--id", "x", "--title", " ", "--summary", "s", "--corpus-manifest", str(manifest),
            "--corpus", str(corpus), "--window-size", "4", "--catalog", str(tmp_path / "c"))
    assert not (tmp_path / "c" / "x").exists()


def test_old_manifest_uses_the_fixture_disclosure(tmp_path):
    run, manifest, corpus = make_run(tmp_path / "src", count=100)
    old = json.loads(manifest.read_text())
    for key in ("corpus", "operational_sha256", "label_provenance"):
        old.pop(key)
    old["item_set_sha256"] = "0" * 64
    manifest.write_text(json.dumps(old))
    catalog = tmp_path / "c"
    cli("add", str(run), "--id", "old", "--title", "T", "--summary", "S", "--notice", "n", "--corpus-manifest", str(manifest),
        "--corpus", str(corpus), "--window-size", "50", "--catalog", str(catalog))
    r = rec(catalog, "old")
    fixture = json.loads((catalog / "old" / "fixture.json").read_text())
    assert r["setup"]["corpus"]["label_provenance"] == fixture["disclosure"]
    assert r["setup"]["corpus"]["id"] == "wikinews" and len(r["results"]["windows"]) == 2
    assert r["engine"] == {"commit": None}


def two_runs(tmp_path):
    """Two drafts in one catalog: 'first' and 'second' (a different rubric cadence and activation)."""
    catalog = add(tmp_path, "first", run=make_run(tmp_path / "a"))
    add(tmp_path, "second", catalog=catalog, run=make_run(tmp_path / "b", activation=5), extra=["--replicate-of", "set-a"])
    return catalog


def test_publish_validation_failures(tmp_path, capsys):
    catalog = add(tmp_path)
    path = catalog / "first" / "recording.json"
    good = json.loads(path.read_text())

    def broken(mutate, message):
        data = json.loads(json.dumps(good))
        mutate(data)
        path.write_text(json.dumps(data))
        with pytest.raises(SystemExit, match=message):
            cli("publish", "first", "--catalog", str(catalog))
        assert rec(catalog)["status"] == "draft"

    broken(lambda d: d.update(title=""), "title is required")
    broken(lambda d: d.update(summary=" "), "summary is required")
    broken(lambda d: d.update(notice=[]), "at least one notice")
    broken(lambda d: d["setup"]["corpus"].update(label_provenance=""), "label_provenance")
    broken(lambda d: d.pop("cost"), "cost.usd_total")
    broken(lambda d: d["results"]["windows"][0].update(majority_baseline=None), "majority_baseline")
    broken(lambda d: d.update(fixture_sha256="0" * 64), "does not match")
    broken(lambda d: d.pop("fixture_sha256"), "fixture_sha256")
    path.write_text(json.dumps(good))
    (catalog / "first" / "fixture.json").write_text("{}")
    with pytest.raises(SystemExit, match="does not match"):
        cli("publish", "first", "--catalog", str(catalog))


def test_publish_unpublish_retire_and_a_single_featured(tmp_path):
    catalog = two_runs(tmp_path)
    cli("publish", "first", "--featured", "--catalog", str(catalog))
    cli("publish", "second", "--catalog", str(catalog))
    assert (rec(catalog)["status"], rec(catalog)["featured"]) == ("published", True)
    assert rec(catalog, "second")["featured"] is False
    cli("publish", "second", "--featured", "--catalog", str(catalog))
    assert rec(catalog)["featured"] is False and rec(catalog, "second")["featured"] is True
    cli("unfeature", "second", "--catalog", str(catalog))
    assert rec(catalog, "second")["featured"] is False and rec(catalog, "second")["status"] == "published"
    cli("publish", "first", "--featured", "--catalog", str(catalog))
    cli("unpublish", "first", "--catalog", str(catalog))
    assert (rec(catalog)["status"], rec(catalog)["featured"]) == ("draft", False)
    cli("retire", "second", "--catalog", str(catalog))
    assert rec(catalog, "second")["status"] == "retired"
    with pytest.raises(SystemExit, match="retired"):
        cli("publish", "second", "--catalog", str(catalog))


def test_list_table_and_filters(tmp_path, capsys):
    catalog = two_runs(tmp_path)
    cli("publish", "second", "--catalog", str(catalog))
    capsys.readouterr()
    cli("list", "--catalog", str(catalog))
    out = capsys.readouterr().out.splitlines()
    assert out[0].split()[:3] == ["id", "status", "featured"] and len(out) == 3
    assert "100.0%" in out[1] and "cycle 3" in out[1] and "$" in out[1] and "draft" in out[1]
    assert "cycle 5" in out[2]
    cli("list", "--status", "published", "--catalog", str(catalog))
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2 and out[1].startswith("second")
    cli("list", "--tag", "nope", "--catalog", str(catalog))
    assert len(capsys.readouterr().out.splitlines()) == 1
    cli("show", "first", "--catalog", str(catalog))
    assert json.loads(capsys.readouterr().out)["id"] == "first"


def test_export_published_drops_source_orders_and_is_deterministic(tmp_path):
    catalog = two_runs(tmp_path)
    add(tmp_path, "third", catalog=catalog, run=make_run(tmp_path / "c"))
    for rid in ("first", "second", "third"):
        data = rec(catalog, rid)
        data["created"] = {"first": "2026-10-01", "second": "2026-10-05", "third": "2026-10-05"}[rid]
        (catalog / rid / "recording.json").write_text(json.dumps(data))
    cli("publish", "first", "--catalog", str(catalog))
    cli("publish", "second", "--catalog", str(catalog))
    cli("publish", "third", "--catalog", str(catalog))
    out = tmp_path / "site"
    cli("export-published", "--to", str(out), "--catalog", str(catalog))
    index = json.loads((out / "index.json").read_text())
    assert [r["id"] for r in index["recordings"]] == ["second", "third", "first"]   # newest first, then id
    assert set(index["recordings"][0]) == {"id", "title", "summary", "tags", "featured"}
    exported = json.loads((out / "second" / "recording.json").read_text())
    assert "source" not in exported and exported["id"] == "second"
    assert (out / "second" / "fixture.json").read_bytes() == (catalog / "second" / "fixture.json").read_bytes()
    assert (out / ".cyclotron-recordings").is_file()
    snapshot = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    cli("export-published", "--to", str(out), "--catalog", str(catalog))
    assert snapshot == {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    cli("publish", "first", "--featured", "--catalog", str(catalog))
    cli("export-published", "--to", str(out), "--catalog", str(catalog))
    assert [r["id"] for r in json.loads((out / "index.json").read_text())["recordings"]][0] == "first"
    assert (catalog / "second" / "recording.json").read_text().count('"source"') == 1   # local copy keeps it


def test_export_removes_stale_only_in_a_marked_directory(tmp_path):
    catalog = two_runs(tmp_path)
    cli("publish", "first", "--catalog", str(catalog))
    cli("publish", "second", "--catalog", str(catalog))
    out = tmp_path / "site"
    cli("export-published", "--to", str(out), "--catalog", str(catalog))
    (out / "notes").mkdir()
    (out / "notes" / "keep.txt").write_text("x")
    cli("unpublish", "second", "--catalog", str(catalog))
    cli("export-published", "--to", str(out), "--catalog", str(catalog))
    assert not (out / "second").exists() and (out / "first").is_dir()
    assert (out / "notes" / "keep.txt").is_file()          # not a recording directory
    # An unmarked directory is never cleaned, even of recording-shaped directories.
    other = tmp_path / "other"
    (other / "old").mkdir(parents=True)
    (other / "old" / "recording.json").write_text("{}")
    cli("export-published", "--to", str(other), "--catalog", str(catalog))
    assert (other / "old" / "recording.json").is_file() and (other / "first").is_dir()
    assert (other / ".cyclotron-recordings").is_file()


def test_compare_shows_only_differing_setup_and_window_accuracy(tmp_path, capsys):
    catalog = add(tmp_path, "first", run=make_run(tmp_path / "a"))
    other = make_run(tmp_path / "b", rubric="Publish good news.")
    protocol = json.loads((other[0] / "protocol.json").read_text())
    protocol["rubric_trigger"]["every"] = 40
    (other[0] / "protocol.json").write_text(json.dumps(protocol))
    add(tmp_path, "second", catalog=catalog, run=other)
    capsys.readouterr()
    cli("compare", "first", "second", "--catalog", str(catalog), "--force")
    out = capsys.readouterr().out
    assert "NOT THE SAME BENCHMARK" in out
    setup_part, windows_part = out.split("Window accuracy")
    assert "parameters.rubric_changes_every" in setup_part and "starting_rubric " in setup_part
    assert "decision_model " not in setup_part and "optimizer_model" not in setup_part
    assert "1-4" in windows_part and "5-8" in windows_part and "0.0% / 100.0% / 75.0%" in windows_part
    cli("compare", "first", "first", "--catalog", str(catalog))
    assert "(none)" in capsys.readouterr().out


def test_add_records_the_review_policy_for_full_and_partial_review(tmp_path):
    full = rec(add(tmp_path, "full", run=make_run(tmp_path / "a")), "full")["setup"]["review"]
    assert full == {"policy": "all", "summary": "Every story is reviewed.", "stories_reviewed": 8, "stories_total": 8}
    run = make_run(tmp_path / "b", mode="casual_ten_percent", reviewed={2})
    casual = rec(add(tmp_path, "casual", run=run), "casual")["setup"]["review"]
    assert casual == {"policy": "casual_ten_percent", "summary": "One story in ten is reviewed.",
                      "stories_reviewed": 1, "stories_total": 8}
    run = make_run(tmp_path / "c", mode="reject_half", reviewed={1, 2, 5})
    half = rec(add(tmp_path, "half", run=run), "half")["setup"]["review"]
    assert half["policy"] == "reject_half" and half["stories_reviewed"] == 3
    assert half["summary"] == "Every story the model calls publish is reviewed, and half of the stories it calls reject."
    run = make_run(tmp_path / "d", mode="odd_mode", reviewed={1})
    other = rec(add(tmp_path, "other", run=run), "other")["setup"]["review"]
    assert other["policy"] == "odd_mode" and other["summary"].strip() and other["stories_reviewed"] == 1


def test_review_is_optional_but_validated(tmp_path):
    catalog = add(tmp_path)
    r = rec(catalog)
    assert R.validate_recording(r) == []
    old = json.loads(json.dumps(r))
    del old["setup"]["review"]
    assert R.validate_recording(old) == []
    for bad in ("all", {"policy": "all"}, {"policy": "", "summary": "x", "stories_reviewed": 1, "stories_total": 2},
                {"policy": "all", "summary": "x", "stories_reviewed": 3, "stories_total": 2},
                {"policy": "all", "summary": "x", "stories_reviewed": "1", "stories_total": 2}):
        broken = json.loads(json.dumps(r))
        broken["setup"]["review"] = bad
        assert any("setup.review" in p for p in R.validate_recording(broken)), bad


def test_list_shows_the_policy_and_old_recordings_read_as_all(tmp_path, capsys):
    catalog = add(tmp_path, "full", run=make_run(tmp_path / "a"))
    add(tmp_path, "casual", catalog=catalog, run=make_run(tmp_path / "b", mode="casual_ten_percent", reviewed={2}))
    data = rec(catalog, "full")
    del data["setup"]["review"]
    (catalog / "full" / "recording.json").write_text(json.dumps(data))
    assert R.review_of(data)["policy"] == "all" and R.review_of(data)["stories_reviewed"] == 8
    capsys.readouterr()
    cli("list", "--catalog", str(catalog))
    out = capsys.readouterr().out.splitlines()
    assert "reviewed" in out[0]
    rows = {line.split()[0]: line for line in out[1:]}
    assert "casual_ten_percent 1/8" in rows["casual"] and " all " in rows["full"]


def test_compare_shows_review_policy_as_a_differing_setting(tmp_path, capsys):
    catalog = add(tmp_path, "first", run=make_run(tmp_path / "a"))
    add(tmp_path, "second", catalog=catalog, run=make_run(tmp_path / "b", mode="reject_half", reviewed={1, 2, 5}))
    capsys.readouterr()
    cli("compare", "first", "second", "--catalog", str(catalog), "--force")
    setup_part = capsys.readouterr().out.split("Window accuracy")[0]
    assert "review.policy" in setup_part and "reject_half" in setup_part and "stories_reviewed" not in setup_part


def test_export_keeps_the_review_block(tmp_path):
    catalog = add(tmp_path, "casual", run=make_run(tmp_path / "a", mode="casual_ten_percent", reviewed={2}))
    cli("publish", "casual", "--catalog", str(catalog))
    out = tmp_path / "site"
    cli("export-published", "--to", str(out), "--catalog", str(catalog))
    exported = json.loads((out / "casual" / "recording.json").read_text())
    assert exported["setup"]["review"]["policy"] == "casual_ten_percent"
    assert exported["setup"]["review"]["stories_reviewed"] == 1


def test_partial_review_baseline_comes_from_all_stories_not_the_reviewed_ones(tmp_path):
    # Reviewed stories 1, 2 and 5 are all "reject"; the corpus has one "publish" in each window of four.
    run = make_run(tmp_path / "p", mode="reject_half", reviewed={1, 2, 5}, hide_unreviewed=True)
    results = rec(add(tmp_path, "partial", run=run), "partial")["results"]
    assert [w["majority_baseline"] for w in results["windows"]] == [0.75, 0.75]
    assert [w["cyclotron_accuracy"] for w in results["windows"]] == [0.0, 1.0]
    full = rec(add(tmp_path, "full", run=make_run(tmp_path / "f")), "full")["results"]
    assert [w["majority_baseline"] for w in full["windows"]] == [0.75, 0.75]


def test_onboarding_policy_sentences_hyphenated_and_underscored(tmp_path):
    expected = {
        "first-50-all-then-half": "Every story for the first 50 stories, then half of the stories.",
        "first_100_all_then_half": "Every story for the first 100 stories, then half of the stories.",
        "publish-priority-taper": "For the first 50 stories, every story the model calls publish and half of those it "
                                  "calls reject; after that, half of the publishes and a quarter of the rejects.",
        "first-100-all-then-half": "Every story for the first 100 stories, then half of the stories.",
        "publish_priority_taper": "For the first 50 stories, every story the model calls publish and half of those it "
                                  "calls reject; after that, half of the publishes and a quarter of the rejects.",
        "first_50_all_then_half": "Every story for the first 50 stories, then half of the stories.",
    }
    for name, sentence in expected.items():
        assert R.review_summary(name) == sentence
    assert R.review_summary("odd-mode") is None
    run = make_run(tmp_path / "t", mode="publish-priority-taper", reviewed={1, 2})
    review = rec(add(tmp_path, "taper", run=run), "taper")["setup"]["review"]
    assert review["summary"] == expected["publish-priority-taper"]


# ---------------------------------------------------------------- comparison-ready recordings

def fx(cycles):
    return {"scenarios": {"run": {"cycles": cycles}}}


def cyc(n, gold, final, raw, story=None):
    other = lambda x: "reject" if x == "publish" else "publish"
    return {"n": n, "actual": gold, "predicted": final, "probabilities": {final: .9, other(final): .1},
            "decision_model_probabilities": {raw: .6, other(raw): .4}, "feedback_revealed": True,
            "article": {"id": story or f"s{n}"}}


def test_publish_metrics_hand_computed():
    # gold: P P P R R R ; final: P P R P R R -> tp2 fp1 fn1 ; raw: P R R R R P -> tp1 fp1 fn2
    gold = "publish publish publish reject reject reject".split()
    final = "publish publish reject publish reject reject".split()
    raw = "publish reject reject reject reject publish".split()
    rows = R.prediction_rows(fx([cyc(i + 1, g, f, r) for i, (g, f, r) in enumerate(zip(gold, final, raw))]))
    assert R.publish_metrics(rows, "final_label") == {"precision": 0.667, "recall": 0.667, "f1": 0.667}
    assert R.publish_metrics(rows, "raw_label") == {"precision": 0.5, "recall": 0.333, "f1": 0.4}
    assert R.accuracy(rows, "final_label") == 0.667 and R.accuracy(rows, "raw_label") == 0.5
    none = R.prediction_rows(fx([cyc(1, "reject", "reject", "reject")]))
    assert R.publish_metrics(none, "final_label") == {"precision": None, "recall": None, "f1": None}


def test_mcnemar_exact_matches_hand_computation():
    assert R.mcnemar_exact(0, 0) == 1.0
    assert R.mcnemar_exact(5, 5) == 1.0
    assert R.mcnemar_exact(0, 5) == pytest.approx(2 / 32)          # 2 * (1/32)
    assert R.mcnemar_exact(2, 8) == pytest.approx(2 * 56 / 1024)   # n=10, tail = 1+10+45 = 56


def test_fingerprint_is_stable_and_ignores_the_decision_model():
    setup = {"corpus": {"id": "c", "sha256": "ab" * 32}, "window_size": 4, "review": {"policy": "all"},
             "parameters": {"rubric_changes_every": 20, "rubric_trigger_basis": "x", "max_rubric_optimizations": None},
             "optimizer_model": "o", "objective": "accuracy", "starting_rubric": "blank",
             "decision_model": "jev", "decision_model_detail": {"name": "jev", "revision": "1"}}
    base = R.compute_benchmark(setup, 8)
    assert base == R.compute_benchmark(json.loads(json.dumps(setup)), 8) and base["id"] == "c-" + base["fingerprint"][:8]
    other_model = dict(setup, decision_model="kev", decision_model_detail={"name": "kev", "revision": "2"})
    assert R.compute_benchmark(other_model, 8)["fingerprint"] == base["fingerprint"]
    changes = [("window_size", 5), ("review", {"policy": "reject_half"}), ("optimizer_model", "p"), ("objective", "f1"),
               ("starting_rubric", "curated"), ("corpus", {"id": "c", "sha256": "cd" * 32}),
               ("parameters", {"rubric_changes_every": 40, "rubric_trigger_basis": "x", "max_rubric_optimizations": None})]
    for key, value in changes:
        assert R.compute_benchmark(dict(setup, **{key: value}), 8)["fingerprint"] != base["fingerprint"], key
    assert R.compute_benchmark(setup, 4)["fingerprint"] != base["fingerprint"]


def test_add_writes_identity_benchmark_predictions_and_classification_results(tmp_path):
    run = make_run(tmp_path / "src")
    run_dir = run[0]
    (run_dir.parent / (run_dir.name + ".card.json")).write_text(json.dumps({"argv": ["--decision-revision", "r7"]}))
    catalog = add(tmp_path, run=run)
    r = rec(catalog)
    assert r["setup"]["decision_model_detail"] == {"name": "jev-1.13.0", "revision": "r7", "provider": "jev", "hosting": "hosted"}
    assert r["benchmark"]["id"].startswith("fake-corpus-") and r["benchmark"]["comparable"] is True
    rows = R.read_predictions(catalog / "first" / "predictions.jsonl")
    assert len(rows) == 8 and rows[0]["id"] == "item-1" and set(rows[0]) == {"n", "id", "gold", "raw_label", "raw_probs", "final_label", "final_probs", "reviewed"}
    assert r["predictions_sha256"] == hashlib.sha256((catalog / "first" / "predictions.jsonl").read_bytes()).hexdigest()
    assert r["results"]["windows"][0]["final_publish"] == {"precision": 0.0, "recall": 0.0, "f1": None}  # all four inverted; f1 undefined at 0/0
    assert r["results"]["raw_accuracy"] == 0.5 and r["results"]["final_accuracy"] == 0.5
    assert json.loads((catalog / "first" / "fixture.json").read_text())["scenarios"]["run"]["cycles"][0]["article"]["id"] == "item-1"
    cli("publish", "first", "--catalog", str(catalog))
    cli("export-published", "--to", str(tmp_path / "site"), "--catalog", str(catalog))
    assert (tmp_path / "site" / "first" / "predictions.jsonl").is_file()
    (catalog / "first" / "predictions.jsonl").write_text("tampered\n")
    with pytest.raises(SystemExit, match="predictions_sha256"):
        cli("export-published", "--to", str(tmp_path / "site2"), "--catalog", str(catalog))


def test_compare_refuses_other_benchmarks_and_runs_paired_tests_on_the_same_one(tmp_path, capsys):
    catalog = add(tmp_path, "first", run=make_run(tmp_path / "a"))
    add(tmp_path, "second", catalog=catalog, run=make_run(tmp_path / "b", decision_ok=8))
    add(tmp_path, "third", catalog=catalog, run=make_run(tmp_path / "c", mode="reject_half"))
    capsys.readouterr()
    with pytest.raises(SystemExit, match="review_policy"):
        cli("compare", "first", "third", "--catalog", str(catalog))
    assert cli("compare", "first", "second", "--catalog", str(catalog), "--disagreements", "3") == 0
    out = capsys.readouterr().out
    assert "exact McNemar" in out and "final decisions: 8 scored" in out and "raw decision-model answers: 8 scored" in out
    assert "only first right 0, only second right 4" in out and "p = 0.1250" in out   # raw: 2*(1/16)
    assert "final accuracy" in out and "final publish recall" in out


def test_upgrade_backfills_an_old_recording_idempotently(tmp_path):
    catalog = add(tmp_path)
    path = catalog / "first" / "recording.json"
    fixture_path = catalog / "first" / "fixture.json"
    fixture = json.loads(fixture_path.read_text())
    for c in fixture["scenarios"]["run"]["cycles"]:
        del c["article"]["id"]
    fixture_path.write_text(json.dumps(fixture))
    old = rec(catalog)
    for key in ("benchmark", "predictions_sha256"):
        del old[key]
    del old["setup"]["decision_model_detail"], old["setup"]["corpus"]["sha256"]
    for w in old["results"]["windows"]:
        del w["final_publish"], w["raw_publish"]
    for key in ("final_publish", "raw_publish", "final_accuracy", "raw_accuracy"):
        del old["results"][key]
    old["fixture_sha256"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(old))
    (catalog / "first" / "predictions.jsonl").unlink()
    assert R.validate_recording(old) == []
    cli("list", "--catalog", str(catalog))
    before = fixture_path.read_bytes()
    cli("upgrade", "first", "--catalog", str(catalog))
    first = path.read_bytes()
    new = rec(catalog)
    assert new["benchmark"]["comparable"] is True and new["setup"]["corpus"]["sha256"]
    assert R.read_predictions(catalog / "first" / "predictions.jsonl")[0]["id"] == "item-1"
    for key in ("status", "notice", "fixture_sha256", "cost"):
        assert new[key] == old[key]
    assert [w["cyclotron_accuracy"] for w in new["results"]["windows"]] == [w["cyclotron_accuracy"] for w in old["results"]["windows"]]
    cli("upgrade", "first", "--catalog", str(catalog))
    assert path.read_bytes() == first and fixture_path.read_bytes() == before


def test_upgrade_marks_a_recording_not_comparable_when_ids_are_unrecoverable(tmp_path):
    catalog = add(tmp_path)
    fixture_path = catalog / "first" / "fixture.json"
    fixture = json.loads(fixture_path.read_text())
    for c in fixture["scenarios"]["run"]["cycles"]:
        del c["article"]["id"]
    fixture_path.write_text(json.dumps(fixture))
    r = rec(catalog)
    r["source"]["run_dir"] = str(tmp_path / "gone")
    (catalog / "first" / "recording.json").write_text(json.dumps(r))
    cli("upgrade", "first", "--catalog", str(catalog))
    b = rec(catalog)["benchmark"]
    assert b["comparable"] is False and "ids" in b["reason"]
