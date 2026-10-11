"""Forward shadow evaluation of ideas (hypothesis portfolio, Stage 2b).

Screening ideas on the engine's small development partition cannot promote anything: a round sees a few dozen
stories, and the optimizer that wrote the idea has already seen them. Decision-model calls are cheap, so instead
every new story that arrives is also scored by a few candidate ideas (the shadow set) alongside the incumbent,
BEFORE its label is revealed. Those stories were never seen by any idea's author, so the evidence is clean, and
it grows by one paired result per idea per story with no extra labels.

Rules this module enforces:

* Forward only. An idea is scored only on stories whose sequence number is greater than the sequence number at
  which it was created (``created_seq``) and joined the shadow set (``joined_seq``). The sequence advances by one
  per processed story, so an idea is never scored on the story during which it was proposed or on any earlier one.
* Selection evidence may only come from stories whose label is revealed to the flywheel and that are not in the
  scoreboard (protected) partition. In the replay that means: the feedback policy selected the story for review and
  its role is training or development. Unreviewed stories and scoreboard stories are never scored by shadow ideas
  and never enter the ledger. Protected stories are never used for promotion.
* Same decision path. Each shadow call goes through ``wheel._answers`` (request byte ceiling, per-open request
  ceiling, answer cache, durable answer records), so ``wheel.requests`` stays truthful and cache hits are free.
* Alpha spending. An idea gets at most ``shadow_looks`` looks per incumbent, at the predetermined shared-item counts
  ``shadow_min_items * k``. A look clears only if its exact two-sided sign-test p-value is below
  ``alpha / shadow_looks`` and gained > lost. Bonferroni over a predetermined number of looks keeps the chance
  that a truly equal idea ever clears below ``alpha``, however the looks are correlated. (With ``k`` ideas
  in the shadow set at once the family-wise chance is up to ``k * alpha``; each idea is still held to ``alpha``.)
* Incumbent epochs. The incumbent is identified by its configuration fingerprint. Ledger rows record the incumbent's
  decision answer under that id, so a paired comparison only ever uses stories scored while that incumbent was
  active. After a promotion the old rows stay in the ledger, but they never pair with the new incumbent, and every
  remaining idea starts a fresh comparison (and fresh looks) on the stories that follow.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .flywheel import FittedClassifier, _json
from .hypothesis_ledger import HypothesisLedger, sign_test
from .idea_screening import CAPS, incumbent_id

SHADOW = "shadow"
SHADOW_CONTROLS = ("rubric", "example_ids")


def look_threshold(alpha, looks):
    """Per-look significance level under alpha spending (Bonferroni over the predetermined looks)."""
    return alpha / looks


def clears_look(gained, lost, alpha, looks):
    return gained > lost and sign_test(gained, lost) < look_threshold(alpha, looks)


@dataclass(frozen=True)
class ShadowCycle:
    """What was scored for one story, held until its label is known."""
    seq: int
    item_id: str
    incumbent_id: str
    incumbent_choice: str
    incumbent_confidence: float
    scored: tuple  # ((idea_id, choice, confidence), ...)


class ShadowEvaluator:
    def __init__(self, wheel):
        self.wheel, self.config = wheel, wheel.idea_screening
        self.ledger = HypothesisLedger(wheel.db)
        wheel.db.executescript("""
            CREATE TABLE IF NOT EXISTS shadow_set (idea_id TEXT PRIMARY KEY, control TEXT NOT NULL,
                joined_seq INTEGER NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shadow_scored (idea_id TEXT, item_id TEXT, seq INTEGER,
                PRIMARY KEY (idea_id, item_id));
            CREATE TABLE IF NOT EXISTS shadow_looks (idea_id TEXT, incumbent_id TEXT, look INTEGER, shared INTEGER,
                gained INTEGER, lost INTEGER, p_value REAL, cleared INTEGER, seq INTEGER,
                PRIMARY KEY (idea_id, incumbent_id, look));
            CREATE TABLE IF NOT EXISTS shadow_incumbents (incumbent_id TEXT PRIMARY KEY, rubric TEXT, first_seq INTEGER);
        """)

    # -- small state -----------------------------------------------------------------------------------------
    def _state(self, key, default=None):
        row = self.wheel.db.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set_state(self, key, value):
        with self.wheel.db:
            self.wheel.db.execute("INSERT OR REPLACE INTO runtime_state VALUES (?, ?)", (key, _json(value)))

    def seq(self):
        return self._state("shadow_seq", 0)

    def begin_cycle(self):
        """One tick per processed story; ideas created during tick k are judged only on ticks after k."""
        value = self.seq() + 1
        self._set_state("shadow_seq", value)
        return value

    def calls(self):
        return self.wheel.db.execute("SELECT count(*) FROM shadow_scored").fetchone()[0]

    def active(self):
        return [{"idea_id": i, "control": c, "joined_seq": j} for i, c, j in self.wheel.db.execute(
            "SELECT idea_id,control,joined_seq FROM shadow_set WHERE status='active' ORDER BY joined_seq,idea_id")]

    def _ideas(self):
        from .control_scheduler import ControlScheduler
        return {idea["id"]: idea for idea in ControlScheduler(self.wheel).ideas()}

    def _set_status(self, idea_id, status):
        with self.wheel.db:
            self.wheel.db.execute("UPDATE shadow_set SET status=? WHERE idea_id=?", (status, idea_id))

    def _changes_incumbent(self, idea, training):
        """The configuration this idea would run (incumbent plus its one change), or None if it changes nothing."""
        try:
            config = self.wheel.active.config.apply(idea["proposal"], training)
        except ValueError:
            return None
        control = idea["control"]
        return config if config.briefing_state()[control] != self.wheel.active.config.briefing_state()[control] \
            else None

    # -- the shadow set --------------------------------------------------------------------------------------
    def fill_slots(self, training):
        """Newest never-shadowed ideas take free slots. Returns the ids that joined."""
        free = self.config.shadow_ideas - len(self.active())
        if free <= 0:
            return []
        known = {i for (i,) in self.wheel.db.execute("SELECT idea_id FROM shadow_set")}
        waiting = sorted((i for i in self._ideas().values() if i["control"] in SHADOW_CONTROLS and i["id"] not in known),
                         key=lambda i: (-i.get("created_seq", -1), i["id"]))
        joined = []
        for idea in waiting:
            if len(joined) == free:
                break
            if self._changes_incumbent(idea, training) is None:
                continue
            seq = max(self.seq(), idea.get("created_seq", 0))
            with self.wheel.db:
                self.wheel.db.execute("INSERT INTO shadow_set VALUES (?,?,?,'active')", (idea["id"], idea["control"], seq))
            self.wheel._emit({"kind": "shadow-idea-added", "idea_id": idea["id"], "control": idea["control"],
                              "joined_seq": seq, "proposal": idea["proposal"]})
            joined.append(idea["id"])
        return joined

    def _evict(self, idea_id, reason, evidence):
        self._set_status(idea_id, "evicted")
        self.ledger.record_verdict(idea_id, "screened_out", reason, {**evidence, "stage": SHADOW})
        self.wheel._emit({"kind": "shadow-idea-evicted", "idea_id": idea_id, "reason": reason, **evidence})

    # -- scoring (before the label is revealed) --------------------------------------------------------------
    def _stop_reason(self, reserve):
        if self.calls() >= self.config.max_shadow_calls:
            return f"max_shadow_calls ({self.config.max_shadow_calls}) reached"
        if self.wheel.max_requests - self.wheel.requests <= reserve:
            return "the request ceiling is near; the remaining requests are kept for normal decisions"
        return None

    def _stopped(self, reason):
        """Log a stop once per distinct reason; shadow scoring resumes by itself if the reason goes away."""
        previous = self._state("shadow_stopped")
        if previous != reason:
            self._set_state("shadow_stopped", reason)
            if reason:
                self.wheel._emit({"kind": "shadow-scoring-stopped", "reason": reason, "shadow_calls": self.calls(),
                                  "requests": self.wheel.requests, "ceiling": self.wheel.max_requests})
        return reason is not None

    async def score_cycle(self, item, training, *, eligible, incumbent_choice, incumbent_confidence,
                          protected_ids=(), reserve=0, now=None):
        """Advance the sequence, then score every eligible shadow idea on ``item``.

        ``eligible`` must be true only for a story whose label will be revealed and that is not protected; the
        shadow ideas never score anything else. ``reserve`` is the number of requests to leave for normal work.
        The label is NOT an input: ``record`` receives it afterwards."""
        from .observability import RequestBudgetExhausted
        seq = self.begin_cycle()
        if not eligible or item.id in set(protected_ids):
            return None
        shadow = self.active()
        if not shadow:
            return None
        ideas, scored, now = self._ideas(), [], now or datetime.now(timezone.utc)
        for member in shadow:
            if seq <= member["joined_seq"]:
                continue  # forward only: never the story during which the idea was created, nor earlier ones
            if self._stopped(self._stop_reason(reserve)):
                break
            idea = ideas.get(member["idea_id"])
            config = self._changes_incumbent(idea, training) if idea else None
            if config is None:
                self._evict(member["idea_id"], "the idea no longer changes the incumbent or cannot be applied", {})
                continue
            with self.wheel.db:
                self.wheel.db.execute("INSERT OR IGNORE INTO shadow_scored VALUES (?,?,?)",
                                      (member["idea_id"], item.id, seq))
            try:
                batch = await self.wheel._answers(config, item, training, now)
            except RequestBudgetExhausted:
                self._stopped("the request ceiling was reached")
                break
            except Exception as error:  # a shadow failure must never block normal decisions
                self.wheel._emit({"kind": "shadow-scoring-failed", "idea_id": member["idea_id"], "item_id": item.id,
                                  "error_type": type(error).__name__})
                continue
            answer = batch.answers["decision"]
            confidence = answer.probabilities[answer.label] if answer.probabilities else answer.confidence
            scored.append((member["idea_id"], answer.label, confidence))
        if not scored:
            return None
        cycle = ShadowCycle(seq, item.id, incumbent_id(self.wheel), incumbent_choice, incumbent_confidence,
                            tuple(scored))
        # One event per story with every voter's choice (no label), so disagreement can be analysed later.
        self.wheel._emit({"kind": "portfolio-scored", "item_id": item.id, "seq": seq,
                          "incumbent": {"id": cycle.incumbent_id, "choice": incumbent_choice,
                                        "confidence": incumbent_confidence},
                          "ideas": [{"idea_id": i, "choice": c, "confidence": p} for i, c, p in scored]})
        return cycle

    def record(self, cycle, label):
        """The label is now known: store per-(idea, story) correctness, the incumbent's included."""
        if cycle is None:
            return
        with self.wheel.db:
            self.wheel.db.execute("INSERT OR IGNORE INTO shadow_incumbents VALUES (?,?,?)",
                                  (cycle.incumbent_id, self.wheel.active.config.rubric[:200], cycle.seq))
        for idea_id, choice, confidence in cycle.scored:
            self.ledger.record_evaluations(idea_id, [{"item_id": cycle.item_id, "correct": choice == label,
                "label": label, "choice": choice, "confidence": confidence}], SHADOW)
        self.ledger.record_evaluations(cycle.incumbent_id, [{"item_id": cycle.item_id,
            "correct": cycle.incumbent_choice == label, "label": label, "choice": cycle.incumbent_choice,
            "confidence": cycle.incumbent_confidence}], SHADOW)

    # -- looks, eviction, promotion --------------------------------------------------------------------------
    def _looks_taken(self, idea_id, incumbent):
        return self.wheel.db.execute("SELECT count(*) FROM shadow_looks WHERE idea_id=? AND incumbent_id=?",
                                     (idea_id, incumbent)).fetchone()[0]

    def _advanced_already(self, idea_id, incumbent):
        return any(v["verdict"] == "advanced" and v["evidence"].get("incumbent") == incumbent
                   for v in self.ledger.verdicts(idea_id))

    async def review(self, training, development, *, source):
        """Update every active idea against the CURRENT incumbent; promote at most one that clears a look.
        Returns the promoted idea id or None."""
        config, inc, seq = self.config, incumbent_id(self.wheel), self.seq()
        cleared = []
        for member in self.active():
            idea_id = member["idea_id"]
            pair = self.ledger.paired_comparison(idea_id, inc, context_version=SHADOW)
            n, gained, lost = pair["shared"], pair["gained"], pair["lost"]
            stats = {"shared": n, "gained": gained, "lost": lost, "net": gained - lost, "p_value": pair["p_value"],
                     "incumbent": inc}
            if n >= config.shadow_min_items and gained > lost and not self._advanced_already(idea_id, inc):
                self.ledger.record_verdict(idea_id, "advanced",
                    f"on {n} forward stories it fixed {gained} and broke {lost} versus the incumbent; "
                    "still collecting evidence", {**stats, "stage": SHADOW})
            if n >= config.shadow_evict_after and gained <= lost:
                self._evict(idea_id, f"on {n} forward stories it fixed {gained} and broke {lost} versus the "
                                     "incumbent, no advantage, so it left the shadow set", stats)
                continue
            taken = self._looks_taken(idea_id, inc)
            if taken < config.shadow_looks and n >= (taken + 1) * config.shadow_min_items:
                ok = clears_look(gained, lost, config.alpha, config.shadow_looks)
                taken += 1
                with self.wheel.db:
                    self.wheel.db.execute("INSERT INTO shadow_looks VALUES (?,?,?,?,?,?,?,?,?)",
                        (idea_id, inc, taken, n, gained, lost, pair["p_value"], int(ok), seq))
                self.wheel._emit({"kind": "shadow-look", "idea_id": idea_id, "look": taken, "cleared": ok,
                                  "threshold": look_threshold(config.alpha, config.shadow_looks), **stats})
                if ok:
                    cleared.append((-(gained - lost), pair["p_value"], idea_id, {**stats, "looks": taken}))
                elif taken >= config.shadow_looks:
                    self._evict(idea_id, f"used all {config.shadow_looks} looks without clearing the "
                                         f"alpha-spending threshold (fixed {gained}, broke {lost} on {n} stories)",
                                {**stats, "looks": taken})
        if not cleared:
            self.fill_slots(training)
            return None
        _, _, idea_id, stats = sorted(cleared)[0]
        await self._promote(idea_id, stats, training, development, source)
        return idea_id

    async def _promote(self, idea_id, stats, training, development, source):
        """Distinct from the fitted-trial path: the evidence is forward and paired, so the idea's change is
        applied directly as the new decision context. No head is fitted here; the usual classifier-retraining
        schedule refits it on the new context. Decision answers are what was compared, so that is what is promoted."""
        wheel, scheduler_ideas = self.wheel, self._ideas()
        idea = scheduler_ideas[idea_id]
        previous = wheel.active.config.briefing_state()
        config = wheel.active.config.apply(idea["proposal"], training)
        evidence = {**stats, "alpha": self.config.alpha, "looks_allowed": self.config.shadow_looks,
                    "threshold": look_threshold(self.config.alpha, self.config.shadow_looks), "stage": SHADOW}
        reason = (f"cleared forward shadow evaluation: on {stats['shared']} stories it fixed {stats['gained']} and broke "
                  f"{stats['lost']} versus the incumbent (p={stats['p_value']:.4f}, look {stats['looks']} of "
                  f"{self.config.shadow_looks}, threshold {evidence['threshold']:.4f})")
        wheel._emit({"kind": "idea-promoted-by-shadow", "idea_id": idea_id, "control": idea["control"],
                     "proposal": idea["proposal"], "evidence": evidence, "reason": reason, "source": source,
                     "previous": previous, "candidate": config.briefing_state()})
        wheel._activate(FittedClassifier(config, None, wheel._evidence(training), wheel._evidence(development),
                                         "evaluated"))
        wheel._emit({"kind": "promoted", "promoted": True, "promotion_path": "shadow-evaluation",
                     "idea_id": idea_id, "version": wheel.active.fingerprint, "reason": reason,
                     "selection": None, "shadow_evidence": evidence})
        self.ledger.record_verdict(idea_id, "promoted", reason, evidence)
        self._set_status(idea_id, "promoted")
        idea["attempts"].append({"feedback_fingerprint": f"shadow:{self.seq()}", "result": {
            "promoted": True, "promotion_path": "shadow-evaluation", "evidence": evidence}})
        from .control_scheduler import ControlScheduler
        ControlScheduler(wheel).save(idea)
        self.fill_slots(training)

    # -- what other parts read -------------------------------------------------------------------------------
    def snapshot(self):
        inc = incumbent_id(self.wheel)
        out = []
        for member in self.active():
            pair = self.ledger.paired_comparison(member["idea_id"], inc, context_version=SHADOW)
            out.append({**member, "shared": pair["shared"], "gained": pair["gained"], "lost": pair["lost"],
                        "looks": self._looks_taken(member["idea_id"], inc)})
        return {"seq": self.seq(), "calls": self.calls(), "incumbent": inc, "active": out}

    def optimizer_digest(self):
        """Each idea's forward record and the forward stories every scored voter got wrong. Everything here
        is a story that already arrived and had its label revealed; a story awaiting its label is never in it."""
        inc = incumbent_id(self.wheel)
        old = {i for (i,) in self.wheel.db.execute("SELECT incumbent_id FROM shadow_incumbents")}
        status = dict(self.wheel.db.execute("SELECT idea_id,status FROM shadow_set"))
        rows = self.ledger.summary_for_optimizer(limit=CAPS["ledger_ideas"], incumbent_id=inc,
                                                 max_item_ids=CAPS["item_ids_per_idea"], context_version=SHADOW,
                                                 skip_ids=old)
        for row in rows:
            row["shadow_status"] = status.get(row["idea_id"])
        return {"hypothesis_ledger": rows,
                "stubborn_items": self.ledger.stubborn_items(context_version=SHADOW)[:CAPS["stubborn_items"]]}


async def shadow_stage(wheel, proposals, training, development):
    """Rubric stage in shadow mode: save the proposals as ideas, give them free shadow slots, then look at the
    evidence already collected. Nothing is scored on the development partition."""
    from .control_scheduler import ControlScheduler
    control = "rubric"
    cycle = wheel.db.execute("SELECT count(*) FROM optimization_stages").fetchone()[0]
    ControlScheduler(wheel).admit_proposals(control, proposals, training, cycle)
    shadow = ShadowEvaluator(wheel)
    shadow.fill_slots(training)
    promoted = await shadow.review(training, development, source="rubric-trigger")
    snapshot = shadow.snapshot()
    wheel._emit({"kind": "shadow-stage-completed", "promoted_idea": promoted, **snapshot})
    return {"promoted": promoted is not None, "promoted_idea": promoted, "shadow": snapshot,
            "reason": ("an idea cleared forward shadow evaluation and is the new decision context" if promoted else
                       "no idea has cleared forward shadow evaluation yet; evidence keeps accumulating")}
