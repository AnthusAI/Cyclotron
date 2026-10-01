"""``ClassifierBundle``: a frozen, reloadable classifier (Decision Flywheel design, section 1.2).

A bundle is a directory of three files:

* ``bundle.json`` -- canonical, text-free JSON with a SHA-256 integrity checksum (not a
  signature), in the style of ``artifacts.py``. Blocks: ``task``, ``engine``, ``rubric``,
  ``fewshot``, ``head``, ``retrieval`` (always ``null`` for now) and ``lineage``.
* ``examples.jsonl`` -- the fixed example list's texts (examples, then one reserve per label).
  Private: never commit it. Each row is checked against the manifest's input hashes on load.
  Absent for a zero-shot bundle.
* ``scorecard.yaml`` -- the rubric questions and the trained head. Core treats it as an opaque
  payload: it is hashed in the manifest and parsed by the caller's ``parse_rubric`` (for the
  harness, a Jev-Flywheel ``Score``), so core does not copy anyone's head or feature code.

``classify`` makes **one** request per item through the engine's ``system_one(state, questions)``
contract: every rubric question, with ``state = {labeled_examples, target}`` when the bundle has
a list (the shape of ``adapters/jev.py``) or ``state = {text}`` when it has none (zero-shot). A
target that is itself one of the examples sees that label's reserve instead (the
``FixedExampleList`` rule). The rubric then turns the answers into a ``DecisionResult``.
"""
from __future__ import annotations

import hashlib
import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .budget import ContextBudget, build_context_plan
from .context import FixedExampleList, input_hash
from .models import DecisionResult, DecisionTask, Item, LabeledItem

BUNDLE_VERSION = 1
MANIFEST_FILE = "bundle.json"
EXAMPLES_FILE = "examples.jsonl"
SCORECARD_FILE = "scorecard.yaml"
DISPLAY_ORDER = "canonical"
_TOP_KEYS = {"bundle_version", "bundle_hash", "task", "engine", "rubric", "fewshot", "head",
             "retrieval", "lineage"}
_ENGINE_KEYS = {"adapter", "configured_model", "model_identity"}


class BundleValidationError(ValueError):
    pass


class BundleModelWarning(UserWarning):
    """The engine reported a different model than the one the bundle was built with."""


class Rubric(Protocol):
    """The parsed ``scorecard.yaml``: the questions to ask and the head that reads the answers."""

    def questions(self) -> Mapping[str, Mapping[str, Any]]: ...

    def predict(self, answers: Mapping[str, Mapping[str, Any]]) -> DecisionResult: ...


@dataclass(frozen=True)
class ClassifierBundle:
    task: DecisionTask
    scorecard: str                                  # the rubric-and-head payload, opaque to core
    rubric: Rubric                                  # ``scorecard`` parsed by the caller
    examples: FixedExampleList | None = None        # None: zero-shot
    example_rows: Sequence[LabeledItem] = ()        # the list's examples and reserves, with texts
    engine: Mapping[str, str] = field(default_factory=dict)
    head: Mapping[str, Any] = field(default_factory=dict)          # text-free head summary
    provenance: Mapping[str, Any] = field(default_factory=dict)    # who proposed the questions
    fewshot_audit: Mapping[str, Any] = field(default_factory=dict)  # the list optimizer's record
    lineage: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.scorecard, str) or not self.scorecard:
            raise BundleValidationError("the scorecard payload must be non-empty text")
        missing = _ENGINE_KEYS - set(self.engine)
        if missing:
            raise BundleValidationError(f"engine needs {sorted(missing)}")
        rows = {row.item.id: row for row in self.example_rows}
        if self.examples is None:
            if rows:
                raise BundleValidationError("a zero-shot bundle has no example rows")
        else:
            refs = self.examples.examples + self.examples.reserves
            if set(rows) != {ref.id for ref in refs} or len(rows) != len(self.example_rows):
                raise BundleValidationError("example rows must be exactly the list's examples and reserves")
            for ref in refs:
                row = rows[ref.id]
                if row.label != ref.label or input_hash(self.task, row.item) != ref.input_hash:
                    raise BundleValidationError(f"example {ref.id!r} does not match the list")
            ordered = tuple(rows[ref.id] for ref in refs)
            object.__setattr__(self, "example_rows", ordered)
        object.__setattr__(self, "example_rows", tuple(self.example_rows))
        _json_only(self.head, "head")
        _json_only(self.provenance, "provenance")
        _json_only(self.fewshot_audit, "fewshot_audit")
        _json_only(self.lineage, "lineage")

    # ---- the request ---------------------------------------------------------------------

    def context_for(self, target: Item) -> tuple[LabeledItem, ...]:
        if self.examples is None:
            return ()
        plan = build_context_plan(self.task, target, self.example_rows, self.examples,
                                  budget=ContextBudget(per_label=self.examples.per_label),
                                  display_order=DISPLAY_ORDER, order_seed=0,
                                  presentation_label_order=self.task.labels)
        return plan.examples

    def request_for(self, target: Item) -> tuple[dict[str, Any], dict[str, Any]]:
        """The one request ``classify`` sends: (state, questions)."""
        self.task.validate_target(target)
        text = target.values[self.task.input_field]
        questions = {name: dict(q) for name, q in self.rubric.questions().items()}
        if self.examples is None:
            return {"text": text}, questions
        shown = [{"text": row.item.values[self.task.input_field], "label": row.label}
                 for row in self.context_for(target)]
        return {"labeled_examples": shown, "target": {"text": text}}, questions

    async def classify(self, target: Item, client: Any) -> DecisionResult:
        """One ``system_one`` request, then the head. ``client`` is any async ``system_one``."""
        state, questions = self.request_for(target)
        response = await client.system_one(state=state, questions=questions)
        reported = getattr(response, "model", None)
        expected = self.engine.get("reported_model")
        if reported and expected and reported != expected:
            warnings.warn(f"the engine reported model {reported!r}; the bundle was built with {expected!r}",
                          BundleModelWarning, stacklevel=2)
        answers = {name: _mapping(answer) for name, answer in _mapping(getattr(response, "answers", {})).items()}
        return self.task.validate_result(self.rubric.predict(answers))

    # ---- persistence ---------------------------------------------------------------------

    def _examples_text(self) -> str:
        return "".join(_canonical({"id": row.item.id, "label": row.label,
                                   "text": row.item.values[self.task.input_field]}) + "\n"
                       for row in self.example_rows)

    def manifest(self) -> dict[str, Any]:
        fewshot = None
        if self.examples is not None:
            fewshot = {"policy": self.examples.name, "fingerprint": self.examples.fingerprint,
                       "per_label": self.examples.per_label,
                       "examples": [ref.as_dict() for ref in self.examples.examples],
                       "reserves": [ref.as_dict() for ref in self.examples.reserves],
                       "display_order": DISPLAY_ORDER, "presentation_label_order": list(self.task.labels),
                       "examples_sha256": _sha(self._examples_text()), "audit": dict(self.fewshot_audit)}
        questions = {name: _sha(_canonical(q)) for name, q in sorted(self.rubric.questions().items())}
        document = {
            "bundle_version": BUNDLE_VERSION,
            "task": {"name": self.task.name, "labels": list(self.task.labels),
                     "input_field": self.task.input_field, "fingerprint": self.task.fingerprint},
            "engine": dict(self.engine),
            "rubric": {"scorecard_sha256": _sha(self.scorecard), "questions": questions,
                       "provenance": dict(self.provenance)},
            "fewshot": fewshot,
            "head": dict(self.head),
            "retrieval": None,
            "lineage": dict(self.lineage),
        }
        document["bundle_hash"] = _sha(_canonical(document))
        return document

    @property
    def bundle_hash(self) -> str:
        return self.manifest()["bundle_hash"]

    def save(self, directory: str | Path) -> str:
        """Write the three files; returns the bundle hash. Never overwrites a different bundle."""
        directory = Path(directory)
        manifest = self.manifest()
        text = _canonical(manifest)
        for row in self.example_rows:
            if len(row.item.values[self.task.input_field]) >= 12 and row.item.values[self.task.input_field] in text:
                raise BundleValidationError("refusing to write a manifest that contains example text")
        existing = directory / MANIFEST_FILE
        if existing.exists():
            if json.loads(existing.read_text()).get("bundle_hash") != manifest["bundle_hash"]:
                raise BundleValidationError(f"{directory} already holds a different bundle")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / SCORECARD_FILE).write_text(self.scorecard, encoding="utf-8")
        if self.examples is not None:
            (directory / EXAMPLES_FILE).write_text(self._examples_text(), encoding="utf-8")
        existing.write_text(text + "\n", encoding="utf-8")
        return manifest["bundle_hash"]


def load_bundle(directory: str | Path, task: DecisionTask, parse_rubric: Callable[[str], Rubric], *,
                configured_model: str | None = None) -> ClassifierBundle:
    """Verify every hash, the task fingerprint and (if given) the configured engine model."""
    directory = Path(directory)
    try:
        document = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"),
                              object_pairs_hook=_unique_object)
    except (OSError, json.JSONDecodeError) as error:
        raise BundleValidationError("bundle.json is missing or not valid JSON") from error
    if not isinstance(document, dict) or set(document) != _TOP_KEYS:
        raise BundleValidationError("bundle.json has unexpected or missing blocks")
    if document["bundle_version"] != BUNDLE_VERSION:
        raise BundleValidationError("bundle version is not supported")
    supplied = document["bundle_hash"]
    if supplied != _sha(_canonical({k: v for k, v in document.items() if k != "bundle_hash"})):
        raise BundleValidationError("bundle hash does not match content")
    if document["task"].get("fingerprint") != task.fingerprint:
        raise BundleValidationError("task fingerprint does not match the bundle")
    engine = document["engine"]
    if not isinstance(engine, dict) or _ENGINE_KEYS - set(engine):
        raise BundleValidationError("engine block is invalid")
    if configured_model is not None and engine["configured_model"] != configured_model:
        raise BundleValidationError(f"the bundle was built for {engine['configured_model']!r}, "
                                    f"not {configured_model!r}")
    if document["retrieval"] is not None:
        raise BundleValidationError("retrieval bundles are not supported yet")
    scorecard = (directory / SCORECARD_FILE).read_text(encoding="utf-8")
    if _sha(scorecard) != document["rubric"]["scorecard_sha256"]:
        raise BundleValidationError("scorecard.yaml does not match its hash")
    fewshot = document["fewshot"]
    examples, rows = None, []
    if fewshot is not None:
        try:
            examples = FixedExampleList.from_configuration({"examples": fewshot["examples"],
                                                            "reserves": fewshot["reserves"]})
        except (KeyError, TypeError, ValueError) as error:
            raise BundleValidationError("the example list is invalid") from error
        if examples.fingerprint != fewshot["fingerprint"]:
            raise BundleValidationError("the example list does not match its fingerprint")
        text = (directory / EXAMPLES_FILE).read_text(encoding="utf-8")
        if _sha(text) != fewshot["examples_sha256"]:
            raise BundleValidationError("examples.jsonl does not match its hash")
        for line in text.splitlines():
            raw = json.loads(line)
            rows.append(LabeledItem(Item(raw["id"], {task.input_field: raw["text"]}), raw["label"]))
    rubric = parse_rubric(scorecard)
    bundle = ClassifierBundle(task, scorecard, rubric, examples=examples, example_rows=rows, engine=engine,
                              head=document["head"], provenance=document["rubric"]["provenance"],
                              fewshot_audit=(fewshot or {}).get("audit") or {}, lineage=document["lineage"])
    if bundle.bundle_hash != supplied:
        raise BundleValidationError("the reloaded bundle does not reproduce its hash")
    return bundle


def _json_only(value: Any, name: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise BundleValidationError(f"{name} must be plain JSON") from error


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    return dict(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BundleValidationError(f"bundle JSON contains duplicate key {key!r}")
        result[key] = value
    return result
