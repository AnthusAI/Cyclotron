"""Bearer-authenticated curation routes for a calling site's reference import.

A site (Papyrus) forwards the editor's account-service access token with three scope
pins. The lab re-verifies that token with the account service on every request through
an injected authority, so a revoked membership stops working at once. The account that
the service authorizes must equal the pinned account, and the publication must be a
trusted configured binding of that account. Nothing here trusts a header as identity.

Routes (slice 1 is file import only; there is no hosted transformer):

    GET  /api/newsroom/curation/context      console:read
    POST /api/newsroom/curation/publication  console:read (status) / console:write (decide)
    POST /api/newsroom/curation/priming      console:read (queries) / console:write (mutations)

Priming runs on the account's existing S3 catalog through a publication-scoped view:
draft identities are namespaced per publication, and only the binding's configured
list and classifier revisions are visible. Accepting examples applies them to the
shared cyclotron's priming context in the same request, with no model or optimizer.
"""

import asyncio
import inspect
import json
import re
from dataclasses import dataclass
from hashlib import sha256
from types import SimpleNamespace

import strawberry
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from graphql import NoSchemaIntrospectionCustomRule, OperationDefinitionNode, OperationType, parse
from graphql.error import GraphQLError
from strawberry.extensions import AddValidationRules
from strawberry.scalars import JSON

from .decision_kinds import WRITTEN
from .hosted_publications import (
    BearerActor,
    PublicationCommandRuntime,
    envelope,
    priming_snapshot,
    status_snapshot,
    target_for,
)
from .journal import uuid
from .lab_verifier import unavailable_authority
from .priming import PrimingService
from .prospectus_accounts import AccountAccessDenied
from .publication_lab import CAPABILITIES, CONTRACT, PublicationError
from .site_credentials import VerifiedSiteCredential, bearer_token
from .storage_errors import StorageLimit

READ, WRITE = "console:read", "console:write"
ROUTE = "/api/newsroom/curation"
PRIMING_ACTIONS = {
    "file": {"identifier", "list_id", "refs", "kind", "content"},
    "transform": {"identifier", "list_id", "refs", "transformer_id", "inputs"},
    "poll": {"identifier"},
    "feedback": {"identifier", "version", "request_id", "reason", "inputs"},
    "cancel": {"identifier"},
    "accept": {"identifier", "version", "labels"},
}
# Operations whose behaviour is the existing hosted publication rules. Everything else is refused.
SUPPORTED_OPERATIONS = ("status", "decide")
NAMESPACED = re.compile(r"pub-[0-9a-f]{32}:")


PRIME_POLL_SECONDS = 2


class CurationError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class _NoTransformerClient:
    def stream(self, *args, **kwargs):
        raise CurationError(400, "no_hosted_transformer", "No hosted transformer is configured; import a file instead.")


async def _resolved(value):
    return await value if inspect.isawaitable(value) else value


def namespace_for(account, publication):
    return "pub-" + sha256(f"{account}\0{publication}".encode()).hexdigest()[:32] + ":"


class ScopedCatalog:
    """The only catalog surface the priming service sees: one list and one set of classifier revisions."""

    def __init__(self, store, list_id, refs):
        self.store, self.list_id, self.refs = store, list_id, tuple((row["id"], row["revision"]) for row in refs)

    def item_lists(self):
        return [row for row in self.store.item_lists() if row["id"] == self.list_id]

    def classifier(self, identifier, revision=None):
        if (identifier, revision) not in self.refs:
            raise ValueError("classifier is outside the selected publication")
        return self.store.classifier(identifier, revision)


class ScopedPrimingStorage:
    """Publication-scoped drafts and accepted evidence over one account's priming storage.

    Draft identities are stored under a per-publication prefix, and every list or classifier
    argument is checked against the binding before it reaches the account's transaction.
    """

    def __init__(self, storage, prefix, list_id, classifier_ids):
        self.storage, self.prefix, self.list_id, self.classifier_ids = storage, prefix, list_id, frozenset(classifier_ids)

    def transaction(self):
        return _ScopedTransaction(self, self.storage.transaction())

    def accepted_records(self, list_id, classifiers):
        self.check_list(list_id)
        for row in classifiers:
            self.check_classifier(row["id"])
        return self.storage.accepted_records(list_id, classifiers)

    def check_list(self, list_id):
        if list_id != self.list_id:
            raise ValueError("priming list is outside the selected publication")

    def check_classifier(self, identifier):
        if identifier not in self.classifier_ids:
            raise ValueError("classifier is outside the selected publication")


class _ScopedTransaction:
    def __init__(self, scope, context):
        self.scope, self.context, self.tx = scope, context, None

    def __enter__(self):
        self.tx = self.context.__enter__()
        return self

    def __exit__(self, *error):
        return self.context.__exit__(*error)

    def key(self, identifier):
        # The service only ever sees this publication's namespaced identities.
        if not isinstance(identifier, str) or not identifier.startswith(self.scope.prefix):
            raise ValueError("draft identity is outside the selected publication")
        return identifier

    def draft(self, identifier):
        return self.tx.draft(self.key(identifier))

    def put_draft(self, identifier, payload):
        self.tx.put_draft(self.key(identifier), payload)

    def latest_classifier_revision(self, identifier):
        self.scope.check_classifier(identifier)
        return self.tx.latest_classifier_revision(identifier)

    def item_exists(self, list_id, identifier):
        self.scope.check_list(list_id)
        return self.tx.item_exists(list_id, identifier)

    def insert_item(self, row):
        self.scope.check_list(row[0])
        self.tx.insert_item(row)

    def insert_feedback(self, row):
        self.scope.check_classifier(row[1])
        self.scope.check_list(row[3])
        self.tx.insert_feedback(row)


@dataclass
class CurationRequest:
    """Everything one verified request is allowed to touch."""

    runtime: object
    account: str
    publication: str
    token: str
    credential: VerifiedSiteCredential
    target: object
    binding: object
    authority: object

    @property
    def scope(self):
        return {"accountId": self.account, "publicationId": self.publication, "bindingRevision": self.binding.revision}

    def headers(self):
        return {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Cyclotron-Account": self.account,
            "X-Papyrus-Publication": self.publication,
            "X-Papyrus-Binding": self.binding.revision,
        }

    async def reauthorize(self, operation):
        current = await verified(self.authority, self.token, self.account, operation)
        if current.account_id != self.account or current.subject_id != self.credential.subject_id:
            raise CurationError(403, "access_refused", "Curation access refused for this account or publication.")
        return current

    def run_config(self):
        return self.runtime.console(self.account).run(self.target.run_id)["config"]

    def priming_refs(self, config):
        pending = set(self.target.pending_outputs)
        rows = {row["id"]: row for row in config["classifiers"]}
        return [
            {"id": output, "revision": rows[output]["revision"]}
            for output in self.binding.outputs
            if output in rows and output not in pending
        ]


async def verified(authority, token, account, operation):
    try:
        credential = await _resolved(authority(token, account, operation))
    except PublicationError as error:
        if error.status not in (401, 403):
            raise CurationError(503, "authority_unavailable", "Curation authorization is unavailable.") from None
        code = "bearer_invalid" if error.status == 401 else "access_refused"
        message = (
            "Bearer credential refused." if error.status == 401 else "Curation access refused for this account or publication."
        )
        raise CurationError(error.status, code, message) from None
    except AccountAccessDenied:
        raise CurationError(403, "access_refused", "Curation access refused for this account or publication.") from None
    except Exception:
        raise CurationError(503, "authority_unavailable", "Curation authorization is unavailable.") from None
    if not isinstance(credential, VerifiedSiteCredential):
        raise CurationError(403, "access_refused", "Curation access refused for this account or publication.")
    return credential


def pins(request, *, binding_required):
    values = {
        "account": request.headers.get("x-cyclotron-account", "").strip(),
        "publication": request.headers.get("x-papyrus-publication", "").strip(),
        "binding": request.headers.get("x-papyrus-binding", "").strip(),
    }
    if not values["account"] or not values["publication"] or (binding_required and not values["binding"]):
        raise CurationError(400, "pins_required", "Expected account, publication and binding pins are required.")
    try:
        uuid(values["account"])
    except (ValueError, TypeError):
        raise CurationError(400, "pins_required", "Expected account, publication and binding pins are required.") from None
    return values


async def json_body(request):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise CurationError(415, "json_required", "A JSON request is required.")
    body = await request.body()
    try:
        value = json.loads(body)
    except ValueError:
        raise CurationError(400, "invalid_request", "A JSON curation request is required.") from None
    if not isinstance(value, dict):
        raise CurationError(400, "invalid_request", "A JSON curation request is required.")
    return value


def graphql_operation(body):
    if set(body) - {"query", "variables"} or not isinstance(body.get("query"), str) or not body["query"].strip():
        raise CurationError(400, "invalid_request", "An explicit priming query is required.")
    variables = body.get("variables")
    if variables is not None and not isinstance(variables, dict):
        raise CurationError(400, "invalid_request", "An explicit priming query is required.")
    try:
        document = parse(body["query"])
    except GraphQLError:
        raise CurationError(400, "invalid_request", "The priming query could not be parsed.") from None
    operations = [d for d in document.definitions if isinstance(d, OperationDefinitionNode)]
    if len(operations) != 1 or len(document.definitions) != 1 or operations[0].operation == OperationType.SUBSCRIPTION:
        raise CurationError(400, "invalid_request", "Exactly one priming query or mutation is required.")
    return WRITE if operations[0].operation == OperationType.MUTATION else READ, variables or {}


# ---- priming GraphQL: the same field names and shapes as the console API's priming surface ----


class PrimingSurface:
    """Per-request operations behind the GraphQL fields.

    Scope is refused from trusted configuration alone first (list, classifier identities, draft
    identity). Only then is the run configuration read for current classifier revisions.
    """

    def __init__(self, request):
        self.request = request
        self.prefix = namespace_for(request.account, request.publication)
        pending = set(request.target.pending_outputs)
        self.output_ids = [output for output in request.binding.outputs if output not in pending]
        self.refs = None
        self._service = None

    @property
    def list_id(self):
        return self.request.binding.priming_list_id

    async def service(self):
        if self._service is None:
            config = await asyncio.to_thread(self.request.run_config)
            self.refs = self.request.priming_refs(config)
            store = self.request.runtime.console(self.request.account)
            self._service = PrimingService(
                ScopedCatalog(store, self.list_id, self.refs),
                storage=ScopedPrimingStorage(store, self.prefix, self.list_id, self.output_ids),
                client=_NoTransformerClient(),
            )
        return self._service

    def check_contract(self, list_id, refs):
        """Scope check made from configuration alone, before any storage is opened."""
        outside = CurationError(
            403, "outside_publication", "The priming list or classifiers are outside the selected publication."
        )
        if str(list_id) != self.list_id:
            raise outside
        if not isinstance(refs, list) or any(not isinstance(row, dict) or set(row) != {"id", "revision"} for row in refs):
            raise outside
        if [row["id"] for row in refs] != self.output_ids:
            raise outside
        return refs

    async def checked_contract(self, list_id, refs):
        refs = self.check_contract(list_id, refs)
        await self.service()
        if refs != [{"id": row["id"], "revision": row["revision"]} for row in self.refs]:
            raise CurationError(
                403, "outside_publication", "The priming list or classifiers are outside the selected publication."
            )
        return refs

    def check_draft_id(self, identifier):
        if not isinstance(identifier, str) or not identifier or NAMESPACED.match(identifier):
            raise CurationError(403, "outside_publication", "The import draft is outside the selected publication.")
        return identifier

    def public_draft(self, draft):
        """Return the draft under the caller's own identity after proving it is this publication's."""
        contract = draft.get("contract", {})
        got = [{"id": row["id"], "revision": row["revision"]} for row in contract.get("classifiers", [])]
        wanted = [{"id": row["id"], "revision": row["revision"]} for row in self.refs]
        if contract.get("list_id") != self.list_id or got != wanted or not str(draft.get("id", "")).startswith(self.prefix):
            raise CurationError(403, "outside_publication", "The import draft is outside the selected publication.")
        return {**draft, "id": draft["id"][len(self.prefix) :]}

    async def run(self, function, *arguments):
        return await asyncio.to_thread(function, *arguments)


def surface(info):
    return info.context["surface"]


@strawberry.type
class PrimingQuery:
    @strawberry.field
    async def priming_contract(self, info: strawberry.Info, list_id: strawberry.ID, classifiers: JSON) -> JSON:
        s = surface(info)
        refs = await s.checked_contract(list_id, classifiers)
        return await s.run((await s.service()).contract, s.list_id, refs)

    @strawberry.field
    async def priming_template(self, info: strawberry.Info, list_id: strawberry.ID, classifiers: JSON, format: str) -> JSON:
        s = surface(info)
        refs = await s.checked_contract(list_id, classifiers)
        return await s.run((await s.service()).template, s.list_id, refs, format)

    @strawberry.field
    async def priming_draft(self, info: strawberry.Info, identifier: str) -> JSON:
        s = surface(info)
        s.check_draft_id(identifier)
        service = await s.service()
        return s.public_draft(await s.run(service.get, s.prefix + identifier))


@strawberry.type
class PrimingMutation:
    @strawberry.mutation
    async def priming_command(self, info: strawberry.Info, action: str, payload: JSON) -> JSON:
        s = surface(info)
        if action not in PRIMING_ACTIONS or not isinstance(payload, dict):
            raise CurationError(400, "unknown_priming_action", "Unknown priming operation.")
        if action == "transform":
            raise CurationError(400, "no_hosted_transformer", "No hosted transformer is configured; import a file instead.")
        if set(payload) != PRIMING_ACTIONS[action]:
            raise CurationError(400, "invalid_request", "The priming command payload is incomplete or has an unknown field.")
        s.check_draft_id(payload["identifier"])
        if action == "file":
            s.check_contract(payload["list_id"], payload["refs"])
            content = payload["content"]
            if not isinstance(content, str):
                raise CurationError(400, "invalid_request", "The import content must be text.")
            refs = await s.checked_contract(payload["list_id"], payload["refs"])
            payload = {**payload, "list_id": s.list_id, "refs": refs}
        if action == "accept":
            return await accept(s, payload)
        service = await s.service()
        method = {"file": service.start_file, "poll": service.poll, "feedback": service.feedback, "cancel": service.cancel}[
            action
        ]
        payload = {**payload, "identifier": s.prefix + payload["identifier"]}
        return s.public_draft(await s.run(lambda: method(**payload)))


async def accept(s, payload):
    """Accept the human-labelled preview, then apply it so the next decisions use it."""
    request = s.request
    if not request.target.model_identity:
        raise CurationError(409, "priming_unavailable", "This publication has no reviewed model identity; nothing was accepted.")
    service = await s.service()
    previewed = s.public_draft(await s.run(service.get, s.prefix + payload["identifier"]))
    # The evaluation firewall: an example may never also be an item this publication decides.
    store = request.runtime.console(request.account)
    operational = {row["id"] for row in await s.run(store.items, request.target.run_id)}
    if previewed["status"] != "accepted" and operational & {row["item"]["id"] for row in previewed["rows"]}:
        raise CurationError(
            409, "firewall_overlap", "An imported example is also an item this publication decides; nothing was accepted."
        )
    draft = await s.run(lambda: service.accept(**{**payload, "identifier": s.prefix + payload["identifier"]}))
    draft = s.public_draft(draft)
    try:
        applied = await apply_priming(request)
    except CurationError:
        raise
    except Exception:
        raise CurationError(
            409,
            "auto_prime_failed",
            "The examples were accepted but could not be applied yet. Accept again to retry; nothing is duplicated.",
        ) from None
    return {**draft, "autoPrime": applied}


async def apply_priming(request):
    """Apply every accepted example of the binding's list to the shared cyclotron's priming context."""
    store = request.runtime.console(request.account)
    # This runs in the request, not on the run's job queue: the queue cannot carry a bearer credential (it holds
    # session or service-principal authority), and the caller expects the applied result back. So it waits for the
    # run to go idle first, and never starts over a running job. A job accepted while it waits can still start
    # first; the engine's ownership lease then refuses the loser (auto_prime_failed, accept again), never both.
    while await asyncio.to_thread(store.run_has_active_work, request.target.run_id):
        await asyncio.sleep(PRIME_POLL_SECONDS)
    config = await asyncio.to_thread(request.run_config)
    _, fingerprint = await asyncio.to_thread(
        priming_snapshot, store, request.target, request.binding, config, request.binding.priming_list_id
    )
    command = {
        "publication": request.publication,
        "bindingRevision": request.binding.revision,
        "priming": {"listId": request.binding.priming_list_id, "fingerprint": fingerprint},
    }

    async def reauthorize():
        await request.reauthorize(WRITE)

    async def guard():
        return None

    actor = BearerActor(request.account, request.credential.subject_id, reauthorize)
    commands = PublicationCommandRuntime(request.runtime, None, guard, lambda: None, actor)
    session = SimpleNamespace(run_id=request.target.run_id, config=config, close=lambda: None)
    result = await commands.execute(session, WRITTEN, command, None, config)
    response = result.result["response"]
    return {"added": response["added"], "primedExamples": response["primedExamples"], "sourceFingerprint": fingerprint}


class QuietSchema(strawberry.Schema):
    def process_errors(self, errors, execution_context=None):
        # Errors are mapped to deliberate responses; no query, payload or stack is logged.
        return None


def priming_schema():
    return QuietSchema(
        query=PrimingQuery,
        mutation=PrimingMutation,
        extensions=[lambda: AddValidationRules([NoSchemaIntrospectionCustomRule])],
    )


def graphql_failure(errors):
    for error in errors:
        original = getattr(error, "original_error", None)
        if isinstance(original, CurationError):
            return original
        if isinstance(original, StorageLimit):
            return CurationError(413, "storage_limit", "The workspace has no room for this import; nothing was saved.")
        if isinstance(original, ValueError) and "budget exceeded" in str(original):
            return CurationError(413, "storage_limit", "The workspace has no room for this import; nothing was saved.")
        if isinstance(original, ValueError):
            return CurationError(409, "priming_refused", str(original)[:500])
        if original is None:
            return CurationError(400, "invalid_request", "The priming query was refused.")
    return CurationError(503, "curation_unavailable", "Curation is unavailable. Check saved history before retrying.")


def install_curation_routes(app: FastAPI, runtime):
    schema = priming_schema()

    def authority():
        configured = getattr(runtime, "curation_authority", None)
        return configured or unavailable_authority()

    async def admit(request, operation, pin_values):
        """Verify the bearer, then the pinned account and configured publication; nothing else is read first."""
        token = bearer_token_or_refusal(request)
        auth = authority()
        credential = await verified(auth, token, pin_values["account"], operation)
        if credential.account_id != pin_values["account"]:
            raise CurationError(403, "access_refused", "Curation access refused for this account or publication.")
        try:
            target, binding = target_for(runtime, credential.account_id, pin_values["publication"])
        except AccountAccessDenied:
            # Indistinguishable from any other refusal: nothing says whether the publication exists.
            raise CurationError(403, "access_refused", "Curation access refused for this account or publication.") from None
        if pin_values["binding"] and pin_values["binding"] != binding.revision:
            raise CurationError(409, "account_changed", "The selected publication binding changed. Refresh and try again.")
        return CurationRequest(
            runtime, credential.account_id, pin_values["publication"], token, credential, target, binding, auth
        )

    def failure(error):
        headers = {"Cache-Control": "no-store"}
        return JSONResponse({"error": error.message, "code": error.code}, status_code=error.status, headers=headers)

    def guarded(handler):
        async def wrapper(http: Request):
            try:
                return await handler(http)
            except CurationError as error:
                return failure(error)
            except PublicationError as error:
                return failure(CurationError(error.status, error.code, str(error)))
            except Exception:
                # No token, payload or stack leaves the lab.
                return failure(
                    CurationError(503, "curation_unavailable", "Curation is unavailable. Check saved history before retrying.")
                )

        wrapper.__name__ = handler.__name__
        return wrapper

    async def scoped_config(request):
        try:
            return await asyncio.to_thread(request.run_config)
        except Exception:
            raise CurationError(
                503, "curation_unavailable", "Curation is unavailable. Check saved history before retrying."
            ) from None

    @app.get(ROUTE + "/context")
    @guarded
    async def context(http: Request):
        bearer_token_or_refusal(http)
        request = await admit(http, READ, pins(http, binding_required=False))
        config = await scoped_config(request)
        try:
            envelope(request.target, request.binding, config)
        except ValueError:
            raise CurationError(
                503, "curation_unavailable", "Curation is unavailable. Check saved history before retrying."
            ) from None
        refs = request.priming_refs(config)
        pending = set(request.target.pending_outputs) & set(request.binding.outputs)
        priming = None
        if request.binding.priming_list_id and request.target.model_identity and refs and not pending:
            priming = {"listId": request.binding.priming_list_id, "classifiers": refs, "transformers": []}
        capabilities = ["read"]
        if priming is not None:
            try:
                await request.reauthorize(WRITE)
                capabilities.append("review")
            except CurationError as error:
                if error.status != 403:
                    raise
        await request.reauthorize(READ)
        body = {"scope": request.scope, "mode": "remote", "priming": priming, "capabilities": capabilities, "bindingRef": None}
        return JSONResponse(body, headers=request.headers())

    @app.post(ROUTE + "/publication")
    @guarded
    async def publication(http: Request):
        bearer_token_or_refusal(http)
        pin_values = pins(http, binding_required=True)
        body = await json_body(http)
        operation, payload = body.get("operation"), body.get("payload")
        if set(body) != {"operation", "payload"} or operation not in CAPABILITIES or not isinstance(payload, dict):
            raise CurationError(400, "invalid_request", "An explicit publication operation is required.")
        needed = WRITE if operation == "decide" else READ
        request = await admit(http, needed if operation in SUPPORTED_OPERATIONS else READ, pin_values)
        if operation not in SUPPORTED_OPERATIONS:
            raise CurationError(
                409, "operation_unavailable", "This publication operation is not available through the hosted curation routes."
            )
        config = await scoped_config(request)
        try:
            meta = envelope(request.target, request.binding, config)
        except ValueError:
            raise CurationError(
                503, "curation_unavailable", "Curation is unavailable. Check saved history before retrying."
            ) from None
        if operation == "status":
            if payload:
                raise CurationError(400, "invalid_request", "Status takes an empty payload.")
            if meta.get("pendingOutputs"):
                result = meta
            else:
                store = runtime.console(request.account)
                result = await asyncio.to_thread(status_snapshot, store, request.target, request.binding, request.publication)
                if result is None:
                    raise CurationError(503, "status_unavailable", "No verified publication status snapshot yet.")
        else:
            if not runtime.config.allow_live:
                raise CurationError(503, "live_disabled", "Live publication predictions are disabled.")
            if set(payload) != {"bindingRevision", "item"} or payload["bindingRevision"] != request.binding.revision:
                raise CurationError(409, "binding_changed", "The publication binding changed.")
            if meta.get("pendingOutputs"):
                raise CurationError(409, "criteria_pending", "Publication criteria are pending.")
            raise CurationError(
                409,
                "operation_unavailable",
                "Decisions are not activated for bearer-authorized curation callers; use the console session path.",
            )
        await request.reauthorize(needed)
        if (
            not isinstance(result, dict)
            or result.get("contractVersion") != CONTRACT
            or any(result.get(key) != value for key, value in request.scope.items())
        ):
            raise CurationError(502, "scope_mismatch", "The publication response did not match the selected scope.")
        return JSONResponse(result, headers=request.headers())

    @app.post(ROUTE + "/priming")
    @guarded
    async def priming(http: Request):
        bearer_token_or_refusal(http)
        pin_values = pins(http, binding_required=True)
        body = await json_body(http)
        operation, variables = graphql_operation(body)
        request = await admit(http, operation, pin_values)
        pending = set(request.target.pending_outputs) & set(request.binding.outputs)
        if not request.binding.priming_list_id or not request.target.model_identity or pending:
            raise CurationError(503, "priming_not_configured", "Priming is not configured for this publication.")
        result = await schema.execute(
            body["query"], variable_values=variables, context_value={"surface": PrimingSurface(request)}
        )
        if result.errors:
            raise graphql_failure(result.errors)
        await request.reauthorize(operation)
        return JSONResponse({"data": result.data}, headers=request.headers())


def bearer_token_or_refusal(request):
    try:
        return bearer_token(request)
    except PublicationError:
        raise CurationError(401, "bearer_required", "A bearer credential is required.") from None
