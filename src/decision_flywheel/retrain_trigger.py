"""Whether one site's new votes are worth a retrain.

A vote teaches in proportion to how wrong the live decision was: a confident mistake counts most, an explanation adds
to it, and a vote agreeing with the decision counts nothing, however many arrive. A changed vote is a new vote. The
requirement adapts: a retrain that gained little raises it, and one that gained a lot lowers it. Sites never share
evidence, because a vote only teaches its own output.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .classification_metrics import wilson_interval
from .vote_examples import Vote

STARTING_REQUIREMENT = 5.0
EXPLANATION_WEIGHT = 1.0
ADAPTATION = 1.5


@dataclass(frozen=True)
class LiveDecision:
    label: str
    confidence: float | None = None


def vote_weight(vote: Vote, decision: LiveDecision | None) -> float:
    """What one vote is worth as evidence; zero when the live decision already agrees or is unknown."""
    if decision is None or decision.label == vote.label:
        return 0.0
    return 1.0 + (decision.confidence or 0.0) + (EXPLANATION_WEIGHT if vote.comment.strip() else 0.0)


def new_votes(votes: Sequence[Vote], seen: Mapping[str, str]) -> tuple[Vote, ...]:
    """The votes the last iteration did not learn from: new items, and items whose vote has changed."""
    return tuple(vote for vote in votes if seen.get(vote.item_id) != vote.label)


def next_requirement(requirement: float, gain: int) -> float:
    """After a retrain: votes newly right minus votes newly wrong, on the scored votes, moves the bar."""
    if gain > 1:
        return requirement / ADAPTATION
    if gain < 1:
        return requirement * ADAPTATION
    return requirement


def retrain_due(
    votes: Sequence[Vote],
    decisions: Mapping[str, LiveDecision],
    *,
    seen: Mapping[str, str],
    requirement: float = STARTING_REQUIREMENT,
    claimed_accuracy: float | None = None,
) -> dict:
    """The decision for one output, with the evidence behind it so the state can be shown and audited."""
    if len({vote.output for vote in votes}) > 1:
        raise ValueError("votes count per site: judge one output's votes at a time")
    fresh = new_votes(votes, seen)
    scored = [vote for vote in fresh if vote.item_id in decisions]
    disagreements = [vote for vote in scored if decisions[vote.item_id].label != vote.label]
    evidence = sum(vote_weight(vote, decisions.get(vote.item_id)) for vote in fresh)
    upper = wilson_interval(len(scored) - len(disagreements), len(scored))[1] if scored else None
    below_claim = claimed_accuracy is not None and upper is not None and upper < claimed_accuracy
    enough = evidence >= requirement
    if not disagreements:
        reason = "no new vote disagrees with a live decision"
    elif below_claim:
        reason = "accuracy on new votes is confidently below what this version claimed"
    elif enough:
        reason = "enough evidence of mistakes has built up"
    else:
        reason = "waiting for more evidence of mistakes"
    return {
        "due": bool(disagreements) and (below_claim or enough),
        "reason": reason,
        "newVotes": len(fresh),
        "scored": len(scored),
        "disagreements": len(disagreements),
        "evidence": evidence,
        "requirement": requirement,
        "accuracyUpperBound": upper,
        "claimedAccuracy": claimed_accuracy,
    }
