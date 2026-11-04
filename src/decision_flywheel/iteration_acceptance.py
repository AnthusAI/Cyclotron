"""Whether a new iteration replaces the version going into the cycle, from one site's votes.

A young cyclotron has too few labels to prove anything, so an iteration is accepted unless it is significantly worse.
Once a site has enough votes, an iteration must show a significant improvement. Scores are paired on the same votes,
which must have been hidden from whatever produced the candidate.
"""

from collections.abc import Mapping, Sequence
from math import comb

from .vote_examples import Vote

SIGNIFICANCE = 0.05
STRICT_AFTER_VOTES = 200


def sign_test_p(first: int, second: int) -> float:
    """Two-sided exact sign test on the items where two paired versions differ."""
    differing = first + second
    if not differing:
        return 1.0
    tail = sum(comb(differing, count) for count in range(min(first, second) + 1))
    return min(1.0, 2 * tail / 2**differing)


def accept_iteration(
    votes: Sequence[Vote], incoming: Mapping[str, str], candidate: Mapping[str, str], *, total_votes: int
) -> dict:
    """The decision on ``candidate`` against ``incoming``, with the evidence and the rule that produced it."""
    if len({vote.output for vote in votes}) > 1:
        raise ValueError("votes count per site: compare one output's votes at a time")
    rows = [vote for vote in votes if vote.item_id in incoming and vote.item_id in candidate]
    candidate_right = sum(candidate[v.item_id] == v.label and incoming[v.item_id] != v.label for v in rows)
    incoming_right = sum(incoming[v.item_id] == v.label and candidate[v.item_id] != v.label for v in rows)
    p = sign_test_p(candidate_right, incoming_right)
    if total_votes < STRICT_AFTER_VOTES:
        phase = "accept-unless-significantly-worse"
        accepted = not (incoming_right > candidate_right and p < SIGNIFICANCE)
    else:
        phase = "require-improvement"
        accepted = candidate_right > incoming_right and p < SIGNIFICANCE
    return {
        "accepted": accepted,
        "phase": phase,
        "totalVotes": total_votes,
        "compared": len(rows),
        "candidateAgreed": sum(candidate[v.item_id] == v.label for v in rows),
        "candidateRight": candidate_right,
        "incomingRight": incoming_right,
        "p": p,
        "rule": {"test": "paired-sign-two-sided", "significance": SIGNIFICANCE, "strictAfterVotes": STRICT_AFTER_VOTES},
    }
