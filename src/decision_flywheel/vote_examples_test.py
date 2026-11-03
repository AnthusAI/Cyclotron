import pytest

from .vote_examples import Vote, agreement, labeled_examples, split_votes


def votes(count, *, output="site-relevance", label="include"):
    return [Vote(output, f"item-{n}", {"text": f"story {n}"}, label) for n in range(count)]


def test_evaluation_votes_and_examples_are_disjoint_and_cover_every_vote():
    examples, evaluation = split_votes(votes(200), "seed")
    assert examples and evaluation
    assert not {v.item_id for v in examples} & {v.item_id for v in evaluation}
    assert len(examples) + len(evaluation) == 200


def test_the_split_does_not_depend_on_the_label():
    first = split_votes(votes(200, label="include"), "seed")
    second = split_votes(votes(200, label="exclude"), "seed")
    assert [v.item_id for v in first[1]] == [v.item_id for v in second[1]]


def test_the_same_seed_gives_the_same_split_and_another_seed_gives_another():
    assert split_votes(votes(200), "seed") == split_votes(votes(200), "seed")
    assert split_votes(votes(200), "seed")[1] != split_votes(votes(200), "other")[1]


def test_one_sites_votes_never_mix_with_anothers():
    mixed = votes(2) + votes(2, output="other-relevance")
    with pytest.raises(ValueError, match="per site"):
        split_votes(mixed, "seed")


def test_an_item_with_two_votes_is_refused_so_only_the_current_vote_counts():
    with pytest.raises(ValueError, match="current vote"):
        split_votes(votes(1) + votes(1), "seed")


def test_an_example_carries_the_editors_explanation_only_when_there_is_one():
    explained, plain = labeled_examples(
        [Vote("s", "a", {"text": "x"}, "exclude", "no evidence"), Vote("s", "b", {"text": "y"}, "include")]
    )
    assert (explained.label, explained.source, explained.context) == ("exclude", "trusted", {"human_feedback": "no evidence"})
    assert plain.context == {}


def test_agreement_counts_wrongly_kept_and_wrongly_dropped_on_items_decided():
    rows = [
        Vote("s", "kept-good", {}, "include"),
        Vote("s", "kept-bad", {}, "exclude"),
        Vote("s", "dropped-good", {}, "include"),
        Vote("s", "dropped-bad", {}, "exclude"),
        Vote("s", "undecided", {}, "include"),
    ]
    decided = {"kept-good": "include", "kept-bad": "include", "dropped-good": "exclude", "dropped-bad": "exclude"}
    assert agreement(rows, decided, positive="include") == {
        "compared": 4,
        "agreed": 2,
        "wronglyKept": 1,
        "wronglyDropped": 1,
        "kept": 2,
        "keptGood": 1,
        "good": 2,
    }
