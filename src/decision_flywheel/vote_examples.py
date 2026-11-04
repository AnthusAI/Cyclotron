"""Editors' votes as labelled examples: one site's votes, split before their labels are read.

A vote says what an editor thought of one output's decision, so it only ever teaches that output. The split comes
from the item's identity alone, so evaluation votes stay disjoint from the examples and the choice cannot favour a
label. Editors choose what they vote on, so a score on these votes is agreement with editors, not held-out accuracy.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .flywheel import development_assignment
from .models import Item, LabeledItem

EVALUATION_RATE = 0.25


@dataclass(frozen=True)
class Vote:
    output: str
    item_id: str
    values: Mapping
    label: str
    comment: str = ""


def split_votes(votes: Sequence[Vote], seed: str, *, rate: float = EVALUATION_RATE):
    """(examples, evaluation) for one output's votes, from each item's identity alone."""
    if len({vote.output for vote in votes}) > 1:
        raise ValueError("votes count per site: split one output's votes at a time")
    if len({vote.item_id for vote in votes}) != len(votes):
        raise ValueError("pass each item's current vote once")
    examples, evaluation = [], []
    for vote in votes:
        (evaluation if development_assignment(seed + ":vote-evaluation", vote.item_id, rate=rate) else examples).append(vote)
    return tuple(examples), tuple(evaluation)


def labeled_examples(votes: Sequence[Vote]) -> tuple[LabeledItem, ...]:
    """The votes as trusted examples, carrying the editor's explanation when there is one."""
    return tuple(
        LabeledItem(
            Item(vote.item_id, dict(vote.values)),
            vote.label,
            "trusted",
            {"human_feedback": vote.comment} if vote.comment else {},
        )
        for vote in votes
    )


def agreement(votes: Sequence[Vote], decided: Mapping[str, str], *, positive: str) -> dict:
    """How the decided labels compare with editors' votes on the items decided under both."""
    rows = [(vote.label, decided[vote.item_id]) for vote in votes if vote.item_id in decided]
    kept = [label for label, said in rows if said == positive]
    good = [label for label, _ in rows if label == positive]
    return {
        "compared": len(rows),
        "agreed": sum(label == said for label, said in rows),
        "wronglyKept": sum(label != positive for label in kept),
        "wronglyDropped": sum(said != positive for label, said in rows if label == positive),
        "kept": len(kept),
        "keptGood": sum(label == positive for label in kept),
        "good": len(good),
    }
