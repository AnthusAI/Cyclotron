"""Long-lived evidence about many ideas: per-item outcomes plus an append-only verdict history.

Ideas themselves stay in ``control_ideas``; this ledger only references their ids.
"""
import json
from datetime import datetime, timezone
from math import exp, lgamma, log

from .flywheel import _json

VERDICTS = ("screened_out", "advanced", "promoted", "retired", "revived")


def sign_test(gained, lost):
    """Exact two-sided sign-test p-value for discordant pair counts."""
    n = gained + lost
    if n == 0:
        return 1.0
    k = min(gained, lost)
    tail = sum(exp(lgamma(n + 1) - lgamma(i + 1) - lgamma(n - i + 1) - n * log(2)) for i in range(k + 1))
    return min(1.0, 2 * tail)


def beta_interval(correct, total, level=0.95):
    """Equal-tailed credible interval of the Beta(1+correct, 1+wrong) posterior (exact, no scipy)."""
    a, b = correct + 1, total - correct + 1
    m = a + b - 1

    def cdf(x):  # I_x(a, b) = P(Binomial(a+b-1, x) >= a) for integer a, b
        if x <= 0 or x >= 1:
            return float(x >= 1)
        return sum(exp(lgamma(m + 1) - lgamma(i + 1) - lgamma(m - i + 1) + i * log(x) + (m - i) * log(1 - x))
                   for i in range(a, m + 1))

    def quantile(q):
        lo, hi = 0.0, 1.0
        for _ in range(50):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if cdf(mid) < q else (lo, mid)
        return (lo + hi) / 2

    return quantile((1 - level) / 2), quantile(1 - (1 - level) / 2)


def _now():
    return datetime.now(timezone.utc).isoformat()


class HypothesisLedger:
    def __init__(self, db, *, clock=_now):
        self.db, self.clock = db, clock
        db.executescript("""
            CREATE TABLE IF NOT EXISTS idea_evaluations (idea_id TEXT, item_id TEXT, correct INTEGER, label TEXT,
                choice TEXT, confidence REAL, context_version TEXT, evaluated_at TEXT,
                PRIMARY KEY (idea_id, item_id, context_version));
            CREATE TABLE IF NOT EXISTS idea_verdicts (idea_id TEXT, at TEXT, verdict TEXT, reason TEXT, evidence TEXT);
        """)

    def record_evaluations(self, idea_id, rows, context_version):
        """Rows are dicts with item_id and correct (optionally label, choice, confidence). Same key replaces."""
        at = self.clock()
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO idea_evaluations VALUES (?,?,?,?,?,?,?,?)",
                [(idea_id, str(r["item_id"]), int(bool(r["correct"])), r.get("label"), r.get("choice"),
                  r.get("confidence"), context_version, at) for r in rows])

    def record_verdict(self, idea_id, verdict, reason, evidence=None):
        if verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}")
        with self.db:
            self.db.execute("INSERT INTO idea_verdicts VALUES (?,?,?,?,?)",
                            (idea_id, self.clock(), verdict, reason, _json(evidence or {})))

    def verdicts(self, idea_id):
        return [{"at": at, "verdict": v, "reason": r, "evidence": json.loads(e or "{}")} for at, v, r, e in
                self.db.execute("SELECT at,verdict,reason,evidence FROM idea_verdicts WHERE idea_id=? ORDER BY rowid",
                                (idea_id,))]

    def _latest(self, idea_id, exclude=frozenset()):
        """item_id -> correct, newest evaluation per item (across context versions), minus ``exclude``."""
        rows = self.db.execute("SELECT item_id,correct FROM idea_evaluations WHERE idea_id=? "
                               "ORDER BY evaluated_at, rowid", (idea_id,))
        return {item: bool(c) for item, c in rows if item not in exclude}

    def items_evaluated(self, idea_id):
        return frozenset(self._latest(idea_id))

    def correctness_vector(self, idea_id, item_ids):
        """Per requested item: True/False, or None when the idea never saw it."""
        got = self._latest(idea_id)
        return [got.get(str(i)) for i in item_ids]

    def paired_comparison(self, a_idea, b_idea, exclude=frozenset()):
        """Candidate a versus b on shared items: gained = a right and b wrong; lost = the reverse."""
        a, b = self._latest(a_idea, exclude), self._latest(b_idea, exclude)
        shared = sorted(set(a) & set(b))
        gained = [i for i in shared if a[i] and not b[i]]
        lost = [i for i in shared if b[i] and not a[i]]
        return {"shared": len(shared), "gained": len(gained), "lost": len(lost), "net": len(gained) - len(lost),
                "p_value": sign_test(len(gained), len(lost)), "gained_items": gained, "lost_items": lost}

    def running_score(self, idea_id, since=None, *, strict=False):
        """Beta(1,1) posterior over accuracy, optionally only evaluations at/after (or strictly after) ``since``."""
        sql, args = "SELECT correct FROM idea_evaluations WHERE idea_id=?", [idea_id]
        if since is not None:
            sql, args = sql + (" AND evaluated_at>?" if strict else " AND evaluated_at>=?"), [*args, since]
        marks = [c for (c,) in self.db.execute(sql, args)]
        n, k = len(marks), sum(marks)
        low, high = beta_interval(k, n)
        return {"n": n, "correct": k, "mean": (k + 1) / (n + 2), "low": low, "high": high}

    def revive_candidates(self, *, min_items=20, min_mean=0.6):
        """Screened-out ideas whose evaluations since that verdict look promising."""
        out = []
        for (idea_id,) in self.db.execute("SELECT DISTINCT idea_id FROM idea_verdicts ORDER BY idea_id").fetchall():
            history = self.verdicts(idea_id)
            if history[-1]["verdict"] != "screened_out":
                continue
            since = history[-1]["at"]
            fresh = self.running_score(idea_id, since, strict=True)
            if fresh["n"] >= min_items and fresh["mean"] >= min_mean:
                out.append({"idea_id": idea_id, **fresh})
        return sorted(out, key=lambda r: (-r["mean"], r["idea_id"]))

    def _idea_text(self, idea_id, width=120):
        try:
            row = self.db.execute("SELECT payload FROM control_ideas WHERE id=?", (idea_id,)).fetchone()
        except Exception:
            row = None
        if not row:
            return ""
        idea = json.loads(row[0])
        return " ".join(str(idea.get("rationale") or idea.get("proposal") or "").split())[:width]

    def summary_for_optimizer(self, *, limit=10, incumbent_id=None, max_item_ids=5, exclude_items=frozenset()):
        """Compact deterministic digest, best accuracy first. Item-id lists are capped and sorted.
        ``exclude_items`` hides items (for example a held-out reserve) from every count and list."""
        ids = sorted({i for (i,) in self.db.execute("SELECT DISTINCT idea_id FROM idea_evaluations")} - {incumbent_id})
        rows = []
        for idea_id in ids:
            got = self._latest(idea_id, exclude_items)
            if not got:
                continue
            entry = {"idea_id": idea_id, "text": self._idea_text(idea_id), "items_tested": len(got),
                     "accuracy": round(sum(got.values()) / len(got), 4)}
            if incumbent_id:
                pair = self.paired_comparison(idea_id, incumbent_id, exclude_items)
                entry.update(fixed=pair["gained_items"][:max_item_ids], fixed_count=pair["gained"],
                             broke=pair["lost_items"][:max_item_ids], broke_count=pair["lost"])
            last = (self.verdicts(idea_id) or [None])[-1]
            entry["last_verdict"] = last and {"verdict": last["verdict"], "reason": last["reason"]}
            rows.append(entry)
        rows.sort(key=lambda r: (-r["accuracy"], -r["items_tested"], r["idea_id"]))
        return rows[:limit]

    def stubborn_items(self, min_ideas=3, exclude_items=frozenset()):
        """Items evaluated by at least ``min_ideas`` ideas that every one of them got wrong."""
        per_item = {}
        for (idea_id,) in self.db.execute("SELECT DISTINCT idea_id FROM idea_evaluations").fetchall():
            for item, ok in self._latest(idea_id, exclude_items).items():
                per_item.setdefault(item, []).append(ok)
        return sorted(i for i, marks in per_item.items() if len(marks) >= min_ideas and not any(marks))
