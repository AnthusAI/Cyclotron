"""What an embedded cyclotron's store fixes when it is first opened, and the priming added later.

The seed rubric, the seed supporting questions and whether identical supporting
questions are shared are written once, when a store is created, so reopening
never changes a store's initial context. Primed examples added after creation
are recorded as additions and applied to each output's example list once.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from .classifier_config import ClassifierConfig
from .embedded_records import ClassifierSpec, CyclotronDefinition, _json
from .models import DecisionTask

PRIMING_ADDITIONS_KEY = "priming_additions"


def normalize_supporting_questions(definition: CyclotronDefinition, seeds) -> dict[str, list[dict]]:
    """Validate seed supporting questions into the optimizer's own question shape."""
    specs = {spec.id: spec for spec in definition.classifiers}
    seeds = dict(seeds or {})
    if set(seeds) - set(specs):
        raise ValueError("seed_supporting_questions maps classifier ids to supporting questions")
    normalized = {}
    for classifier, questions in seeds.items():
        if isinstance(questions, (str, Mapping)) or not isinstance(questions, Sequence):
            raise ValueError("seed_supporting_questions needs a list of questions per classifier")
        rows = []
        for question in questions:
            if isinstance(question, DecisionTask):
                question = {"name": question.name, "instructions": question.instructions,
                            "labels": list(question.labels)}
            if not isinstance(question, Mapping) or set(question) != {"name", "instructions", "labels"}:
                raise ValueError("a supporting question has exactly name, instructions and labels")
            task = DecisionTask(question["name"], question["labels"], question["instructions"],
                                specs[classifier].input_field)
            rows.append({"name": task.name, "instructions": task.instructions, "labels": list(task.labels)})
        # The same validation the optimizer's proposals pass: unique, not "decision".
        ClassifierConfig(specs[classifier].task, tasks=tuple(
            DecisionTask(row["name"], tuple(row["labels"]), row["instructions"]) for row in rows))
        normalized[classifier] = rows
    return normalized


def priming_includes(saved_priming: Mapping[str, Any], requested_priming: Mapping[str, Any]) -> bool:
    """A reopen may pass the examples a store was created with after more were added."""
    return all(row in saved_priming.get(cid, ()) for cid, rows in requested_priming.items() for row in rows)


class StoreSeeds:
    def __init__(self, root_storage, *, seed_rubrics: Mapping[str, str], seed_supporting_questions: Mapping[str, list],
                 requested_sharing: bool | None):
        self.root_storage = root_storage
        self.seed_rubrics = seed_rubrics
        self.seed_supporting_questions = seed_supporting_questions
        self.requested_sharing = requested_sharing

    def rubric(self, classifier: str) -> str:
        """The first rubric for a classifier: fixed when its store is first opened.

        The seed is not part of the definition, so an application can derive it
        from its own parameters (for example a publication's doctrine). Later
        changes to those parameters do not rewrite a store's rubric; the LLM
        optimizer owns it from then on.
        """
        key = f"seed_rubric:{classifier}"
        row = self.root_storage.meta(key)
        if row is not None:
            return json.loads(row[0])
        seed = self.seed_rubrics.get(classifier, "")
        with self.root_storage.transaction():
            self.root_storage.insert_meta(key, _json(seed))
        return seed

    def sharing(self, *, created: bool) -> bool:
        """Whether identical supporting questions are asked once: fixed when the store is created.

        New stores share unless the application opts out. A store created
        before sharing existed keeps asking every copy separately, so its
        requests, answers and ML model features never change on upgrade.
        """
        row = self.root_storage.meta('share_supporting_questions')
        if row is not None:
            return json.loads(row[0])
        sharing = (self.requested_sharing is not False) if created else False
        with self.root_storage.transaction():
            self.root_storage.insert_meta('share_supporting_questions', _json(sharing))
        return sharing

    def supporting_questions(self, spec: ClassifierSpec, *, created: bool) -> tuple[DecisionTask, ...]:
        """The first supporting questions for a classifier: fixed when its store is first opened.

        Like the seed rubric, they are a starting point the optimizer may
        revise, replace or drop. A store created before seeding existed keeps
        an empty seed, so reopening it never changes its initial context.
        """
        key = f"seed_supporting_questions:{spec.id}"
        row = self.root_storage.meta(key)
        if row is None:
            raw = self.seed_supporting_questions.get(spec.id, []) if created else []
            with self.root_storage.transaction():
                self.root_storage.insert_meta(key, _json(raw))
        else:
            raw = json.loads(row[0])
        return tuple(DecisionTask(q["name"], tuple(q["labels"]), q["instructions"], spec.input_field) for q in raw)


def stored_priming_additions(root_storage) -> list[dict[str, Any]]:
    stored = root_storage.meta(PRIMING_ADDITIONS_KEY)
    return json.loads(stored[0]) if stored else []


def apply_priming_additions(root_storage, wheels: Mapping[str, Any]) -> None:
    """Bring each output's example list up to its recorded additions, once each.

    An addition is marked applied per output, so an example the optimizer
    later swaps out of the list is not put back on reopen.
    """
    additions = stored_priming_additions(root_storage)
    for addition in additions:
        for cid, example_ids in addition['examples'].items():
            if cid in addition['applied']:
                continue
            wheel = wheels[cid]
            missing_example_ids = [key for key in example_ids if key not in wheel.active.config.example_ids]
            if missing_example_ids:
                count = len(missing_example_ids)
                wheel.extend_example_list(missing_example_ids, change_summary=(
                    f"Added {count} primed example{'' if count == 1 else 's'} to the example list."))
            addition['applied'][cid] = wheel.active.fingerprint
            with root_storage.transaction():
                root_storage.put_meta(PRIMING_ADDITIONS_KEY, _json(additions))
