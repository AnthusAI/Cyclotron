"""Classifier criteria as versioned source: question, labels, doctrine source and editor approval.

A criteria file is the reviewed source of one classifier's question. Applying it saves a new
classifier revision whose config carries a ``criteria`` provenance block (doctrine source and
approval), so every revision of the question can be audited from the catalog alone. Only approved
criteria are ever applied; a pending file is reported and held.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "cyclotron-lab-criteria/v1"
DEFINITION_FILE = "definition.json"
DEFINITION_SCHEMA = "cyclotron-lab-definition/v1"
DRAFT_SUFFIX = " (DRAFT criteria, awaiting editor approval)"
STATUSES = ("approved", "pending")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def validate_provenance(block: object) -> dict:
    """Shape of the ``criteria`` block stored in a classifier config."""
    if not isinstance(block, Mapping) or set(block) != {"file", "source", "approval", "question_sha256"}:
        raise ValueError("criteria provenance needs file, source, approval and question_sha256")
    _text(block["file"], "criteria file")
    source = block["source"]
    if not isinstance(source, Mapping) or not _text(source.get("summary"), "doctrine summary") or not source.get("sources"):
        raise ValueError("criteria provenance needs a doctrine summary and at least one source")
    approval = block["approval"]
    if not isinstance(approval, Mapping) or approval.get("status") not in STATUSES:
        raise ValueError("criteria approval status must be approved or pending")
    if approval["status"] == "approved":
        _text(approval.get("approved_by"), "approved_by")
        if not isinstance(approval.get("approved_on"), str) or not _DATE.fullmatch(approval["approved_on"]):
            raise ValueError("an approval needs an ISO approved_on date")
    if not re.fullmatch(r"[0-9a-f]{64}", str(block["question_sha256"])):
        raise ValueError("question_sha256 must be a sha256 hex digest")
    return dict(block)


@dataclass(frozen=True)
class Criteria:
    path: str
    classifier_id: str
    name: str
    labels: tuple[dict, ...]
    rubric: str
    question: str
    doctrine: dict
    approval: dict
    previous: dict | None  # None: a new classifier that does not exist in the catalog yet

    @property
    def approved(self) -> bool:
        return self.approval["status"] == "approved"

    def provenance(self) -> dict:
        return {
            "file": self.path,
            "source": self.doctrine,
            "approval": self.approval,
            "question_sha256": hashlib.sha256(self.question.encode()).hexdigest(),
        }

    def classifier_config(self) -> dict:
        """Exactly what ``save_classifier`` receives for this criteria."""
        return {
            "classes": [dict(row) for row in self.labels],
            "question": self.question,
            "rubric": self.rubric,
            "criteria": self.provenance(),
        }

    @property
    def is_new(self) -> bool:
        return self.previous is None

    def previous_config(self) -> dict | None:
        if self.previous is None:
            return None
        return {
            "classes": [dict(row) for row in self.labels],
            "question": self.previous["question"],
            "rubric": self.rubric,
        }


def parse(document: object, path: str) -> Criteria:
    if not isinstance(document, Mapping) or document.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a {SCHEMA} document")
    classifier = document.get("classifier") or {}
    previous = document.get("previous")
    if previous is not None and not isinstance(previous, Mapping):
        raise ValueError(f"{path}: previous must be an object or null")
    if not isinstance(document.get("labels"), list) or len(document["labels"]) < 2:
        raise ValueError(f"{path}: at least two labels required")
    criteria = Criteria(
        path,
        _text(classifier.get("id"), "classifier id"),
        _text(classifier.get("name"), "classifier name"),
        tuple(dict(row) for row in document["labels"]),
        document.get("rubric", ""),
        _text(document.get("question"), "question"),
        dict(document.get("doctrine") or {}),
        dict(document.get("approval") or {}),
        None if previous is None else dict(previous),
    )
    if previous is not None:
        if type(previous.get("revision")) is not int:
            raise ValueError(f"{path}: previous revision required")
        _text(previous.get("question"), "previous question")
    validate_provenance(criteria.provenance())
    if path.rsplit("/", 1)[-1] != f"{criteria.classifier_id}.json":
        raise ValueError(f"{path}: file name must be the classifier id")
    return criteria


def load_directory(directory: Path, *, relative_to: Path | None = None) -> tuple[Criteria, ...]:
    result = []
    for file in sorted(Path(directory).glob("*.json")):
        if file.name == DEFINITION_FILE:
            continue
        shown = str(file.relative_to(relative_to)) if relative_to else file.name
        result.append(parse(json.loads(file.read_text()), shown))
    if len({c.classifier_id for c in result}) != len(result):
        raise ValueError("criteria files must name distinct classifiers")
    return tuple(result)


@dataclass(frozen=True)
class Step:
    action: str  # save | current | hold | drift
    classifier_id: str
    live_revision: int | None
    reason: str = ""


def plan_classifiers(store, criteria) -> list[Step]:
    """Compare each criteria file with the latest live classifier revision. Reads only."""
    steps = []
    for item in criteria:
        try:
            live = store.classifier(item.classifier_id)
        except ValueError:
            steps.append(Step("save" if item.approved else "hold", item.classifier_id, None, "no live classifier yet"))
            continue
        config = live["config"]
        expected = item.classifier_config()
        if item.is_new:
            if item.approved and live["name"] == item.name and config == expected:
                steps.append(Step("current", item.classifier_id, live["revision"]))
            else:
                steps.append(
                    Step(
                        "drift",
                        item.classifier_id,
                        live["revision"],
                        "a classifier meant to be new already exists with other text",
                    )
                )
        elif item.approved and live["name"] == item.name and config == expected:
            steps.append(Step("current", item.classifier_id, live["revision"]))
        elif {k: config.get(k) for k in ("classes", "question", "rubric")} == item.previous_config() and "criteria" not in config:
            if item.approved:
                steps.append(Step("save", item.classifier_id, live["revision"], "live still has the previous question"))
            else:
                steps.append(Step("hold", item.classifier_id, live["revision"], "approval is pending"))
        else:
            steps.append(
                Step("drift", item.classifier_id, live["revision"], "live matches neither the previous nor the new text")
            )
    return steps


def load_definition(directory: Path) -> dict:
    document = json.loads((Path(directory) / DEFINITION_FILE).read_text())
    keys = {"schema", "id", "name", "description", "draft_description", "order", "settings_from", "supersedes"}
    if document.get("schema") != DEFINITION_SCHEMA or set(document) != keys:
        raise ValueError("definition.json is not a complete " + DEFINITION_SCHEMA + " document")
    if not document["order"] or len(set(document["order"])) != len(document["order"]):
        raise ValueError("definition order needs distinct classifier ids")
    return document


@dataclass(frozen=True)
class DefinitionStep:
    action: str  # define | current | blocked
    definition_id: str
    name: str
    classifiers: tuple[tuple[str, int], ...]
    settings: dict
    reason: str = ""


def plan_definition(store, criteria, steps, definition: Mapping) -> DefinitionStep:
    """The successor cyclotron definition that pins the criteria revisions a deploy would produce."""
    by_id = {c.classifier_id: c for c in criteria}
    by_step = {s.classifier_id: s for s in steps}
    if set(definition["order"]) != set(by_id):
        raise ValueError("definition order must name exactly the criteria files")
    draft = any(not c.approved for c in criteria)
    source = store.cyclotron_definition(definition["settings_from"]["id"], definition["settings_from"]["revision"])
    settings = {**source["settings"], "description": definition["draft_description" if draft else "description"]}
    name = definition["name"] + (DRAFT_SUFFIX if draft else "")
    refs = []
    for cid in definition["order"]:
        step = by_step[cid]
        if step.action == "drift":
            return DefinitionStep("blocked", definition["id"], name, (), settings, f"{cid}: {step.reason}")
        revision = (step.live_revision or 0) + 1 if step.action == "save" else step.live_revision
        refs.append((cid, revision))
    try:
        live = store.cyclotron_definition(definition["id"])
    except ValueError:
        live = None
    refs_json = [{"id": cid, "revision": revision} for cid, revision in refs]
    if live and (live["name"], live["classifiers"], live["settings"]) == (name, refs_json, settings):
        return DefinitionStep("current", definition["id"], name, tuple(refs), settings)
    return DefinitionStep("define", definition["id"], name, tuple(refs), settings)


def apply_plan(store, criteria, steps, definition_step) -> dict:
    """Write the planned catalog changes. Idempotent, and refuses drift and unapproved criteria."""
    by_id = {c.classifier_id: c for c in criteria}
    if definition_step.action == "blocked" or any(s.action == "drift" for s in steps):
        raise ValueError("live classifiers drifted from the reviewed text; reconcile before applying")
    saved = {}
    for step in steps:
        if step.action != "save":
            continue
        item = by_id[step.classifier_id]
        if not item.approved:
            raise ValueError(f"{item.classifier_id} is not approved")
        row = store.save_classifier(item.classifier_id, item.name, item.classifier_config())
        expected = dict(definition_step.classifiers)[item.classifier_id]
        if row["revision"] != expected:
            raise ValueError(f"{item.classifier_id} saved as revision {row['revision']}, planned {expected}")
        saved[item.classifier_id] = row["revision"]
    result = {"classifiers": saved, "definition": None}
    if definition_step.action == "define":
        refs = [{"id": cid, "revision": revision} for cid, revision in definition_step.classifiers]
        row = store.save_cyclotron_definition(definition_step.definition_id, definition_step.name, refs, definition_step.settings)
        result["definition"] = {"id": row["id"], "revision": row["revision"]}
    return result
