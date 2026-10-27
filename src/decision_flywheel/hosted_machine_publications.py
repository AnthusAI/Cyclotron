"""Machine routes for standing publication targets (SigV4 service principals, no human session).

``POST /machine/publications/{publication}/operations`` accepts ``decide``, ``status`` and ``feedback``.
``GET  /machine/publications/{publication}/commands/{jobId}`` polls a queued ``decide``.
``POST /machine/decisions`` queues one ``decide`` answering several publications of a standing target.
``GET  /machine/decisions/{jobId}`` polls it and returns every requested publication's projection.

The caller identity comes only from the API Gateway IAM context. Account and run are derived from the
trusted target configuration; the account header must merely select a configured target. A machine
caller can never prime, bootstrap, grant, change settings or reach a non-standing target.
"""

import asyncio
import json
import re
from hashlib import sha256

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .hosted_publications import (
    QUEUE_REFUSALS,
    configured_targets,
    envelope,
    queue_fields,
    queue_refusal,
    receipt_response,
    status_snapshot,
    target_for,
)
from .journal import uuid
from .machine_access import MachineActor, MachineDenied, verified_caller
from .prospectus_accounts import AccountAccessDenied
from .publication_lab import CONTRACT
from .standing_publications import validate_item
from .storage_errors import StorageMissing, StorageUncertain

REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{8,128}")
FEEDBACK_KEYS = {"bindingRevision", "itemId", "itemRevision", "output", "label", "editor"}


def _text(value, name, limit, *, required=True):
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise HTTPException(400, f"{name} must be bounded text")
    return value


def install_machine_routes(app, runtime):
    def resolve(request, publication):
        try:
            role, session = verified_caller(request.scope, runtime.machine_principals)
        except MachineDenied:
            raise HTTPException(403, "machine access refused") from None
        try:
            account = uuid(request.headers.get("X-Cyclotron-Account"))
        except (ValueError, TypeError):
            raise HTTPException(400, "explicit account pin required") from None
        try:
            target, binding = target_for(runtime, account, publication)
        except AccountAccessDenied:
            raise HTTPException(403, "machine access refused") from None
        if not target.standing:
            raise HTTPException(403, "machine access refused")
        return role, session, account, target, binding

    @app.post("/machine/publications/{publication}/operations")
    async def operation(publication: str, request: Request):
        role, session, account, target, binding = resolve(request, publication)
        content = await request.body()
        if len(content) > 65536:
            raise HTTPException(413, "bounded publication request required")
        try:
            body = json.loads(content)
            name = body["operation"]
            keys = {"contractVersion", "operation", "payload"}
            if name != "status":
                keys.add("requestId")
            if (
                set(body) != keys
                or body["contractVersion"] != CONTRACT
                or name not in ("decide", "status", "feedback")
                or not isinstance(body["payload"], dict)
            ):
                raise ValueError()
            if name != "status" and (not isinstance(body["requestId"], str) or not REQUEST_ID.fullmatch(body["requestId"])):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, "publication contract with a machine request id required") from None
        payload = body["payload"]
        store = runtime.console(account)
        try:
            run = await asyncio.to_thread(store.run, target.run_id)
        except (ValueError, StorageMissing):
            raise HTTPException(409, "standing run unavailable") from None
        meta = envelope(target, binding, run["config"])
        if name == "status":
            if payload:
                raise HTTPException(400, "status takes an empty payload")
            if meta.get("pendingOutputs"):
                return meta
            snapshot = await asyncio.to_thread(status_snapshot, store, target, binding, publication)
            if snapshot is None:
                raise HTTPException(503, "no verified publication status snapshot yet")
            return snapshot
        if meta.get("pendingOutputs"):
            raise HTTPException(409, "publication criteria are pending")
        if name == "feedback":
            return await feedback(role, session, account, target, binding, run, store, body["requestId"], payload)
        if not runtime.config.allow_live:
            raise HTTPException(503, "live publication predictions are disabled")
        if set(payload) != {"bindingRevision", "item"} or payload["bindingRevision"] != binding.revision:
            raise HTTPException(409, "publication binding changed")
        try:
            inputs = [row["config"].get("input_field", "text") for row in run["config"]["classifiers"]]
            item_id, values = validate_item(payload["item"], inputs)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        command = {"publication": publication, "bindingRevision": binding.revision, "item": {"id": item_id, "values": values}}
        actor = MachineActor(
            role, session, account, target.run_id, await asyncio.to_thread(runtime.binding, account, target.run_id)
        )
        try:
            job = await runtime.workspace(account).service.submit_machine_command(
                actor, "machine:" + body["requestId"], "publication", command
            )
        except AccountAccessDenied:
            raise HTTPException(403, "machine access refused") from None
        except QUEUE_REFUSALS as error:
            return queue_refusal(error, meta)
        except ValueError as exc:
            return JSONResponse({**meta, "code": "request_conflict", "message": str(exc)}, status_code=409)
        return JSONResponse({**meta, "jobId": job["id"], "status": job["status"]}, status_code=202)

    async def feedback(role, session, account, target, binding, run, store, request_id, payload):
        try:
            if not (set(payload) == FEEDBACK_KEYS or set(payload) == FEEDBACK_KEYS | {"comment"}):
                raise HTTPException(400, "feedback payload fields are fixed")
            if payload["bindingRevision"] != binding.revision:
                raise HTTPException(409, "publication binding changed")
            editor = payload["editor"]
            if not isinstance(editor, dict) or set(editor) != {"site", "subject"}:
                raise HTTPException(400, "asserted editor requires site and subject")
            site, subject = _text(editor["site"], "editor site", 100), _text(editor["subject"], "editor subject", 200)
            item_id = _text(payload["itemId"], "item id", 200)
            output, label = _text(payload["output"], "output", 200), _text(payload["label"], "label", 200)
            comment = _text(payload.get("comment", ""), "comment", 4000, required=False)
            revision = payload["itemRevision"]
            if type(revision) is not int or revision < 1:
                raise HTTPException(400, "item revision must be a positive integer")
            if output not in binding.outputs:
                raise HTTPException(403, "output is not part of this publication binding")
            classifier = next((c for c in run["config"]["classifiers"] if c["id"] == output), None)
            if classifier is None:
                raise HTTPException(409, "publication output has no runnable classifier")
            event = {
                "kind": "publication-machine-feedback",
                "publicationId": binding.publication_id,
                "bindingRevision": binding.revision,
                "output": output,
                "itemId": item_id,
                "itemRevision": revision,
                "label": label,
                "comment": comment,
                "classifierId": output,
                "classifierRevision": classifier["revision"],
                "machine": {"principal": role, "session": session},
                "editor": {"site": site, "subject": subject, "asserted": True},
                "requestId": request_id,
            }
            identity = "machine-feedback:" + sha256(f"{target.run_id}:{request_id}".encode()).hexdigest()
            vote = await asyncio.to_thread(
                store.record_machine_feedback,
                target.run_id,
                output,
                classifier["revision"],
                item_id,
                revision,
                label,
                comment,
                identity,
                event,
            )
        except StorageUncertain:
            raise HTTPException(503, "feedback outcome uncertain; retry the same request id") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {
            "contractVersion": CONTRACT,
            "accountId": account,
            "publicationId": binding.publication_id,
            "bindingRevision": binding.revision,
            "operation": "feedback",
            "status": "recorded",
            "requestId": request_id,
            "output": output,
            "itemId": item_id,
            "itemRevision": revision,
            "label": vote["label"],
            "recordedAs": "human-label",
            "asserted": {"machine": role, "editor": {"site": site, "subject": subject}},
        }

    @app.get("/machine/publications/{publication}/commands/{job_id}")
    async def receipt(publication: str, job_id: str, request: Request):
        _, _, account, target, binding = resolve(request, publication)
        store = runtime.console(account)
        try:
            uuid(job_id)
            job, actor = await asyncio.to_thread(store.command_envelope, job_id)
            run = await asyncio.to_thread(store.run, target.run_id)
        except (ValueError, StorageMissing):
            raise HTTPException(404, "unknown publication command") from None
        if (
            not isinstance(actor, MachineActor)
            or job["run_id"] != target.run_id
            or job["kind"] != "publication"
            or job["payload"].get("publication") != publication
            or job["payload"].get("bindingRevision") != binding.revision
        ):
            raise HTTPException(404, "unknown publication command")
        return receipt_response(job, envelope(target, binding, run["config"]), await asyncio.to_thread(queue_fields, store, job))

    def resolve_account(request):
        try:
            role, session = verified_caller(request.scope, runtime.machine_principals)
        except MachineDenied:
            raise HTTPException(403, "machine access refused") from None
        try:
            account = uuid(request.headers.get("X-Cyclotron-Account"))
        except (ValueError, TypeError):
            raise HTTPException(400, "explicit account pin required") from None
        return role, session, account

    def decisions_meta(account, target, bindings):
        return {"contractVersion": CONTRACT, "accountId": account, "runId": target.run_id, "bindings": bindings}

    @app.post("/machine/decisions")
    async def decisions(request: Request):
        role, session, account = resolve_account(request)
        content = await request.body()
        if len(content) > 65536:
            raise HTTPException(413, "bounded publication request required")
        try:
            body = json.loads(content)
            payload = body["payload"]
            if (
                set(body) != {"contractVersion", "requestId", "payload"}
                or body["contractVersion"] != CONTRACT
                or not isinstance(body["requestId"], str)
                or not REQUEST_ID.fullmatch(body["requestId"])
                or not isinstance(payload, dict)
                or set(payload) != {"bindings", "item"}
                or not isinstance(payload["bindings"], dict)
                or not 1 <= len(payload["bindings"]) <= 100
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in payload["bindings"].items())
            ):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, "publication contract with a machine request id required") from None
        bindings = payload["bindings"]
        standing = [t for t in configured_targets(runtime) if t.account_id == account and t.standing]
        if not standing:
            raise HTTPException(403, "machine access refused")
        target = next((t for t in standing if set(bindings) <= {b.publication_id for b in t.bindings}), None)
        if target is None:
            raise HTTPException(409, "publication binding changed")
        known = {b.publication_id: b for b in target.bindings}
        store = runtime.console(account)
        try:
            run = await asyncio.to_thread(store.run, target.run_id)
        except (ValueError, StorageMissing):
            raise HTTPException(409, "standing run unavailable") from None
        if any(known[name].revision != revision for name, revision in bindings.items()):
            raise HTTPException(409, "publication binding changed")
        meta = decisions_meta(account, target, bindings)
        if any(set(known[name].outputs) & set(target.pending_outputs) for name in bindings):
            raise HTTPException(409, "publication criteria are pending")
        if not runtime.config.allow_live:
            raise HTTPException(503, "live publication predictions are disabled")
        for name in bindings:
            envelope(target, known[name], run["config"])
        try:
            inputs = [row["config"].get("input_field", "text") for row in run["config"]["classifiers"]]
            item_id, values = validate_item(payload["item"], inputs)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        command = {"bindings": bindings, "item": {"id": item_id, "values": values}}
        actor = MachineActor(
            role, session, account, target.run_id, await asyncio.to_thread(runtime.binding, account, target.run_id)
        )
        try:
            job = await runtime.workspace(account).service.submit_machine_command(
                actor, "machine:" + body["requestId"], "publication", command
            )
        except AccountAccessDenied:
            raise HTTPException(403, "machine access refused") from None
        except QUEUE_REFUSALS as error:
            return queue_refusal(error, meta)
        except ValueError as exc:
            return JSONResponse({**meta, "code": "request_conflict", "message": str(exc)}, status_code=409)
        return JSONResponse({**meta, "jobId": job["id"], "status": job["status"]}, status_code=202)

    @app.get("/machine/decisions/{job_id}")
    async def decisions_receipt(job_id: str, request: Request):
        _, _, account = resolve_account(request)
        store = runtime.console(account)
        try:
            uuid(job_id)
            job, actor = await asyncio.to_thread(store.command_envelope, job_id)
        except (ValueError, StorageMissing):
            raise HTTPException(404, "unknown publication decision") from None
        payload = job["payload"]
        target = next((t for t in configured_targets(runtime) if t.account_id == account and t.run_id == job["run_id"]), None)
        if (
            target is None
            or not target.standing
            or not isinstance(actor, MachineActor)
            or job["kind"] != "publication"
            or set(payload) != {"bindings", "item"}
        ):
            raise HTTPException(404, "unknown publication decision")
        meta = decisions_meta(account, target, payload["bindings"])
        if job["status"] == "completed":
            return {
                "contractVersion": CONTRACT,
                "accountId": account,
                "runId": target.run_id,
                "jobId": job["id"],
                **job["result"]["response"],
            }
        return receipt_response(job, meta, await asyncio.to_thread(queue_fields, store, job))
