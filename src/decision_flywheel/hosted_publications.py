"""Publication projections over the existing original-session queue and S3 SDK.

Only prediction commands are enabled here. Targets are trusted application
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
from .prospectus_accounts import AccountAccessDenied, VerifiedAccountSession
from .publication_lab import CONTRACT, MAX_BYTES, PublicationActor, PublicationBinding, PublicationLab
from .s3_embedded_storage import S3CyclotronStores


@dataclass(frozen=True)
class PublicationTarget:
    account_id: str
    run_id: str
    bindings: tuple[PublicationBinding, ...]

    def __post_init__(self):
        uuid(self.account_id)
        uuid(self.run_id)
        object.__setattr__(self, "bindings", tuple(self.bindings))
        if (
            not self.bindings
            or any(not isinstance(b, PublicationBinding) for b in self.bindings)
            or len({b.publication_id for b in self.bindings}) != len(self.bindings)
        ):
            raise ValueError("distinct configured publication bindings required")
        outputs = [output for b in self.bindings for output in b.outputs]
        if len(set(outputs)) != len(outputs):
            raise ValueError("publication feedback outputs must be distinct")


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
            if set(target) != {"accountId", "runId", "bindings"}:
                raise ValueError()
            bindings = []
            for row in target["bindings"]:
                if set(row) != {"publicationId", "outputs", "revision"} or not isinstance(row["outputs"], list):
                    raise ValueError()
                bindings.append(PublicationBinding(row["publicationId"], tuple(row["outputs"]), row["revision"]))
            result.append(PublicationTarget(target["accountId"], target["runId"], tuple(bindings)))
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
    if any(output not in specs for b in target.bindings for output in b.outputs):
        raise ValueError("publication binding names an unavailable run output")
    return {
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
        ],
    }


class PublicationCommandRuntime:
    def __init__(self, host, view, guard, check, actor):
        self.host, self.view, self.guard, self.check, self.actor = host, view, guard, check, actor

    def open_session(self, run_id, config, items, shown):
        # Opening a console command does not open provider/storage state yet.
        return SimpleNamespace(run_id=run_id, config=config, close=lambda: None)

    async def execute(self, session, kind, payload, shown, config, *, request_id=None):
        if kind != "publication" or set(payload) != {"publication", "bindingRevision", "item"}:
            raise ValueError("supported publication prediction command required")
        publication = payload["publication"]
        target, binding = target_for(self.host, self.actor.account_id, publication)
        if target.run_id != session.run_id or binding.revision != payload["bindingRevision"]:
            raise AccountAccessDenied("publication run or binding changed")
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

        async def fresh():
            await self.guard()
            if target_for(self.host, self.actor.account_id, publication) != (target, binding):
                raise AccountAccessDenied("publication target changed")
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
        model, optimizer = self.host.model_factory(config)
        if getattr(optimizer, "enabled", False):
            raise ValueError("baseline prediction requires optimizer disabled")
        name = "publication-" + sha256(config["cyclotron_id"].encode()).hexdigest()

        def journal(domain, key):
            # Reuse the existing worker's reviewed storage domains. No new IAM
            # prefix and no cloud SQLite or alternate authority is introduced.
            return self.host.journal(self.actor.account_id, "shared-batches" if domain == "shared" else domain, key)

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
            max_requests=config["max_requests"],
        ) as engine:
            lab = PublicationLab(engine, account_id=self.actor.account_id, bindings=target.bindings)
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
            for job in reversed(await asyncio.to_thread(store.jobs, target.run_id)):
                if (
                    job["kind"] == "publication"
                    and job["status"] == "completed"
                    and job["payload"].get("publication") == publication
                    and job["payload"].get("bindingRevision") == binding.revision
                ):
                    return job["result"]["statusSnapshot"]
            raise HTTPException(503, "no verified publication status snapshot yet")
        if body["operation"] != "decide":
            raise HTTPException(503, "only publication prediction is activated; feedback is not applied")
        if not runtime.config.allow_live:
            raise HTTPException(503, "live publication predictions are disabled")
        session, target, binding, store, meta = await access(request, publication, "console:write")
        if set(payload) != {"bindingRevision", "item"} or payload["bindingRevision"] != binding.revision:
            raise HTTPException(409, "publication binding changed")
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
