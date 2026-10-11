import asyncio
import hashlib
import random
import sqlite3
from types import SimpleNamespace

from decision_flywheel.hypothesis_ledger import HypothesisLedger
from decision_flywheel.idea_screen import holm, screen


class FakeScorer:
    """Idea 'good' is right with prob 0.9, every other idea (incl. the incumbent) with prob `base`."""

    def __init__(self, base=0.7, good=0.9, salt=0):
        self.base, self.good, self.salt, self.log = base, good, salt, []

    async def __call__(self, idea, item):
        self.log.append((idea, item))
        digest = hashlib.sha256(f"{self.salt}|{idea}|{item}".encode()).digest()
        p = self.good if idea == "good" else self.base
        return SimpleNamespace(choice="x", confidence=0.5, correct=digest[0] / 256 < p)


ITEMS = [f"item{i}" for i in range(400)]
IDEAS = ["good", "n1", "n2", "n3", "n4", "n5"]


def run(scorer, **kw):
    return asyncio.run(screen(kw.pop("candidates", IDEAS), "inc", kw.pop("items", ITEMS), scorer, **kw))


def test_better_candidate_advances_and_clears_reserve_gate():
    result = run(FakeScorer(base=0.6, good=0.95), seed=1)
    assert "good" in result.finalists and result.finalists[0] == "good"
    assert "good" in result.cleared
    assert result.paired["good"]["net"] > 0 and result.paired["good"]["p_adjusted"] < 0.05
    assert [s["stage"] for s in result.stages] == [1, 2, 3] and not result.truncated
    assert len(result.stages[0]["table"]) == 6 and result.stages[1]["scored"] == 4


def test_reserve_never_scored_during_screening_stages():
    scorer = FakeScorer()
    result = run(scorer, seed=3)
    assert result.reserve_size == 80
    stage_calls = sum(s["items"] * s["scored"] for s in result.stages)
    screened = {item for _, item in scorer.log[:stage_calls]}
    reserved = {item for _, item in scorer.log[stage_calls:]}
    assert not screened & reserved and len(reserved) == 80
    assert len(scorer.log) == result.calls == stage_calls + 80 * (len(result.finalists) + 1)


def test_deterministic_by_seed():
    a, b, c = (run(FakeScorer(), seed=s) for s in (5, 5, 6))
    assert (a.finalists, a.paired, a.calls) == (b.finalists, b.paired, b.calls)
    assert a.stages != c.stages


def test_noise_rarely_clears_the_gate():
    ids = ["a", "b", "c", "d", "e", "f"]
    items = [f"q{i}" for i in range(150)]
    false_positives = 0
    for seed in range(200):
        r = run(FakeScorer(base=0.7, good=0.7, salt=seed), candidates=ids, items=items, seed=seed,
                stages=((30, 0.5), (None, None)))
        false_positives += bool(r.cleared)
    assert false_positives / 200 <= 0.10


def test_budget_stops_cleanly_with_partial_result():
    scorer = FakeScorer()
    r = run(scorer, max_calls=400, seed=1)
    assert r.truncated and r.calls <= 400 and len(scorer.log) == r.calls
    assert r.cleared == [] and r.paired == {} and len(r.stages) == 1
    r2 = run(FakeScorer(), max_calls=1, seed=1)
    assert r2.truncated and r2.calls == 0 and r2.stages == []


def test_ledger_rows_and_verdicts_written():
    ledger = HypothesisLedger(sqlite3.connect(":memory:"))
    r = run(FakeScorer(base=0.6, good=0.95), seed=1, ledger=ledger)
    assert ledger.db.execute("SELECT COUNT(*) FROM idea_evaluations").fetchone()[0] == r.calls
    assert {e["verdict"] for i in IDEAS for e in ledger.verdicts(i)} >= {"screened_out", "advanced", "promoted"}
    assert ledger.verdicts("good")[-1]["verdict"] == "promoted"
    assert "Holm" in ledger.verdicts("good")[-1]["reason"]
    assert len(ledger.items_evaluated("inc")) == len(ITEMS)


def test_holm_adjustment():
    assert holm([0.01, 0.04, 0.03]) == [0.03, 0.06, 0.06]
    assert holm([0.5]) == [0.5]
