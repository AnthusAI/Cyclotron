"""The changes an embedded cyclotron's classifier set has been through since its store was created.

A store is created with one set of classifiers. Classifiers can later be added
and retired. Each change is recorded with who made it and when, together with
the specification of every classifier it touched, so a reopen can tell a
definition that is current from one that predates a recorded change, and a
retired classifier can be added again as the same identity.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from .embedded_records import ClassifierSpec, CyclotronDefinition, _json

CLASSIFIER_SET_CHANGES_KEY = "classifier_set_changes"
ADDED = "added"
RETIRED = "retired"
DIFFERENT_DEFINITION = "this store belongs to a different cyclotron definition"


def stored_classifier_set_changes(root_storage) -> list[dict[str, Any]]:
    stored = root_storage.meta(CLASSIFIER_SET_CHANGES_KEY)
    return json.loads(stored[0]) if stored else []


def spec_from_record(record: Mapping[str, Any]) -> ClassifierSpec:
    return ClassifierSpec(**{**record, "labels": tuple(record["labels"])})


def last_known_specs(changes: Sequence[Mapping[str, Any]]) -> dict[str, ClassifierSpec]:
    """The specification each changed classifier last had, in the order it first changed."""
    known: dict[str, ClassifierSpec] = {}
    for change in changes:
        for identifier, record in change["specs"].items():
            known[identifier] = spec_from_record(record)
    return known


def last_change_kinds(changes: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for change in changes:
        for identifier in change["classifiers"]:
            latest[identifier] = change
    return latest


def retired_classifier_ids(changes: Sequence[Mapping[str, Any]], active_ids: Sequence[str]) -> list[str]:
    """Classifiers that were retired and have not been added again, in the order they were retired."""
    return [
        identifier
        for identifier, change in last_change_kinds(changes).items()
        if change["kind"] == RETIRED and identifier not in active_ids
    ]


def change_record(
    kind: str, identifiers: Sequence[str], specs: Sequence[ClassifierSpec], by: str, at: str, **extra
) -> dict[str, Any]:
    return {
        "kind": kind,
        "classifiers": list(identifiers),
        "specs": {spec.id: asdict(spec) for spec in specs},
        "by": by,
        "at": at,
        **extra,
    }


def recorded_definition(definition: CyclotronDefinition) -> str:
    return _json({"fingerprint": definition.fingerprint, "definition": asdict(definition)})


def _order_free_identity(definition_record: Mapping[str, Any]) -> dict[str, Any]:
    identity = json.loads(_json(dict(definition_record)))
    identity.pop("sibling_policy", None)
    identity["classifiers"] = sorted(identity["classifiers"], key=lambda spec: spec["id"])
    return identity


def definition_matches_store(
    saved: Mapping[str, Any], definition: CyclotronDefinition, changes: Sequence[Mapping[str, Any]]
) -> bool:
    """Whether a definition is the one the store was last saved with.

    A store whose classifier set was never changed needs the exact fingerprint.
    Once classifiers have been added or retired, the order an application lists
    them in does not matter, because the store never fixed one.
    """
    if saved["fingerprint"] == definition.fingerprint:
        return True
    return bool(changes) and _order_free_identity(saved["definition"]) == _order_free_identity(asdict(definition))


def stale_definition_error(
    saved: Mapping[str, Any], definition: CyclotronDefinition, changes: Sequence[Mapping[str, Any]]
) -> ValueError:
    """The refusal for a definition that does not match the store, naming the recorded change it misses."""
    wanted = {spec.id for spec in definition.classifiers}
    saved_ids = {spec["id"] for spec in saved["definition"]["classifiers"]}
    reasons = []
    for identifier, change in last_change_kinds(changes).items():
        if change["kind"] == RETIRED and identifier in wanted:
            reasons.append(
                f"it retired classifier {identifier!r} ({change['by']}, {change['at']}); remove it from the definition,"
                " or add it again with add_classifiers"
            )
        elif change["kind"] == ADDED and identifier in saved_ids and identifier not in wanted:
            reasons.append(
                f"it added classifier {identifier!r} ({change['by']}, {change['at']}); include it in the definition,"
                " or retire it with retire_classifiers"
            )
    if not reasons:
        return ValueError(DIFFERENT_DEFINITION)
    return ValueError(f"{DIFFERENT_DEFINITION}: " + "; ".join(reasons))
