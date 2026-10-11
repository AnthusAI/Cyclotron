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

    def _latest(self, idea_id, exclude=frozenset(), context_version=None):
        """item_id -> correct, newest evaluation per item (across context versions, or only ``context_version``),
        minus ``exclude``."""
        sql, args = "SELECT item_id,correct FROM idea_evaluations WHERE idea_id=?", [idea_id]
        if context_version is not None:
            sql, args = sql + " AND context_version=?", [*args, context_version]
        rows = self.db.execute(sql + " ORDER BY evaluated_at, rowid", args)
        return {item: bool(c) for item, c in rows if item not in exclude}

    def items_evaluated(self, idea_id):
        return frozenset(self._latest(idea_id))

    def correctness_vector(self, idea_id, item_ids):
        """Per requested item: True/False, or None when the idea never saw it."""
        got = self._latest(idea_id)
        return [got.get(str(i)) for i in item_ids]

    def paired_comparison(self, a_idea, b_idea, exclude=frozenset(), context_version=None):
        """Candidate a versus b on shared items: gained = a right and b wrong; lost = the reverse."""
        a, b = self._latest(a_idea, exclude, context_version), self._latest(b_idea, exclude, context_version)
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

    def _idea_payload(self, idea_id):
        try:
            row = self.db.execute("SELECT payload FROM control_ideas WHERE id=?", (idea_id,)).fetchone()
        except Exception:
            row = None
        return json.loads(row[0]) if row else None

    def _idea_text(self, idea_id, width=120):
        idea = self._idea_payload(idea_id)
        if not idea:
            return ""
        return " ".join(str(idea.get("rationale") or idea.get("proposal") or "").split())[:width]

    def _idea_ids(self, context_version=None):
        sql, args = "SELECT DISTINCT idea_id FROM idea_evaluations", []
        if context_version is not None:
            sql, args = sql + " WHERE context_version=?", [context_version]
        return [i for (i,) in self.db.execute(sql, args)]

    def summary_for_optimizer(self, *, limit=10, incumbent_id=None, max_item_ids=5, exclude_items=frozenset(),
                              context_version=None, skip_ids=frozenset()):
        """Compact deterministic digest, best accuracy first. Item-id lists are capped and sorted.
        ``exclude_items`` hides items (for example a held-out reserve) from every count and list.
        ``context_version`` restricts the digest to one kind of evidence; ``skip_ids`` drops ids (old incumbents)."""
        ids = sorted(set(self._idea_ids(context_version)) - {incumbent_id} - set(skip_ids))
        rows = []
        for idea_id in ids:
            got = self._latest(idea_id, exclude_items, context_version)
            if not got:
                continue
            entry = {"idea_id": idea_id, "text": self._idea_text(idea_id), "items_tested": len(got),
                     "accuracy": round(sum(got.values()) / len(got), 4)}
            if incumbent_id:
                pair = self.paired_comparison(idea_id, incumbent_id, exclude_items, context_version)
                entry.update(fixed=pair["gained_items"][:max_item_ids], fixed_count=pair["gained"],
                             broke=pair["lost_items"][:max_item_ids], broke_count=pair["lost"])
            last = (self.verdicts(idea_id) or [None])[-1]
            entry["last_verdict"] = last and {"verdict": last["verdict"], "reason": last["reason"]}
            idea = self._idea_payload(idea_id) or {}
            if idea.get("operator"):  # lineage is shown only for ideas that have it
                entry.update(operator=idea["operator"], parents=[p[:8] for p in idea.get("parents", ())],
                             outcome=last["verdict"] if last else "untested")
            rows.append(entry)
        rows.sort(key=lambda r: (-r["accuracy"], -r["items_tested"], r["idea_id"]))
        return rows[:limit]

    def stubborn_items(self, min_ideas=3, exclude_items=frozenset(), context_version=None):
        """Items evaluated by at least ``min_ideas`` ideas that every one of them got wrong."""
        per_item = {}
        for idea_id in self._idea_ids(context_version):
            for item, ok in self._latest(idea_id, exclude_items, context_version).items():
                per_item.setdefault(item, []).append(ok)
        return sorted(i for i, marks in per_item.items() if len(marks) >= min_ideas and not any(marks))


    # -- lineage and operators (Stage 2c) ----------------------------------------------------------------------
    def lineage(self, idea_id):
        """The idea's operator, parents, novelty and verdict, then every ancestor reachable through ``parents``
        (nearest first, each once). Parents that are not stored ideas (the incumbent) appear as bare ids."""
        def describe(i, depth):
            idea = self._idea_payload(i) or {}
            last = (self.verdicts(i) or [None])[-1]
            return {"idea_id": i, "depth": depth, "operator": idea.get("operator"),
                    "parents": list(idea.get("parents", ())), "novelty": idea.get("novelty"),
                    "outcome": last["verdict"] if last else None}
        root = describe(idea_id, 0)
        seen, queue, ancestors = {idea_id}, [(p, 1) for p in root["parents"]], []
        while queue:
            parent, depth = queue.pop(0)
            if parent in seen:
                continue
            seen.add(parent)
            row = describe(parent, depth)
            ancestors.append(row)
            queue.extend((p, depth + 1) for p in row["parents"])
        return {**root, "ancestors": ancestors}

    def operator_outcomes(self):
        """Per operator: ideas proposed, ideas advanced (ever 'advanced' or 'promoted'), ideas promoted, and the mean
        forward net gain (fixed minus broke versus the incumbent of the time) over ideas whose verdicts recorded one;
        None when no idea of that operator has such evidence. Ideas without an operator are not counted."""
        try:
            rows = self.db.execute("SELECT payload FROM control_ideas ORDER BY id").fetchall()
        except Exception:
            return {}
        out = {}
        for (payload,) in rows:
            idea = json.loads(payload)
            if not idea.get("operator"):
                continue
            history = self.verdicts(idea["id"])
            seen = {v["verdict"] for v in history}
            nets = [v["evidence"]["net"] for v in history if isinstance(v["evidence"].get("net"), (int, float))]
            row = out.setdefault(idea["operator"], {"proposed": 0, "advanced": 0, "promoted": 0, "nets": []})
            row["proposed"] += 1
            row["advanced"] += bool(seen & {"advanced", "promoted"})
            row["promoted"] += "promoted" in seen
            if nets:
                row["nets"].append(nets[-1])  # the latest recorded look at this idea
        return {op: {"proposed": r["proposed"], "advanced": r["advanced"], "promoted": r["promoted"],
                     "mean_forward_net": round(sum(r["nets"]) / len(r["nets"]), 4) if r["nets"] else None}
                for op, r in sorted(out.items())}

    def _wrong_records(self, ids, exclude_items, context_version):
        """item -> {label, choices (wrong answers seen)} for items each given idea answered wrongly."""
        out = {}
        for idea_id in ids:
            sql = ("SELECT item_id,label,choice FROM idea_evaluations WHERE idea_id=? AND correct=0"
                   + (" AND context_version=?" if context_version is not None else "") + " ORDER BY evaluated_at, rowid")
            args = [idea_id] + ([context_version] if context_version is not None else [])
            for item, label, choice in self.db.execute(sql, args):
                if item not in exclude_items:
                    rec = out.setdefault(item, {"label": label, "choices": []})
                    rec["choices"].append(choice)
        return out

    def error_records(self, *, stubborn_min_ideas=3, incumbent_id=None, exclude_items=frozenset(), context_version=None,
                      recent=20):
        """Stories to aim at: first those every tried idea gets wrong (stubborn), then the incumbent's newest errors.
        Each row is {item_id, label, choice, kind}; ``choice`` is the wrong answer given."""
        rows, seen = [], set()
        stubborn = self.stubborn_items(stubborn_min_ideas, exclude_items, context_version)
        wrong = self._wrong_records(self._idea_ids(context_version), exclude_items, context_version)
        for item in stubborn:
            rec = wrong.get(item)
            if rec:
                seen.add(item)
                rows.append({"item_id": item, "label": rec["label"], "kind": "stubborn",
                             "choice": max(set(rec["choices"]), key=lambda c: (rec["choices"].count(c), str(c)))})
        if incumbent_id:
            sql = ("SELECT item_id,label,choice FROM idea_evaluations WHERE idea_id=? AND correct=0"
                   + (" AND context_version=?" if context_version is not None else "")
                   + " ORDER BY evaluated_at DESC, rowid DESC LIMIT ?")
            args = [incumbent_id] + ([context_version] if context_version is not None else []) + [recent]
            for item, label, choice in self.db.execute(sql, args):
                if item not in seen and item not in exclude_items:
                    seen.add(item)
                    rows.append({"item_id": item, "label": label, "choice": choice, "kind": "incumbent-error"})
        return rows

    def complementary_pair(self, *, incumbent_id=None, ids=None, context_version=None, min_items=10, min_fix=2,
                           skip_ids=frozenset(), exclude_pairs=frozenset()):
        """The pair of ideas (the incumbent may be one) that fix the most DIFFERENT stories.

        For ideas X and Y on the stories both were scored on: fixes_Y_not_X = Y right and X wrong, fixes_X_not_Y the
        reverse. A pair qualifies when both are at least ``min_fix`` and at least ``min_items`` stories are shared.
        Score = min(fixes_X_not_Y, fixes_Y_not_X); higher is better. Ties prefer more members with positive net
        versus the incumbent on shared stories, then more shared stories, then the lower ids. Returns the pair as a
        dict (``exclude_pairs`` holds frozensets already combined), or {"skipped": reason} when fewer than two ideas have ``min_items`` stories or no pair qualifies."""
        pool = {}
        for idea_id in sorted(set(self._idea_ids(context_version) if ids is None else ids) - set(skip_ids)):
            got = self._latest(idea_id, context_version=context_version)
            if len(got) >= min_items:
                pool[idea_id] = got
        if len(pool) < 2:
            return {"skipped": f"fewer than two eligible ideas ({len(pool)} with at least {min_items} evaluated stories)"}
        base = pool.get(incumbent_id) if incumbent_id else None

        def positive(idea_id):
            if base is None or idea_id == incumbent_id:
                return 0
            shared = set(pool[idea_id]) & set(base)
            return int(sum(pool[idea_id][i] and not base[i] for i in shared) > sum(base[i] and not pool[idea_id][i] for i in shared))
        best, best_key = None, None
        names = sorted(pool)
        for n, a in enumerate(names):
            for b in names[n + 1:]:
                shared = sorted(set(pool[a]) & set(pool[b]))
                if len(shared) < min_items or frozenset((a, b)) in exclude_pairs:
                    continue
                a_not_b = [i for i in shared if pool[a][i] and not pool[b][i]]
                b_not_a = [i for i in shared if pool[b][i] and not pool[a][i]]
                if len(a_not_b) < min_fix or len(b_not_a) < min_fix:
                    continue
                key = (min(len(a_not_b), len(b_not_a)), positive(a) + positive(b), len(shared))
                if best_key is None or key > best_key:
                    best_key = key
                    best = {"a": a, "b": b, "score": key[0], "shared": len(shared),
                            "fixes_a_not_b": a_not_b, "fixes_b_not_a": b_not_a}
        return best or {"skipped": f"no pair where each idea fixes at least {min_fix} stories the other gets wrong"}
