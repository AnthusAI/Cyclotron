"""Publication projections over the existing original-session queue and S3 SDK.

Prediction and accepted-human priming use distinct, validated payloads. Targets are trusted application
configuration, not membership grants. The BFF never opens a model or engine.
"""

import asyncio
import json
from dataclasses import dataclass, field
from hashlib import sha256
from types import SimpleNamespace

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .application_runtime import RuntimeCommand
from .command_refusal import CommandRefused
from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from .hosted_command_worker import AuthorizedModel
from .journal import encode, uuid
from .machine_access import MachineActor
from .optimizer_agent import DisabledOptimizer
from .priming_context import catalog_priming, decode_priming
from .prospectus_accounts import AccountAccessDenied, VerifiedAccountSession
from .publication_lab import CONTRACT, MAX_BYTES, PublicationActor, PublicationBinding, PublicationLab
from .s3_console_storage import StandingCapacity
from .s3_embedded_storage import S3CyclotronStores
from .standing_publications import DEFAULT_MAX_ITEMS, RequestCeiling, today, validate_item


@dataclass(frozen=True)
class PublicationTarget:
    account_id: str
    run_id: str
    bindings: tuple[PublicationBinding, ...]
    pending_outputs: tuple[str, ...] = ()
    model_identity: str | None = None
    # Standing runs accept streaming sources and a trusted cumulative request allowance.
    # Only operator configuration can set these; no request field can.
    standing: bool = False
    request_ceiling: RequestCeiling | None = None
    max_items: int | None = None

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
        if type(self.standing) is not bool:
            raise ValueError("standing must be a boolean")
        if self.standing:
            if not isinstance(self.request_ceiling, RequestCeiling):
                raise ValueError("a standing target requires an explicit request ceiling")
            if self.max_items is None:
                object.__setattr__(self, "max_items", DEFAULT_MAX_ITEMS)
            if type(self.max_items) is not int or not 1 <= self.max_items <= 100000:
                raise ValueError("standing item capacity must be a bounded positive integer")
        elif self.request_ceiling is not None or self.max_items is not None:
            raise ValueError("request ceiling and item capacity belong only to standing targets")


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
                "standing",
                "requestCeiling",
                "maxItems",
            }:
                raise ValueError()
            ceiling = target.get("requestCeiling")
            if ceiling is not None:
                if not isinstance(ceiling, dict) or "total" not in ceiling or set(ceiling) - {"total", "perDay", "since"}:
                    raise ValueError()
                ceiling = RequestCeiling(ceiling["total"], ceiling.get("perDay"), ceiling.get("since"))
            pending = target.get("pendingOutputs", [])
            if not isinstance(pending, list) or any(not isinstance(output, str) for output in pending):
                raise ValueError()
            bindings = []
            for row in target["bindings"]:
                if (
                    not isinstance(row, dict)
                    or not {"publicationId", "outputs", "revision"} <= set(row)
                    or set(row) - {"publicationId", "outputs", "revision", "priming"}
                    or not isinstance(row["outputs"], list)
                ):
                    raise ValueError()
                priming = row.get("priming")
                if "priming" in row and (
                    not isinstance(priming, dict)
                    or set(priming) != {"listId"}
                    or not isinstance(priming["listId"], str)
                    or not priming["listId"].strip()
                    or len(priming["listId"]) > 200
                ):
                    raise ValueError()
                bindings.append(
                    PublicationBinding(
                        row["publicationId"],
                        tuple(row["outputs"]),
                        row["revision"],
                        priming["listId"] if priming else None,
                    )
                )
            result.append(
                PublicationTarget(
                    target["accountId"],
                    target["runId"],
                    tuple(bindings),
                    tuple(pending),
                    target.get("modelIdentity"),
                    target.get("standing", False),
                    ceiling,
                    target.get("maxItems"),
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


@dataclass(frozen=True)
class BearerActor:
    """A caller whose authority is a bearer credential re-verified on every check.

    reauthorize is an async callable that raises when the account service no longer
    allows console:write for this person. The credential itself is never stored here.
    """

    account_id: str
    person_id: str
    reauthorize: object = field(repr=False, compare=False)


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
        machine = isinstance(self.actor, MachineActor)
        if not priming and (kind != "publication" or set(payload) != {"publication", "bindingRevision", "item"}):
            raise ValueError("supported publication prediction command required")
        publication = payload["publication"]
        target, binding = target_for(self.host, self.actor.account_id, publication)
        if machine and (priming or not target.standing):
            raise AccountAccessDenied("machine callers may only decide on standing targets")
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
            if not target.standing:
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
            if machine:
                # Authority is the trusted target configuration re-checked in check_scope; there is no
                # human session or Prospectus membership behind a service principal.
                return
            if isinstance(self.actor, BearerActor):
                await self.actor.reauthorize()
                return
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
        # The provider credential is resolved here, before any state is registered or any request is reserved.
        model, optimizer = (
            (_PrimingModel(target.model_identity), DisabledOptimizer()) if priming else self.host.model_factory(config)
        )
        if target.model_identity is not None and model.model_identity != target.model_identity:
            raise ValueError("provider differs from reviewed publication model identity")
        if getattr(optimizer, "enabled", False):
            raise ValueError("baseline prediction requires optimizer disabled")
        registration, max_requests = None, 1 if priming else config["max_requests"]
        if target.standing and not priming:
            max_requests = target.request_ceiling.allowed(today())
            if max_requests < 1:
                raise CommandRefused("request_ceiling_reached", "the standing request allowance has not started or is exhausted")
            try:
                item_id, values = validate_item(item, [spec.input_field for spec in specs])
                registration = await asyncio.to_thread(
                    self.view.register_standing_item, session.run_id, item_id, values, max_items=target.max_items
                )
            except StandingCapacity as exc:
                raise CommandRefused("standing_capacity_reached", str(exc)) from None
            except ValueError as exc:
                # Includes priming/evaluation overlap. Nothing was registered and no request was reserved.
                raise CommandRefused("source_refused", str(exc)) from None
            item = {"id": item_id, "values": values}
        name = "publication-" + sha256(config["cyclotron_id"].encode()).hexdigest()

        def journal(domain, key):
            # Reuse the existing worker's reviewed storage domains. No new IAM
            # prefix and no cloud SQLite or alternate authority is introduced.
            journal = self.host.journal(self.actor.account_id, "shared-batches" if domain == "shared" else domain, key)
            return _GuardedJournal(journal, check_scope) if priming else journal

        stores = S3CyclotronStores(journal("workspace", name), journal_for=journal, max_records=self.host.config.max_records)
        actor = PublicationActor(
            self.actor.account_id,
            self.actor.service_id if machine else self.actor.person_id,
            frozenset((publication, cap) for cap in ("decide", "read")),
        )
        definition = CyclotronDefinition(config["cyclotron_id"], tuple(specs), seed=config["seed"])
        with Cyclotron.open(
            None,
            definition,
            AuthorizedModel(model, fresh),
            optimizer,
            storage=stores,
            seed_rubrics=rubrics,
            max_requests=max_requests,
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
                try:
                    result = await lab.command(
                        actor, publication, "decide", {"bindingRevision": binding.revision, "item": item}, execution_guard=fresh
                    )
                except RuntimeError as exc:
                    if "explicit retry required" in str(exc):
                        # Raised while reserving the shared request, before any dispatch: an earlier attempt at
                        # this exact decision failed and only an operator may retry it. A terminal receipt keeps
                        # the run free; leaving the job running would block every other item.
                        raise CommandRefused(
                            "source_refused",
                            "an earlier attempt at this exact decision failed and needs an operator retry; "
                            "no provider call was made",
                        ) from None
                    if "request ceiling reached" not in str(exc):
                        raise
                    raise CommandRefused(
                        "request_ceiling_reached", "the request ceiling was reached; no provider call was made"
                    ) from None
                if registration is not None:
                    result = {**result, "source": registration}
            status = await lab.command(actor, publication, "status", {}, execution_guard=fresh)
        await fresh()
        return RuntimeCommand({"response": result, "statusSnapshot": status})


REFUSAL_STATUS = {
    "provider_key_unavailable": 503,
    "request_ceiling_reached": 429,
    "standing_capacity_reached": 507,
    "source_refused": 409,
}


def status_snapshot(store, target, binding, publication):
    """Newest verified completed worker snapshot for this binding, or None."""
    for job in reversed(store.jobs(target.run_id)):
        if (
            job["kind"] == "publication"
            and job["status"] == "completed"
            and job["payload"].get("publication") == publication
            and job["payload"].get("bindingRevision") == binding.revision
        ):
            return job["result"]["statusSnapshot"]
    return None


def receipt_response(job, meta):
    """Durable job state as a publication receipt; refusals carry their public code, never details."""
    if job["status"] == "completed":
        return job["result"]["response"]
    if job["status"] in ("pending", "running"):
        return JSONResponse({**meta, "jobId": job["id"], "status": job["status"]}, status_code=202)
    refusal = (job.get("result") or {}).get("refusal") if job["status"] == "failed" else None
    if isinstance(refusal, dict) and isinstance(refusal.get("code"), str):
        return JSONResponse(
            {**meta, "jobId": job["id"], "status": "failed", "code": refusal["code"], "message": refusal.get("message")},
            status_code=REFUSAL_STATUS.get(refusal["code"], 409),
        )
    return JSONResponse({**meta, "code": "reconciliation_required"}, status_code=409)


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
            snapshot = await asyncio.to_thread(status_snapshot, store, target, binding, publication)
            if snapshot is None:
                raise HTTPException(503, "no verified publication status snapshot yet")
            return snapshot
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
        return receipt_response(job, meta)
