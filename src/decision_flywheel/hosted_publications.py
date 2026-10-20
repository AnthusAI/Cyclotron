"""Publication projections over the existing original-session queue and S3 SDK.

Prediction and accepted-human priming use distinct, validated payloads. Targets are trusted application
configuration, not membership grants. The BFF never opens a model or engine.
"""

import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from types import SimpleNamespace

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .application_runtime import RuntimeCommand
from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from .hosted_command_worker import AuthorizedModel
from .journal import encode, uuid
from .optimizer_agent import DisabledOptimizer
from .priming_context import catalog_priming, decode_priming
from .prospectus_accounts import AccountAccessDenied, VerifiedAccountSession
from .publication_lab import CONTRACT, MAX_BYTES, PublicationActor, PublicationBinding, PublicationLab
from .s3_embedded_storage import S3CyclotronStores


@dataclass(frozen=True)
class PublicationTarget:
    account_id: str
    run_id: str
    bindings: tuple[PublicationBinding, ...]
    pending_outputs: tuple[str, ...] = ()
    model_identity: str | None = None

    def __post_init__(self):
        uuid(self.account_id)
        uuid(self.run_id)
        if self.model_identity is not None and (
            not isinstance(self.model_identity, str) or not self.model_identity.strip() or len(self.model_identity) > 512
        ):
            raise ValueError("bounded configured model identity required")
        object.__setattr__(self, "bindings", tuple(self.bindings))
        object.__setattr__(self, "pending_outputs", tuple(self.pending_outputs))
        if (
            not self.bindings
            or any(not isinstance(b, PublicationBinding) for b in self.bindings)
            or len({b.publication_id for b in self.bindings}) != len(self.bindings)
        ):
            raise ValueError("distinct configured publication bindings required")
        outputs = [output for b in self.bindings for output in b.outputs]
        if len(set(outputs)) != len(outputs):
            raise ValueError("publication feedback outputs must be distinct")
        if len(set(self.pending_outputs)) != len(self.pending_outputs) or not set(self.pending_outputs) <= set(outputs):
            raise ValueError("pending outputs must name distinct declared publication outputs")


def targets_from_environment(environment):
    raw = environment.get("CYCLOTRON_PUBLICATION_TARGETS")
    if raw is None:
        return None
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or not values or len(values) > 100:
            raise ValueError()
        result = []
        for target in values:
            if not {"accountId", "runId", "bindings"} <= set(target) or set(target) - {
                "accountId",
                "runId",
                "bindings",
                "pendingOutputs",
                "modelIdentity",
            }:
                raise ValueError()
            pending = target.get("pendingOutputs", [])
            if not isinstance(pending, list) or any(not isinstance(output, str) for output in pending):
                raise ValueError()
            bindings = []
            for row in target["bindings"]:
                if set(row) != {"publicationId", "outputs", "revision"} or not isinstance(row["outputs"], list):
                    raise ValueError()
                bindings.append(PublicationBinding(row["publicationId"], tuple(row["outputs"]), row["revision"]))
            result.append(
                PublicationTarget(
                    target["accountId"], target["runId"], tuple(bindings), tuple(pending), target.get("modelIdentity")
                )
            )
        return tuple(result)
    except (ValueError, TypeError, KeyError):
        raise ValueError("reviewed publication target configuration is invalid") from None


def configured_targets(runtime):
    targets = tuple(runtime.publication_targets() if callable(runtime.publication_targets) else runtime.publication_targets)
    if any(not isinstance(target, PublicationTarget) for target in targets):
        raise ValueError("validated publication targets required")
    keys = [(t.account_id, b.publication_id) for t in targets for b in t.bindings]
    if len(set(keys)) != len(keys):
        raise ValueError("a publication cannot select competing configured runs")
    return targets


def target_for(runtime, account, publication):
    for target in configured_targets(runtime):
        if target.account_id == account:
            for binding in target.bindings:
                if binding.publication_id == publication:
                    return target, binding
    raise AccountAccessDenied("publication is not configured for this account")


def envelope(target, binding, config):
    specs = {row["id"]: row["config"] for row in config["classifiers"]}
    pending = set(target.pending_outputs)
    if pending & set(specs):
        raise ValueError("pending publication criteria must not have a runnable classifier")
    if any(output not in specs and output not in pending for b in target.bindings for output in b.outputs):
        raise ValueError("publication binding names an unavailable run output")
    result = {
        "contractVersion": CONTRACT,
        "accountId": target.account_id,
        "publicationId": binding.publication_id,
        "bindingRevision": binding.revision,
        "features": {"frozenContext": False, "replayExport": False, "steering": False},
        "outputs": [
            {
                "id": output,
                "question": specs[output]["question"],
                "classes": [row["label"] for row in specs[output]["classes"]],
                "positiveLabel": next((row["label"] for row in specs[output]["classes"] if row.get("role") == "positive"), None),
            }
            for output in binding.outputs
            if output not in pending
        ],
    }
    waiting = [output for output in binding.outputs if output in pending]
    if waiting:
        result.update(pendingOutputs=waiting, status="pending_criteria")
    return result


def is_priming_command(kind, payload):
    """Host-owned data-only dispatch rule; never a caller-supplied live bypass."""
    return (
        kind == "publication"
        and isinstance(payload, dict)
        and set(payload) == {"publication", "bindingRevision", "priming"}
        and isinstance(payload["priming"], dict)
        and set(payload["priming"]) == {"listId", "fingerprint"}
        and all(isinstance(payload[key], str) and payload[key] for key in ("publication", "bindingRevision"))
        and isinstance(payload["priming"]["listId"], str)
        and bool(payload["priming"]["listId"])
        and isinstance(payload["priming"]["fingerprint"], str)
        and len(payload["priming"]["fingerprint"]) == 64
    )


def priming_snapshot(store, target, binding, config, list_id):
    if not target.model_identity:
        raise ValueError("reviewed publication model identity required for priming")
    if not isinstance(list_id, str) or not list_id.strip() or len(list_id) > 200:
        raise ValueError("bounded accepted priming list required")
    classifiers = [row for row in config["classifiers"] if row["id"] in binding.outputs]
    for row in classifiers:
        if store.classifier(row["id"])["revision"] != row["revision"]:
            raise ValueError("classifier revision changed; review the publication binding")
    if list_id not in {row["id"] for row in store.item_lists()}:
        raise ValueError("unknown accepted priming list")
    examples = catalog_priming(store, list_id, classifiers, {row["id"] for row in store.items(target.run_id)}, storage=store)
    fingerprint = sha256(encode([target.model_identity, list_id, classifiers, examples])).hexdigest()
    return examples, fingerprint


class _PrimingModel:
    def __init__(self, identity):
        self.model_identity = identity

    async def classify_many(self, *args, **kwargs):
        raise ValueError("priming-only operation cannot execute a provider")


class _GuardedJournal:
    """Fence native writes as well as console receipts with the original actor."""

    def __init__(self, journal, check):
        self.journal, self.check = journal, check
        self.namespace, self.max_bytes, self.boundary = journal.namespace, journal.max_bytes, journal.boundary

    def exists(self):
        return self.journal.exists()

    def read(self):
        return self.journal.read()

    def __getattr__(self, name):
        return getattr(self.journal, name)

    def initialize(self, value):
        self.check()
        return self.journal.initialize(value)

    def commit(self, expected, value):
        self.check()
        return self.journal.commit(expected, value)


class PublicationCommandRuntime:
    def __init__(self, host, view, guard, check, actor):
        self.host, self.view, self.guard, self.check, self.actor = host, view, guard, check, actor

    def open_session(self, run_id, config, items, shown):
        # Opening a console command does not open provider/storage state yet.
        return SimpleNamespace(run_id=run_id, config=config, close=lambda: None)

    async def execute(self, session, kind, payload, shown, config, *, request_id=None):
        priming = is_priming_command(kind, payload)
        if not priming and (kind != "publication" or set(payload) != {"publication", "bindingRevision", "item"}):
            raise ValueError("supported publication prediction command required")
        publication = payload["publication"]
        target, binding = target_for(self.host, self.actor.account_id, publication)
        if target.run_id != session.run_id or binding.revision != payload["bindingRevision"]:
            raise AccountAccessDenied("publication run or binding changed")
        if set(binding.outputs) & set(target.pending_outputs):
            raise ValueError("publication criteria are pending")
        examples = None
        catalog = self.host.console(self.actor.account_id) if priming else None
        if priming:
            examples, fingerprint = priming_snapshot(catalog, target, binding, config, payload["priming"]["listId"])
            if fingerprint != payload["priming"]["fingerprint"]:
                raise ValueError("accepted priming snapshot changed")
        else:
            item = payload["item"]
            if not isinstance(item, dict) or set(item) != {"id", "values"}:
                raise ValueError("a frozen structured source revision is required")
            frozen = next((row for row in self.view.items(session.run_id) if row["id"] == item["id"]), None)
            if frozen is None or frozen["values"] != item["values"]:
                raise ValueError("source does not match the canonical run input")
        if config.get("priming_examples") or config.get("priming_list_id"):
            raise ValueError("baseline prediction cannot apply priming examples")
        if not config.get("cyclotron_id"):
            raise ValueError("one configured versioned shared cyclotron is required")

        def check_scope():
            self.check()
            if target_for(self.host, self.actor.account_id, publication) != (target, binding):
                raise AccountAccessDenied("publication target changed")
            if priming:
                _, current = priming_snapshot(catalog, target, binding, config, payload["priming"]["listId"])
                if current != fingerprint:
                    raise ValueError("accepted priming snapshot changed")

        async def fresh():
            await self.guard()
            await asyncio.to_thread(check_scope)
            original = await asyncio.to_thread(self.host.sessions.get_ref, self.actor.session_ref)
            if original is None:
                raise AccountAccessDenied("original session unavailable")
            # Existing Prospectus role policy: members/admins/owners decide;
            # AccountsClient independently rejects viewer console writes.
            await asyncio.to_thread(
                self.host.accounts.authorize, original.access_token, self.actor.account_id, self.actor.person_id, "console:write"
            )

        await fresh()
        specs, rubrics = [], {}
        for row in config["classifiers"]:
            definition = row["config"]
            positive = next((c["label"] for c in definition["classes"] if c.get("role") == "positive"), None)
            specs.append(
                ClassifierSpec(
                    row["id"],
                    tuple(c["label"] for c in definition["classes"]),
                    definition["question"],
                    input_field=definition.get("input_field", "text"),
                    positive_label=positive,
                )
            )
            rubrics[row["id"]] = definition.get("seed_rubric", "")
        envelope(target, binding, config)  # Validate all configured outputs before opening state.
        model, optimizer = (
            (_PrimingModel(target.model_identity), DisabledOptimizer()) if priming else self.host.model_factory(config)
        )
        if target.model_identity is not None and model.model_identity != target.model_identity:
            raise ValueError("provider differs from reviewed publication model identity")
        if getattr(optimizer, "enabled", False):
            raise ValueError("baseline prediction requires optimizer disabled")
        name = "publication-" + sha256(config["cyclotron_id"].encode()).hexdigest()

        def journal(domain, key):
            # Reuse the existing worker's reviewed storage domains. No new IAM
            # prefix and no cloud SQLite or alternate authority is introduced.
            journal = self.host.journal(self.actor.account_id, "shared-batches" if domain == "shared" else domain, key)
            return _GuardedJournal(journal, check_scope) if priming else journal

        stores = S3CyclotronStores(journal("workspace", name), journal_for=journal, max_records=self.host.config.max_records)
        actor = PublicationActor(
            self.actor.account_id, self.actor.person_id, frozenset((publication, cap) for cap in ("decide", "read"))
        )
        definition = CyclotronDefinition(config["cyclotron_id"], tuple(specs), seed=config["seed"])
        with Cyclotron.open(
            None,
            definition,
            AuthorizedModel(model, fresh),
            optimizer,
            storage=stores,
            seed_rubrics=rubrics,
            max_requests=1 if priming else config["max_requests"],
        ) as engine:
            active_bindings = tuple(b for b in target.bindings if not set(b.outputs) & set(target.pending_outputs))
            lab = PublicationLab(engine, account_id=self.actor.account_id, bindings=active_bindings)
            if priming:
                applied = await engine.add_priming_examples(
                    decode_priming(examples), added_by=self.actor.person_id, execution_guard=fresh
                )
                result = {
                    **envelope(target, binding, config),
                    "operation": "prime",
                    "sourceFingerprint": fingerprint,
                    "added": {cid: keys for cid, keys in applied["added"].items() if cid in binding.outputs},
                    "primedExamples": {cid: applied["primed_examples"][cid] for cid in binding.outputs},
                    "primingAdditions": [
                        {
                            "addedBy": row["added_by"],
                            "examples": {cid: ids for cid, ids in row["examples"].items() if cid in binding.outputs},
                        }
                        for row in engine.priming_additions()
                        if set(row["examples"]) & set(binding.outputs)
                    ],
                }
            else:
                result = await lab.command(
                    actor, publication, "decide", {"bindingRevision": binding.revision, "item": item}, execution_guard=fresh
                )
            status = await lab.command(actor, publication, "status", {}, execution_guard=fresh)
        await fresh()
        return RuntimeCommand({"response": result, "statusSnapshot": status})


def install_publication_routes(app, runtime):
    async def access(request, publication, operation):
        account = request.headers.get("X-Cyclotron-Account")
        try:
            uuid(account)
        except (ValueError, TypeError):
            raise HTTPException(400, "explicit account pin required") from None
        session = await (runtime.gateway._csrf(request) if operation == "console:write" else runtime.gateway._require(request))
        await asyncio.to_thread(runtime.accounts.authorize, session.access_token, account, session.person_id, operation)
        target, binding = target_for(runtime, account, publication)
        store = runtime.console(account)
        run = await asyncio.to_thread(store.run, target.run_id)
        meta = envelope(target, binding, run["config"])
        return session, target, binding, store, meta

    @app.post("/publications/{publication}/operations")
    async def operation(publication: str, request: Request):
        content = await request.body()
        if len(content) > min(MAX_BYTES, 65536):
            raise HTTPException(413, "bounded publication request required")
        try:
            body = json.loads(content)
            if set(body) != {"contractVersion", "operation", "payload"} or body["contractVersion"] != CONTRACT:
                raise ValueError()
            payload = body["payload"]
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, "publication contract required") from None
        if body["operation"] == "status":
            _, target, binding, store, meta = await access(request, publication, "console:read")
            if payload:
                raise HTTPException(400, "status takes an empty payload")
            if meta.get("pendingOutputs"):
                return meta
            for job in reversed(await asyncio.to_thread(store.jobs, target.run_id)):
                if (
                    job["kind"] == "publication"
                    and job["status"] == "completed"
                    and job["payload"].get("publication") == publication
                    and job["payload"].get("bindingRevision") == binding.revision
                ):
                    return job["result"]["statusSnapshot"]
            raise HTTPException(503, "no verified publication status snapshot yet")
        priming = body["operation"] == "prime"
        if body["operation"] not in ("decide", "prime"):
            raise HTTPException(503, "only publication prediction is activated; feedback is not applied")
        if not priming and not runtime.config.allow_live:
            raise HTTPException(503, "live publication predictions are disabled")
        session, target, binding, store, meta = await access(request, publication, "console:write")
        if set(payload) != {"bindingRevision", "listId" if priming else "item"} or payload["bindingRevision"] != binding.revision:
            raise HTTPException(409, "publication binding changed")
        if meta.get("pendingOutputs"):
            raise HTTPException(409, "publication criteria are pending")
        if priming:
            try:
                _, fingerprint = await asyncio.to_thread(
                    priming_snapshot, store, target, binding, store.run(target.run_id)["config"], payload["listId"]
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            command = {
                "publication": publication,
                "bindingRevision": binding.revision,
                "priming": {"listId": payload["listId"], "fingerprint": fingerprint},
            }
        else:
            item = payload["item"]
            if not isinstance(item, dict) or set(item) != {"id", "values"}:
                raise HTTPException(400, "frozen structured source required")
            items = await asyncio.to_thread(store.items, target.run_id)
            if not any(row["id"] == item["id"] and row["values"] == item["values"] for row in items):
                raise HTTPException(409, "source does not match the canonical run input")
            command = {"publication": publication, **payload}
        identity = "publication:" + sha256(encode([target.account_id, target.run_id, command])).hexdigest()
        verified = VerifiedAccountSession(
            session.access_token, session.person_id, target.account_id, session_ref=session.session_ref
        )
        job = await runtime.workspace(target.account_id).service.submit_command(
            verified, target.run_id, identity, "publication", command
        )
        return JSONResponse({**meta, "jobId": job["id"], "status": job["status"]}, status_code=202)

    @app.get("/publications/{publication}/commands/{job_id}")
    async def receipt(publication: str, job_id: str, request: Request):
        _, target, binding, store, meta = await access(request, publication, "console:read")
        try:
            uuid(job_id)
            job, actor = await asyncio.to_thread(store.command_envelope, job_id)
        except ValueError:
            raise HTTPException(404, "unknown publication command") from None
        if (
            job["run_id"] != target.run_id
            or job["kind"] != "publication"
            or job["payload"].get("publication") != publication
            or job["payload"].get("bindingRevision") != binding.revision
        ):
            raise HTTPException(404, "unknown publication command")
        if job["status"] == "completed":
            return job["result"]["response"]
        if job["status"] not in ("pending", "running"):
            return JSONResponse({**meta, "code": "reconciliation_required"}, status_code=409)
        return JSONResponse({**meta, "jobId": job_id, "status": job["status"]}, status_code=202)
