from .context import PerLabelLexicalRetrieval, RandomBalanced
from .models import DecisionTask, Item, LabeledItem

TASK = DecisionTask("topic", ("a", "b"), "Classify the target.")
CANDIDATES = [LabeledItem(Item(f"{label}-{number}", {"text": f"{label} shared token {number}"}), label) for label in TASK.labels for number in range(4)]


def test_a_random_context_is_balanced_and_repeatable():
    policy = RandomBalanced(seed=7)
    first = policy.select(TASK, Item("target", {"text": "shared"}), CANDIDATES, per_label=2)
    assert first == policy.select(TASK, Item("target", {"text": "shared"}), CANDIDATES, per_label=2)
    assert [item.label for item in first] == ["a", "a", "b", "b"]


def test_retrieval_never_selects_the_target_itself():
    target = CANDIDATES[0].item
    context = PerLabelLexicalRetrieval().select(TASK, target, CANDIDATES, per_label=1)
    assert target.id not in {item.item.id for item in context}
