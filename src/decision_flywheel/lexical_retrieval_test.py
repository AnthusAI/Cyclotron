import math

import pytest

from .context import PerLabelLexicalRetrieval
from .lexical_retrieval import STOPWORDS, LexicalRetriever
from .models import DecisionTask, Item, LabeledItem
from .retrieval import PerLabelRetrieval, stable_hash

TASK = DecisionTask("topic", ("a", "b"), "Classify the target.")


def rows(*pairs):
    return [LabeledItem(Item(item_id, {"text": text}), item_id.split("-")[0]) for item_id, text in pairs]


def test_default_tokens_drop_common_stop_words_but_keep_negations():
    retriever = LexicalRetriever()

    assert retriever.tokens("This is not a good movie") == ["not", "good", "movie"]
    assert retriever.tokens("No, I never liked it, nor did they") == ["no", "never", "liked", "nor"]
    assert retriever.tokens("I didn't like it") == ["didn", "t", "like"]


def test_the_vendored_stop_list_is_frozen_and_contains_no_negations():
    stop = STOPWORDS["en-v1"]

    assert not {"not", "no", "nor", "never", "t", "didn", "don", "but", "very"} & stop
    assert stable_hash(sorted(stop)) == "c370a9d8fbfafe767a9cfd6ecc10937194e34274091b888a58c895558bd3f361"


def test_unicode_tokens_keep_digits_and_non_english_text_while_ascii_tokens_do_not():
    text = "Rückgabe in 30 Tagen, Ｆｕｌｌ refund"

    assert LexicalRetriever(stopwords="none").tokens(text) == ["rückgabe", "in", "30", "tagen", "full", "refund"]
    assert LexicalRetriever(stopwords="none", tokenizer="ascii-v1").tokens(text) == [
        "r", "ckgabe", "in", "tagen", "refund"]


def test_lexical_v1_keeps_its_name_version_and_fingerprint():
    v1 = PerLabelLexicalRetrieval()

    assert (v1.metadata.name, v1.metadata.version, dict(v1.metadata.configuration)) == (
        "per-label-lexical-retrieval", "1", {})
    assert v1.fingerprint == "9a25d4e34dde52d3e8026ce0036b4ecb15fecaf310736b62d05ab26a64e3c4b3"


def test_lexical_v2_with_v1_settings_selects_the_same_examples_as_v1():
    pool = rows(("a-1", "alpha beta words"), ("a-2", "alpha"), ("a-3", "gamma delta"),
                ("b-1", "beta gamma"), ("b-2", "alpha beta gamma"), ("b-3", "zeta"))
    target = Item("t", {"text": "alpha beta gamma delta"})
    v2 = PerLabelRetrieval(LexicalRetriever(stopwords="none", tokenizer="ascii-v1"))

    assert v2.select(TASK, target, pool, per_label=2) == PerLabelLexicalRetrieval().select(
        TASK, target, pool, per_label=2)
    assert v2.fingerprint != PerLabelLexicalRetrieval().fingerprint


def test_bm25_scores_match_a_hand_computed_fixture():
    pool = rows(("a-1", "apple banana"), ("a-2", "apple apple cherry"),
                ("b-1", "date"), ("b-2", "banana date elder"))
    index = LexicalRetriever(stopwords="none", weighting="bm25").index(TASK, pool)
    target = Item("t", {"text": "apple banana"})
    idf = math.log(2)  # ln(1 + (4 - 2 + 0.5) / (2 + 0.5)) for apple and banana; average length 9/4

    assert index.top_per_label(target, "a", 2, set()) == [
        ("a-1", round(2 * idf * 2.2 / 2.1, 9)), ("a-2", round(idf * 4.4 / 3.5, 9))]
    assert index.top_per_label(target, "b", 2, set()) == [("b-2", round(idf * 2.2 / 2.5, 9)), ("b-1", 0.0)]


def test_tfidf_prefers_a_shared_rare_word_over_a_shared_common_word():
    pool = rows(("a-1", "common rare"), ("a-2", "common filler"), ("a-3", "common other"),
                ("b-1", "common stuff"), ("b-2", "common things"))
    index = LexicalRetriever(weighting="tfidf-cosine").index(TASK, pool)

    best = index.top_per_label(Item("t", {"text": "rare common"}), "a", 1, set())

    assert best[0][0] == "a-1"


def test_exact_score_ties_break_by_ascending_id():
    pool = rows(("a-2", "same words"), ("a-1", "words same"), ("b-1", "x"), ("b-2", "y"))
    index = LexicalRetriever().index(TASK, pool)

    assert [item_id for item_id, _ in index.top_per_label(Item("t", {"text": "same"}), "a", 2, set())] == [
        "a-1", "a-2"]


def test_the_fingerprint_changes_with_each_knob_and_bm25_parameters_only_matter_for_bm25():
    base = LexicalRetriever()

    assert base.configuration == {"kind": "lexical", "version": "2", "stopwords": "en-v1",
                                  "tokenizer": "unicode-v1", "weighting": "binary-cosine"}
    variants = [LexicalRetriever(stopwords="none"), LexicalRetriever(tokenizer="ascii-v1"),
                LexicalRetriever(weighting="bm25"), LexicalRetriever(weighting="tfidf-cosine")]
    assert len({base.fingerprint, *(variant.fingerprint for variant in variants)}) == 5
    assert LexicalRetriever(k1=2.0).fingerprint == base.fingerprint
    assert LexicalRetriever(weighting="bm25", k1=2.0).fingerprint != LexicalRetriever(weighting="bm25").fingerprint


@pytest.mark.parametrize("options", [{"stopwords": "en-v9"}, {"tokenizer": "whitespace"},
                                     {"weighting": "dense"}, {"k1": -1}, {"b": 1.5}, {"k1": True}])
def test_unknown_or_invalid_lexical_settings_are_rejected(options):
    with pytest.raises(ValueError):
        LexicalRetriever(**options)
