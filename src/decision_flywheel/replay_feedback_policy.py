"""Deterministic, pre-label feedback selection for operational replays."""
from dataclasses import dataclass, field
import hashlib
from typing import Mapping


@dataclass(frozen=True)
class FeedbackSelection:
    selected: bool
    propensity: float
    inclusion_propensity: float


@dataclass(frozen=True)
class FeedbackPhase:
    """A cycle-bounded, predicted-class sampling rule.

    ``end_cycle`` is inclusive; ``None`` means the final open-ended phase.
    Rates are keyed by the *issued prediction*, never the hidden human label.
    """
    end_cycle: int | None
    rates: Mapping[str, float]

    def __post_init__(self):
        if self.end_cycle is not None and (type(self.end_cycle) is not int or self.end_cycle < 1):
            raise ValueError('phase end_cycle must be a positive integer or None')
        if not self.rates or any(not isinstance(label,str) or not label or isinstance(rate,bool) or
                                 not isinstance(rate,(int,float)) or not 0 <= rate <= 1
                                 for label,rate in self.rates.items()):
            raise ValueError('phase rates must be non-empty label-to-probability mappings')

@dataclass(frozen=True)
class ReplayFeedbackPolicy:
    """Select labels using the issued prediction, never the hidden label."""
    mode: str = "all"
    seed: str = "replay-feedback-v1"
    def __post_init__(self):
        if self.mode not in {"all", "reject_half", "casual_ten_percent"}: raise ValueError("feedback mode must be all, reject_half, or casual_ten_percent")
        if not isinstance(self.seed, str) or not self.seed: raise ValueError("feedback selection seed must be non-empty")
    def select(self, *, item_id, predicted_label, negative_label, cycle_number=None, confidence=None):
        """Make the decision before the hidden human label is accessed."""
        if self.mode == "all": return FeedbackSelection(True, 1., 1.)
        if self.mode == "reject_half" and predicted_label != negative_label:
            return FeedbackSelection(False, 0., 0.)
        denominator = 2 if self.mode == "reject_half" else 10
        propensity = 1. / denominator
        selected = int.from_bytes(hashlib.sha256(f"{self.seed}:{item_id}".encode()).digest()[:8], "big") % denominator == 0
        return FeedbackSelection(selected, propensity if selected else 0., propensity)

    def selects(self, **kwargs):
        return self.select(**kwargs).selected
    def manifest(self, negative_label):
        return {"mode": self.mode, "seed": self.seed, "negative_label": negative_label, "selection_timing": "after prediction, before hidden label reveal"}


@dataclass(frozen=True)
class PhasedFeedbackPolicy:
    """Customer-onboarding feedback schedule with deterministic random sampling.

    The policy always chooses using only a cycle number, item ID, and the model's
    issued label.  A rate of one means everyone in that predicted class is asked;
    zero means nobody is asked.  The human label is unavailable until after this
    choice, preserving the operational replay firewall.
    """
    phases: tuple[FeedbackPhase, ...]
    seed: str = 'phased-feedback-v1'
    name: str = 'phased'

    def __post_init__(self):
        if not self.phases or not isinstance(self.seed,str) or not self.seed or not isinstance(self.name,str) or not self.name:
            raise ValueError('phased feedback requires phases, a seed, and a name')
        ends=[phase.end_cycle for phase in self.phases]
        if ends[-1] is not None or any(end is None for end in ends[:-1]):
            raise ValueError('only the final phase may be open-ended')
        if any(later <= earlier for earlier,later in zip(ends[:-2],ends[1:-1])):
            raise ValueError('finite phase boundaries must be strictly increasing')

    def _phase(self, cycle_number):
        if type(cycle_number) is not int or cycle_number < 1:
            raise ValueError('cycle_number must be a positive integer')
        return next(phase for phase in self.phases if phase.end_cycle is None or cycle_number <= phase.end_cycle)

    def select(self, *, item_id, predicted_label, negative_label, cycle_number=None, confidence=None):
        phase=self._phase(cycle_number)
        if predicted_label not in phase.rates:
            raise ValueError('each phase must specify the issued predicted label')
        probability=float(phase.rates[predicted_label])
        if probability in (0.,1.):
            selected=bool(probability)
        else:
            selected=int.from_bytes(hashlib.sha256(f'{self.seed}:{cycle_number}:{item_id}:{predicted_label}'.encode()).digest()[:8], 'big') / 2**64 < probability
        return FeedbackSelection(selected, probability if selected else 0., probability)

    def manifest(self, negative_label):
        start=1; encoded=[]
        for phase in self.phases:
            encoded.append({'start_cycle':start,'end_cycle':phase.end_cycle,
                            'predicted_label_rates':dict(sorted(phase.rates.items()))})
            if phase.end_cycle is not None: start=phase.end_cycle+1
        return {'mode':self.name,'seed':self.seed,'selection_timing':'after prediction, before hidden label reveal',
                'selection_basis':'issued predicted label and cycle number; never hidden human label',
                'phases':encoded}


def onboarding_all_then_half(*, seed='editorial-onboarding-a'):
    return PhasedFeedbackPolicy((FeedbackPhase(50,{'publish':1.,'reject':1.}),
                                 FeedbackPhase(None,{'publish':.5,'reject':.5})),seed,'first-50-all-then-half')


def onboarding_first_hundred_then_half(*, seed='editorial-onboarding-c'):
    return PhasedFeedbackPolicy((FeedbackPhase(100,{'publish':1.,'reject':1.}),
                                 FeedbackPhase(None,{'publish':.5,'reject':.5})),seed,'first-100-all-then-half')


def onboarding_publish_priority_taper(*, seed='editorial-onboarding-b'):
    return PhasedFeedbackPolicy((FeedbackPhase(50,{'publish':1.,'reject':.5}),
                                 FeedbackPhase(None,{'publish':.5,'reject':.25})),seed,'publish-priority-taper')


def _unit(seed, item_id):
    return int.from_bytes(hashlib.sha256(f'{seed}:{item_id}'.encode()).digest()[:8], 'big') / 2**64


@dataclass
class ConfidenceFeedbackPolicy:
    """Review a fixed share of items, chosen by the issued confidence or at random.

    ``least_confident`` reviews an item when its confidence is at or below the
    ``rate`` quantile of the previous ``window`` issued confidences (random at
    ``rate`` until 20 have been seen). ``mixed`` does the same at
    ``rate - audit_rate`` and audits the rest at random so that about
    ``audit_rate`` of all items are audited. ``random`` reviews ``rate`` of items
    by a seeded hash. The choice uses only the issued prediction and earlier
    confidences, never the hidden label. A selected low-confidence item has
    propensity one; a random or audit selection has its sampling probability.
    The confidence history lives in memory: a resumed replay starts it afresh.
    """
    rule: str
    rate: float = .25
    audit_rate: float = .05
    window: int = 100
    seed: str = 'confidence-feedback-v1'
    history: list = field(default_factory=list, repr=False)

    def __post_init__(self):
        if self.rule not in {'least_confident', 'random', 'mixed'}:
            raise ValueError('rule must be least_confident, random, or mixed')
        if not 0 < self.rate < 1 or not 0 < self.audit_rate < self.rate or type(self.window) is not int or self.window < 20:
            raise ValueError('rates must satisfy 0 < audit_rate < rate < 1 and the window must be at least 20')

    @property
    def name(self):
        return f'{self.rule}_{round(self.rate * 100)}'

    def _cut(self, share):
        if len(self.history) < 20:
            return None
        ordered = sorted(self.history)
        return ordered[max(0, min(len(ordered) - 1, int(share * len(ordered)) - 1))]

    def select(self, *, item_id, predicted_label, negative_label, cycle_number=None, confidence=None):
        if self.rule != 'random' and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))):
            raise ValueError('confidence-based selection needs the issued confidence')
        draw = _unit(self.seed, item_id)
        if self.rule == 'random':
            selection = FeedbackSelection(draw < self.rate, self.rate if draw < self.rate else 0., self.rate)
        else:
            share = self.rate if self.rule == 'least_confident' else self.rate - self.audit_rate
            cut = self._cut(share)
            if cut is None:
                selection = FeedbackSelection(draw < self.rate, self.rate if draw < self.rate else 0., self.rate)
            elif confidence <= cut:
                selection = FeedbackSelection(True, 1., 1.)
            elif self.rule == 'mixed':
                audit = self.audit_rate / (1 - share)
                selection = FeedbackSelection(draw < audit, audit if draw < audit else 0., audit)
            else:
                selection = FeedbackSelection(False, 0., 0.)
        if confidence is not None:
            self.history = [*self.history, float(confidence)][-self.window:]
        return selection

    def selects(self, **kwargs):
        return self.select(**kwargs).selected

    def manifest(self, negative_label):
        return {'mode': self.name, 'rule': self.rule, 'rate': self.rate,
                **({'audit_rate': self.audit_rate} if self.rule == 'mixed' else {}),
                'window': self.window if self.rule != 'random' else None, 'seed': self.seed,
                'negative_label': negative_label, 'selection_timing': 'after prediction, before hidden label reveal',
                'selection_basis': 'issued confidence against the previous window of issued confidences, or a seeded hash; never the hidden label'}


@dataclass
class MetricTaperPolicy:
    """Lower the review rate when reviewed outcomes show the cyclotron can be trusted.

    Reviews everything at first. At each ``window``-cycle boundary the rule reads
    only labels reviewers gave: the calibration error of the last ``ece_reviews``
    reviewed items, and the accuracy of reviewed items whose confidence was at or
    above ``threshold`` in the window just ended. A window passes when the error
    is below ``max_ece`` and that accuracy is at least ``target``. After
    ``passes_to_step`` passing windows in a row the rate steps down one level of
    ``levels``; one failing window steps it back up. Below full review, items are
    chosen at random at the current rate, so every reviewed item is also an audit
    and the estimates stay unbiased; the lowest level is the audit floor.
    """
    levels: tuple = (1., .5, .35, .25, .15)
    window: int = 100
    threshold: float = .8
    target: float = .9
    max_ece: float = .05
    ece_reviews: int = 200
    passes_to_step: int = 2
    min_approved: int = 10
    seed: str = 'metric-taper-v1'
    level: int = 0
    passes: int = 0
    reviews: list = field(default_factory=list, repr=False)
    log: list = field(default_factory=list, repr=False)

    def __post_init__(self):
        if not self.levels or self.levels[0] != 1. or any(b >= a for a, b in zip(self.levels, self.levels[1:])) or self.levels[-1] <= 0:
            raise ValueError('levels must start at full review and fall strictly to a positive audit floor')

    name = 'metric_taper'

    @property
    def rate(self):
        return self.levels[self.level]

    def _ece(self, rows):
        total, ece = len(rows), 0.
        for bucket in range(10):
            members = [r for r in rows if min(9, int(r['confidence'] * 10)) == bucket]
            if members:
                ece += len(members) / total * abs(sum(r['confidence'] for r in members) / len(members) - sum(r['right'] for r in members) / len(members))
        return ece

    def _close_window(self, cycle_number):
        recent = self.reviews[-self.ece_reviews:]
        window = [r for r in self.reviews if r['cycle'] > cycle_number - self.window]
        approved = [r for r in window if r['confidence'] >= self.threshold]
        ece = self._ece(recent) if recent else None
        accuracy = sum(r['right'] for r in approved) / len(approved) if len(approved) >= self.min_approved else None
        passed = ece is not None and accuracy is not None and ece < self.max_ece and accuracy >= self.target
        before = self.rate
        if passed:
            self.passes += 1
            if self.passes >= self.passes_to_step and self.level < len(self.levels) - 1:
                self.level += 1; self.passes = 0
        else:
            self.passes = 0
            if self.level > 0:
                self.level -= 1
        self.log.append({'cycle': cycle_number, 'ece_recent_reviews': ece, 'approved_accuracy': accuracy,
                         'approved_reviewed': len(approved), 'passed': passed, 'rate_before': before, 'rate_after': self.rate})

    def select(self, *, item_id, predicted_label, negative_label, cycle_number=None, confidence=None):
        if type(cycle_number) is not int or cycle_number < 1:
            raise ValueError('cycle_number must be a positive integer')
        if cycle_number > 1 and (cycle_number - 1) % self.window == 0 and (not self.log or self.log[-1]['cycle'] != cycle_number - 1):
            self._close_window(cycle_number - 1)
        rate = self.rate
        if rate >= 1.:
            return FeedbackSelection(True, 1., 1.)
        draw = _unit(f'{self.seed}:{cycle_number}', item_id)
        return FeedbackSelection(draw < rate, rate if draw < rate else 0., rate)

    def observe_review(self, *, cycle_number, label, predicted_label, confidence, propensity):
        self.reviews.append({'cycle': cycle_number, 'right': label == predicted_label, 'confidence': float(confidence)})

    def selects(self, **kwargs):
        return self.select(**kwargs).selected

    def manifest(self, negative_label):
        return {'mode': self.name, 'levels': list(self.levels), 'window': self.window, 'threshold': self.threshold,
                'target': self.target, 'max_ece': self.max_ece, 'ece_reviews': self.ece_reviews,
                'passes_to_step': self.passes_to_step, 'min_approved': self.min_approved, 'seed': self.seed,
                'negative_label': negative_label, 'selection_timing': 'after prediction, before hidden label reveal',
                'selection_basis': 'random at the current level; levels move only on reviewed labels at window boundaries',
                'rate_log': self.log}
