"""Authenticated canonical run snapshots without provider execution authority."""

import asyncio
import hmac
import json
from hashlib import sha256

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .cyclotron_runtime import CyclotronRuntime
from .hosted_publications import _GuardedJournal
from .journal import encode, uuid
from .prospectus_accounts import AccountAccessDenied
from .publication_lab import CONTRACT, DECISION_CONTRACT, in_contract
from .s3_console_storage import S3ConsoleStorage
from .storage_errors import StorageConflict, StorageMissing, StorageUncertain

# Where a frozen run records the bootstrap request that made it. Stored runs are never rewritten (the workspace
# binding is a hash of the config), so both keys are read; ``WRITTEN_KEY`` is the one new runs get.
BOOTSTRAP_KEYS = ("standing_bootstrap", "publication_bootstrap")
WRITTEN_KEY = "standing_bootstrap"
WRITTEN_KIND = "standing-bootstrap"


class BootstrapConflict(ValueError):
    pass


def bootstrap_of(config):
    return next((config[key] for key in BOOTSTRAP_KEYS if key in config), {})


def freeze_run(runtime, account, session, body):
    def check():
        current = runtime.sessions.get_ref(session.session_ref)
        if (
            current is None
            or current.person_id != session.person_id
            or current.subject != session.subject
            or not hmac.compare_digest(current.access_token, session.access_token)
            or runtime.verifier.access(current.access_token).get("sub") != current.subject
            or runtime.identities.me(current.access_token).get("id") != current.person_id
        ):
            raise AccountAccessDenied("original bootstrap session changed")
        runtime.accounts.authorize(current.access_token, account, current.person_id, "console:write")

    check()
    store = S3ConsoleStorage(_GuardedJournal(runtime.journal(account, "console", "catalog"), check))
    signature = sha256(encode([account, body["name"], body["config"]])).hexdigest()
    with store.transaction():
        # Retry checks precede latest resolution: a later catalog revision must
        # never turn an uncertain successful request into a second frozen run.
        for run in store.runs():
            bootstrap = bootstrap_of(run["config"])
            if bootstrap.get("requestId") == body["requestId"]:
                if bootstrap.get("fingerprint") != signature or bootstrap.get("createdBy") != session.person_id:
                    raise BootstrapConflict("bootstrap request identity has different content or person")
                check()
                return receipt(account, run), False
        config = body["config"]
        definition = store.cyclotron_definition(config["cyclotron_id"])
        if definition["revision"] != config["cyclotron_definition_revision"] or any(
            store.classifier(row["id"])["revision"] != row["revision"] for row in definition["classifiers"]
        ):
            raise BootstrapConflict("review current canonical classifier and definition revisions")

        def unavailable(*args, **kwargs):
            raise ValueError("bootstrap cannot construct a provider or execution store")

        normalizer = CyclotronRuntime(
            store, None, model_factory=unavailable, sink_factory=unavailable, storage_factory=unavailable
        )
        frozen, items = normalizer.normalize_run_config({**config, "max_requests": 1, "max_optimizer_calls": 1})
        frozen[WRITTEN_KEY] = {
            "requestId": body["requestId"],
            "fingerprint": signature,
            "createdBy": session.person_id,
        }
        # 'live' is the existing run data schema required by the command store,
        # not dispatch permission. Host allow_live remains independently false.
        run = store.create_run(
            body["name"],
            "live",
            frozen,
            items=items,
            initial_events=[
                (
                    WRITTEN_KIND + ":" + body["requestId"],
                    {
                        "kind": WRITTEN_KIND,
                        "accountId": account,
                        "createdBy": session.person_id,
                        "requestId": body["requestId"],
                        "configFingerprint": sha256(encode(frozen)).hexdigest(),
                    },
                )
            ],
        )
    return receipt(account, run), True


def receipt(account, run):
    return {
        "contractVersion": CONTRACT,
        "accountId": account,
        "runId": run["id"],
        "createdBy": bootstrap_of(run["config"])["createdBy"],
        "configFingerprint": sha256(encode(run["config"])).hexdigest(),
        "definitionRevision": run["config"]["cyclotron_definition_revision"],
    }


def install_bootstrap_route(app, runtime):
    # The lab's CDN forwards /api/* to this service but not /publication-runs/*, so the same handler is also
    # served under /api/ for signed-in browsers that reach the lab through its public address. The standing-runs
    # path runs it in the decision contract.
    @app.post("/publication-runs/bootstrap")
    @app.post("/api/publication-runs/bootstrap")
    async def bootstrap(request: Request):
        return await freeze(request, CONTRACT)

    @app.post("/api/standing-runs/bootstrap")
    async def standing_bootstrap(request: Request):
        return await freeze(request, DECISION_CONTRACT)

    async def freeze(request, contract):
        raw = await request.body()
        try:
            body = json.loads(raw)
            account = uuid(request.headers.get("X-Cyclotron-Account"))
            if (
                not isinstance(body, dict)
                or set(body) != {"contractVersion", "requestId", "name", "config"}
                or body["contractVersion"] != contract
                or not isinstance(body["name"], str)
                or not body["name"].strip()
                or not isinstance(body["config"], dict)
                or set(body["config"]) != {"cyclotron_id", "cyclotron_definition_revision", "item_list_id"}
                or type(body["config"]["cyclotron_definition_revision"]) is not int
                or body["config"]["cyclotron_definition_revision"] < 1
                or any(not isinstance(body["config"][k], str) or not body["config"][k] for k in ("cyclotron_id", "item_list_id"))
            ):
                raise ValueError()
            uuid(body["requestId"])
        except (ValueError, TypeError):
            raise HTTPException(400, "canonical bootstrap identity and configuration required") from None
        session = await runtime.gateway._csrf(request)
        if session.access_token.encode() in raw:
            raise HTTPException(400, "credential-free bootstrap request required")
        try:
            result, created = await asyncio.to_thread(freeze_run, runtime, account, session, body)
        except (BootstrapConflict, StorageConflict, StorageMissing):
            raise HTTPException(409, "bootstrap identity or catalog changed; inspect and retry the same request") from None
        except StorageUncertain:
            raise HTTPException(503, "bootstrap outcome uncertain; retry only the same request identity") from None
        except ValueError:
            raise HTTPException(400, "canonical bootstrap configuration refused") from None
        return JSONResponse(in_contract(contract, result), status_code=201 if created else 200)
