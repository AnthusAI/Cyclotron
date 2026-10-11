import itertools
import json
import math
import sqlite3

import pytest

from decision_flywheel.hypothesis_ledger import HypothesisLedger, beta_interval, sign_test


def make():
    ticks = itertools.count()
    db = sqlite3.connect(":memory:")
    return db, HypothesisLedger(db, clock=lambda: f"2026-01-01T00:00:{next(ticks):04d}")


def rows(pattern, start=0):
    return [{"item_id": f"i{start + n}", "correct": bool(c), "choice": "x", "confidence": 0.5} for n, c in enumerate(pattern)]


def test_sign_test_matches_hand_computed_values():
    assert sign_test(3, 1) == pytest.approx(0.625)
    assert sign_test(20, 3) == pytest.approx(2 * sum(math.comb(23, i) for i in range(4)) / 2**23)
    assert 0.0004 < sign_test(20, 3) < 0.0006
    assert sign_test(0, 0) == 1.0 and sign_test(5, 5) == 1.0


def test_beta_interval_contains_mean_and_narrows():
    low, high = beta_interval(7, 10)
    assert low < 8 / 12 < high
    wide, narrow = beta_interval(7, 10), beta_interval(700, 1000)
    assert narrow[1] - narrow[0] < wide[1] - wide[0]
    assert beta_interval(0, 0)[0] == pytest.approx(0.025, abs=1e-6)


def test_rerecording_replaces_and_versions_coexist():
    db, ledger = make()
    ledger.record_evaluations("a", rows([1, 0]), "v1")
    ledger.record_evaluations("a", rows([0, 0]), "v1")
    assert db.execute("SELECT COUNT(*) FROM idea_evaluations").fetchone()[0] == 2
    assert ledger.correctness_vector("a", ["i0", "i1", "zz"]) == [False, False, None]
    ledger.record_evaluations("a", rows([1]), "v2")
    assert db.execute("SELECT COUNT(*) FROM idea_evaluations").fetchone()[0] == 3
    assert ledger.correctness_vector("a", ["i0"]) == [True]  # newest evaluation wins
    assert ledger.items_evaluated("a") == {"i0", "i1"}


def test_paired_comparison_and_verdict_history():
    _, ledger = make()
    ledger.record_evaluations("a", rows([1, 1, 1, 0, 1]), "v")
    ledger.record_evaluations("b", rows([0, 0, 0, 1, 1]), "v")
    pair = ledger.paired_comparison("a", "b")
    assert (pair["gained"], pair["lost"], pair["net"], pair["shared"]) == (3, 1, 2, 5)
    assert pair["p_value"] == pytest.approx(0.625)
    assert pair["gained_items"] == ["i0", "i1", "i2"] and pair["lost_items"] == ["i3"]
    ledger.record_verdict("a", "advanced", "looked good", {"n": 5})
    ledger.record_verdict("a", "retired", "stopped helping")
    assert [v["verdict"] for v in ledger.verdicts("a")] == ["advanced", "retired"]
    assert ledger.verdicts("a")[0]["evidence"] == {"n": 5}
    with pytest.raises(ValueError):
        ledger.record_verdict("a", "bogus", "x")


def test_running_score_and_since():
    _, ledger = make()
    ledger.record_evaluations("a", rows([1, 1, 0, 1]), "v")
    assert ledger.running_score("a")["mean"] == pytest.approx(4 / 6)
    assert ledger.running_score("none")["mean"] == 0.5
    cutoff = "2026-01-01T00:00:0099"
    ledger.clock = lambda: "2026-01-01T00:00:0100"
    ledger.record_evaluations("a", rows([0, 0], start=10), "v")
    late = ledger.running_score("a", since=cutoff)
    assert (late["n"], late["correct"]) == (2, 0) and late["high"] < 0.8


def test_revive_candidates_uses_only_labels_after_the_verdict():
    _, ledger = make()
    ledger.record_evaluations("a", rows([0] * 30), "v")
    ledger.record_verdict("a", "screened_out", "weak")
    ledger.record_evaluations("b", rows([0] * 30), "v")
    ledger.record_verdict("b", "screened_out", "weak")
    ledger.record_evaluations("c", rows([1] * 30), "v")
    ledger.record_verdict("c", "advanced", "fine")
    ledger.record_evaluations("a", rows([1] * 25 + [0] * 5, start=100), "v2")
    ledger.record_evaluations("b", rows([0] * 25, start=100), "v2")
    found = ledger.revive_candidates(min_items=20, min_mean=0.6)
    assert [f["idea_id"] for f in found] == ["a"]
    ledger.record_verdict("a", "revived", "new labels favour it")
    assert ledger.revive_candidates(min_items=20, min_mean=0.6) == []


def test_summary_is_deterministic_capped_and_uses_idea_text():
    db, ledger = make()
    db.execute("CREATE TABLE control_ideas (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    db.execute("INSERT INTO control_ideas VALUES ('a', ?)", (json.dumps({"rationale": "x" * 500}),))
    ledger.record_evaluations("inc", rows([0] * 10), "v")
    ledger.record_evaluations("a", rows([1] * 10), "v")
    ledger.record_evaluations("b", rows([1] * 5 + [0] * 5), "v")
    ledger.record_verdict("b", "screened_out", "half right")
    first = ledger.summary_for_optimizer(limit=5, incumbent_id="inc", max_item_ids=3)
    assert first == ledger.summary_for_optimizer(limit=5, incumbent_id="inc", max_item_ids=3)
    assert [e["idea_id"] for e in first] == ["a", "b"]
    assert len(first[0]["text"]) == 120 and len(first[0]["fixed"]) == 3 and first[0]["fixed_count"] == 10
    assert first[1]["last_verdict"] == {"verdict": "screened_out", "reason": "half right"}
    assert len(ledger.summary_for_optimizer(limit=1, incumbent_id="inc")) == 1
    json.dumps(first)


def test_stubborn_items():
    _, ledger = make()
    for name, pattern in {"a": [0, 1, 0], "b": [0, 0, 1], "c": [0, 1, 1], "d": [0, 1, 1]}.items():
        ledger.record_evaluations(name, rows(pattern), "v")
    assert ledger.stubborn_items(min_ideas=3) == ["i0"]
    assert ledger.stubborn_items(min_ideas=5) == []
