"""Publication provenance composed through an injected revisioned catalog.

No SQL, files, cloud client or alternative storage adapter lives here. Logical
item identities and evaluation partitions are unchanged. Content addresses name
only immutable context/receipt envelopes, not new operational item identities.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def copied(value):
    return json.loads(canonical(value))


@dataclass(frozen=True)
class RevisionRef:
    listId: str
    itemId: str
    revision: int
    contentHash: str

    def __post_init__(self):
        if (
            not isinstance(self.listId, str)
            or not self.listId
            or not isinstance(self.itemId, str)
            or not self.itemId
            or type(self.revision) is not int
            or self.revision < 1
        ):
            raise ValueError("an explicit immutable catalog revision is required")
        if (
            not isinstance(self.contentHash, str)
            or len(self.contentHash) != 64
            or any(char not in "0123456789abcdef" for char in self.contentHash)
        ):
            raise ValueError("canonical decoded revision content hash required")

    def to_json(self):
        return asdict(self)


class PublicationRecords:
    def __init__(self, catalog, *, account_id, source_lists, binding_list, record_list):
        if (
            not account_id
            or not source_lists
            or not binding_list
            or not record_list
            or record_list in set(source_lists) | {binding_list}
        ):
            raise ValueError("separate host-scoped source, binding and record catalogs required")
        self.catalog = catalog
        self.account_id = account_id
        self.source_lists = frozenset(source_lists)
        self.binding_list = binding_list
        self.record_list = record_list

    def reference(self, list_id, item_id, revision):
        if list_id not in self.source_lists | {self.binding_list}:
            raise ValueError("catalog scope refused")
        value = self.catalog.item_revision(list_id, item_id, revision)
        return RevisionRef(list_id, item_id, revision, digest(value))

    def resolve(self, value, allowed):
        if not isinstance(value, dict) or set(value) != {"listId", "itemId", "revision", "contentHash"}:
            raise ValueError("revision reference fields required")
        ref = RevisionRef(**value)
        if ref.listId not in allowed:
            raise ValueError("catalog scope refused")
        snapshot = self.catalog.item_revision(ref.listId, ref.itemId, ref.revision)
        if digest(snapshot) != ref.contentHash:
            raise ValueError("immutable revision content mismatch")
        return copied(snapshot)

    def prepare(self, binding, item, payload):
        if not isinstance(payload, dict) or set(payload) != {"sourceRef", "bindingRef"}:
            raise ValueError("source and binding references required")
        source = self.resolve(payload["sourceRef"], self.source_lists)
        config = self.binding_revision(binding, payload["bindingRef"])
        if source["id"] != item.id or source["values"] != dict(item.values):
            raise ValueError("model item differs from frozen source revision")
        return copied({"publicationId": binding.publication_id, "source": source, "binding": config, **payload})

    def binding_revision(self, binding, reference):
        config = self.resolve(reference, {self.binding_list})
        values = config["values"]
        if set(config) != {"id", "values", "occurred_at"} or set(values) != {"accountId", "publicationId", "revision", "outputs"}:
            raise ValueError(
                "binding records contain only public identity/revision/output metadata; private transport configuration belongs outside them"
            )
        if (values.get("accountId"), values.get("publicationId"), values.get("revision"), values.get("outputs")) != (
            self.account_id,
            binding.publication_id,
            binding.revision,
            list(binding.outputs),
        ):
            raise ValueError("binding revision does not match authorized publication outputs")
        self.check_binding(binding)
        return config

    def check_binding(self, binding):
        for row in self.records():
            if (
                row["kind"] == "context"
                and row["data"]["publicationId"] != binding.publication_id
                and set(row["data"]["binding"]["values"]["outputs"]) & set(binding.outputs)
            ):
                raise ValueError("historical output ownership requires an explicit migration, not publication reassignment")
            if (
                row["kind"] == "context"
                and row["data"]["publicationId"] == binding.publication_id
                and row["data"]["binding"]["values"]["revision"] == binding.revision
                and row["data"]["binding"]["values"]["outputs"] != list(binding.outputs)
            ):
                raise ValueError("binding revision identity cannot be reused for changed output mapping")
            if row["kind"] == "steering-pending":
                prior = self.resolve(row["data"]["payload"]["bindingRef"], {self.binding_list})["values"]
                if prior["publicationId"] != binding.publication_id and set(prior["outputs"]) & set(binding.outputs):
                    raise ValueError("historical output ownership requires an explicit migration, not publication reassignment")
                if (
                    prior["publicationId"] == binding.publication_id
                    and prior["revision"] == binding.revision
                    and prior["outputs"] != list(binding.outputs)
                ):
                    raise ValueError("binding revision identity cannot be reused for changed output mapping")

    def preview(self, kind, data, at):
        values = copied(
            {
                "schema": "cyclotron-publication-record/v1",
                "kind": kind,
                "accountId": self.account_id,
                "createdAt": at,
                "data": data,
            }
        )
        identifier = "publication-" + digest(values)
        occurred = datetime.fromisoformat(at.replace("Z", "+00:00"))
        occurred = occurred.replace(tzinfo=timezone.utc) if occurred.tzinfo is None else occurred.astimezone(timezone.utc)
        return {"id": identifier, "occurred_at": occurred.isoformat(), "values": values}

    def ensure_capacity(self, items):
        """The record ids that already exist. Validates every stored record; there is no count or byte ceiling."""
        return frozenset(row["id"] for row in self._rows())

    def put(self, kind, data, at):
        item = self.preview(kind, data, at)
        existing = self.ensure_capacity([item])
        if item["id"] not in existing:
            self.catalog.upsert_list_items(self.record_list, [item])
        return item["id"]

    def _rows(self):
        rows = self.catalog.snapshot_items(self.record_list)
        for row in rows:
            values = row["values"]
            if (
                row["revision"] != 1
                or values.get("schema") != "cyclotron-publication-record/v1"
                or values.get("accountId") != self.account_id
                or row["id"] != "publication-" + digest(values)
            ):
                raise ValueError("publication immutable record changed or escaped account scope")
        return rows

    def records(self):
        rows = self._rows()
        result = []
        for row in rows:
            values = row["values"]
            result.append({"recordId": row["id"], **values})
        return result

    def freeze(self, decision_id, at, prepared, *, prediction):
        from types import SimpleNamespace

        prior = prepared["binding"]["values"]
        self.check_binding(
            SimpleNamespace(publication_id=prior["publicationId"], outputs=tuple(prior["outputs"]), revision=prior["revision"])
        )
        if (
            not isinstance(prediction, dict)
            or prediction.get("decisionId") != decision_id
            or prediction.get("itemId") != prepared["source"]["id"]
            or set(prediction.get("classifiers", {})) != set(prior["outputs"])
        ):
            raise ValueError("the exact publication-scoped prediction must accompany its frozen inputs")
        data = copied({"decisionId": decision_id, **prepared, "prediction": prediction})
        identifier = self.put("context", data, at)
        return {"contextId": identifier, **data}

    def contexts(self, publication, decision_id):
        rows = [
            {"contextId": row["recordId"], **row["data"]}
            for row in self.records()
            if row["kind"] == "context"
            and row["data"]["publicationId"] == publication
            and row["data"]["decisionId"] == decision_id
        ]
        return sorted(rows, key=lambda row: (row["sourceRef"]["revision"], row["bindingRef"]["revision"], row["contextId"]))

    def context(self, identifier, publication, decision_id):
        found = [row for row in self.contexts(publication, decision_id) if row["contextId"] == identifier]
        if len(found) != 1:
            raise ValueError("frozen publication context unavailable")
        row = found[0]
        # Reads remain tied to the original immutable catalog revisions.
        if (
            self.resolve(row["sourceRef"], self.source_lists) != row["source"]
            or self.resolve(row["bindingRef"], {self.binding_list}) != row["binding"]
        ):
            raise ValueError("frozen publication revision changed")
        return row

    def review(self, context, review, *, source_cursor):
        if type(source_cursor) is not int or source_cursor < 1:
            raise ValueError("authoritative review event cursor required")
        if review["decisionId"] != context["decisionId"] or review["classifier"] not in context["binding"]["values"]["outputs"]:
            raise ValueError("review escaped frozen publication context")
        data = {
            "publicationId": context["publicationId"],
            "contextId": context["contextId"],
            "review": review,
            "sourceCursor": source_cursor,
        }
        old = self.receipt(context["publicationId"], review["reviewId"])
        if old is not None and canonical(old) != canonical(data):
            raise ValueError("review provenance identity conflict")
        self.put("review", data, review["createdAt"])
        return copied(data)

    def receipt(self, publication, review_id):
        found = [
            row["data"]
            for row in self.records()
            if row["kind"] == "review"
            and row["data"]["publicationId"] == publication
            and row["data"]["review"]["reviewId"] == review_id
        ]
        if len(found) > 1:
            raise ValueError("review provenance identity conflict")
        return copied(found[0]) if found else None

    def replay(self, publication, outputs):
        rows = self.records()
        contexts = {
            row["recordId"]: row["data"]
            for row in rows
            if row["kind"] == "context" and row["data"]["publicationId"] == publication
        }
        result = []
        for row in rows:
            if row["kind"] != "review" or row["data"]["publicationId"] != publication:
                continue
            data = row["data"]
            review = data["review"]
            if review["classifier"] not in outputs:
                continue
            context = contexts.get(data["contextId"])
            if context is None:
                raise ValueError("review context is missing")
            checked = self.context(data["contextId"], publication, review["decisionId"])
            result.append({"contextId": data["contextId"], **checked, "review": review, "sourceCursor": data["sourceCursor"]})
        return sorted(result, key=lambda row: row["sourceCursor"])
