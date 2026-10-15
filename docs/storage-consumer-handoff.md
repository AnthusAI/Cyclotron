# Storage handoff for remote and embedded integrations

This is the shared Python persistence boundary, not a second classifier engine.
Continue from the isolated `codex/prospectus-console` branch. Catalog baseline is
`45f8ee15057a069eb961b6a01e0240ca29fb1476`; this checkpoint adds definitions,
runs/commands and runtime state/evidence. Parent supplies the exact new commit.
Priming integrations own their existing worktrees and must not replace or duplicate
these repositories. No storage code here modifies those worktrees.

## Consumer contract

Remote and embedded modes must retain the same classifier configuration, revision
pins, labels/explanations, probability/calibration provenance and trace semantics.
Keep using the existing Python `Cyclotron`, `DecisionFlywheel`, `WorkspaceRuntime`
and console APIs. Remote transport and embedded ownership do not select a new
model, learning algorithm or feedback meaning. Do not fork these consumers to
choose a persistence implementation.

| Domain | Shared interface and current native implementation | Consumer surface |
| --- | --- | --- |
| Catalog, items, labels, explanations, retractions, displayed predictions | `CatalogStorage` / `SQLiteCatalogStorage` | Existing fifteen `WorkspaceCatalog` operations through `WebStore` |
| Versioned definitions and activation | `DefinitionStorage` / `SQLiteDefinitionStorage` | Existing seven `CyclotronDefinitions` operations |
| Frozen runs/items/feedback, events, summaries and command lifecycle | `RunStorage` / `SQLiteRunStorage` | Existing twenty-five `WebStore` operations |
| Shared model request generations, budgets and dispatch | `BatchStorage` / `SQLiteBatchStorage` | `SharedDecisions(..., storage=...)` |
| Core contract/active state, optimizer guidance, screening state and monitoring | `RuntimeStateStorage` / `SQLiteRuntimeStateStorage` | Existing `DecisionFlywheel` API; shadow evaluation uses the same state boundary |

`WebStore(path)` remains the native local composition. Its optional catalog,
definition and run repository arguments are composition/test seams. Other stores
still use its SQLite file; injecting one repository does not enable a hosted or
complete cloud backend. `register_run_definition` and
`advance_classifier_definitions` are legacy **native-only** hooks used to share a
SQLite transaction. They are not part of either portable protocol. A cloud
coordinator must implement the related-record invariant without exposing SQL.

Core `wheel.db` remains only for learning/answer-cache/cycle/feature modules not
yet converted. The portable runtime state interface does not expose it. No public
runtime backend injection/authority switch is offered until those dependencies
are converted. Papyrus integrations should not depend on `connect`, `db`, native
hooks, table names, export ordinals or a cloud SDK. Existing local-first use needs
neither central identity nor AWS libraries/credentials.

## Stable migration and canonical payload APIs

- Catalog: `store.catalog_storage.export(max_records=..., max_bytes=...)` and
  `.restore(snapshot, ...)`, version `cyclotron-catalog/v1`. Restore requires
  empty catalog and empty run/definition composition.
- Complete **native console database**: `store.export_storage(...)` and
  `store.restore_storage(snapshot, ...)`, version `cyclotron-console/v1`. Twenty
  known domain tables, raw JSON text, physical row ordinals and three native
  AUTOINCREMENT watermarks are retained. Unknown tables/columns or behavior-bearing
  views/triggers fail closed, rather than silently dropping future priming data.
  Restore requires an empty native composition; imported SQL is never executed.
- Immutable transport: `CatalogArchive` and `ConsoleArchive` both use the same
  scoped `FilePayloads`/`S3Payloads` implementation. `publish(snapshot)` returns
  `{manifest, records}` as `CatalogPublication` attributes containing `PayloadRef`s;
  `read(publication.manifest)` returns validated records. Manifest versions are
  `cyclotron-catalog-manifest/v1` and `cyclotron-console-manifest/v1` respectively.
  Constructor scope, record and aggregate-byte budgets are explicit. No mutable
  current head is selected by publication. The existing catalog v1 format stays
  unchanged.
- Discovery: `DynamoPayloadIndex.rebuild(payloads, references, max_records=...)`
  accepts verified canonical references; `page(after=..., limit=...)` returns
  bounded hints. Consumers must verify canonical bytes and canonical manifest
  membership. An index cannot authorize a person, activate a classifier or claim
  paid work.

The console export is **not** a full workspace/embedded-engine migration. Separate
per-classifier runtime/learning databases, shared request stores, reviewer state,
embedding caches, sessions and process ownership still need their own domain
conversion and coordinated export. Exporting the console database while another
engine is running does not freeze that engine. Cross-store migration must quiesce
its writers. Do not restore only active learned state without its answer/evaluation
provenance and then claim the classifier is safely resumed.

If priming adds persistent columns/tables, extend the appropriate shared interface
and versioned export codec with fixtures before landing the integration. Preserve
old versions and import behavior. Do not work around unknown-schema refusal with
a lossy copy or an integration-specific storage adapter.

## Authority and deployment boundary

Ryan's S3-only versus additional DynamoDB coordination decision remains pending.
S3 immutable payloads and a rebuildable discovery index are implemented, but no
live cloud mutation coordinator/current-head registry is implemented. Both modes
must fail closed if asked to choose an unavailable authority; neither should
silently fall back to ephemeral hosted SQLite. Pending paid calls are never
requeued merely because a timeout or process restart occurred.

Ryan subsequently authorized tested publication/merge/deployment once conversion
and dependencies are ready. This does not select coordination authority or grant
new persistent IAM/OAuth access. No cloud mutation, role/client creation, public
push or deployment occurred in this checkpoint. Exact production identifiers
remain in the private deployment review, not in this shared handoff.

Remaining hosted prerequisites are:

1. Select the coordination authority and finish all authoritative cloud adapters,
   including definitions/run composition and runtime learning/caches. Prove the
   same contract tests plus real cloud conditional/uncertain-response behavior in
   an approved disposable namespace.
2. Select and wire the existing Python BFF/worker lifecycle and durable ownership;
   preserve monitoring/reconnect behavior and no replay of uncertain paid calls.
   Normal frontend deployment alone does not supply this runtime.
3. Central owner must activate durable identity/accounts with reviewed issuer,
   client, product-role and entitlement policies. A local handler fixture is not
   deployed IAM enforcement or live Google authentication.
4. Review the exact console-specific OAuth client configuration, callback and
   Google-only/unlinked federation policy; then obtain action-time approval for
   any new persistent client/provider access change. Do not reuse the CRM client
   or copy Google secrets.
5. Once runtime placement is concrete, review the exact temporary-credential
   runtime role ARN, fixed central API/stage/method resource ARNs, caller Invoke
   permission, central exact-role policy/registry entry and selected storage
   bucket/table permissions. Obtain action-time approval for new persistent IAM
   grants; no broad policy or bespoke deployment-role workaround.
6. Verify the real build, deployed API, OAuth callback, allowed/denied accounts,
   account switching, logged-out access and actual browser flow on the approved
   target before reporting a running URL. Until then hosted mode is unavailable.

## Publication integration compatibility check

Read-only review of integration checkpoint `1cbe194` (owner head `5553658`) found
`PublicationLab` accepts an already-open `Cyclotron`, immutable host-supplied
publication/output bindings, and verified actors. Both `EmbeddedPublicationClient`
and `HTTPPublicationClient` call that same facade. This storage checkpoint changes
none of those engine, review, decision/status or subscribe signatures.

Reproduce the cross-branch check from this storage checkout:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python \
  scripts/verify_publication_storage_contract.py PATH_TO_INTEGRATION_CHECKOUT
```

The script reads the external trusted publication implementation/specifications,
loads **this** checkout's engine/runtime storage modules, disables external pytest
cache/bytecode writes and uses disposable engine data. It refuses a mismatched
core import. All **13 embedded/HTTP contract tests passed** against the new
storage/state boundary: common-item independent outputs, explicit explanations,
account/publication denial, restart/idempotency, stale prediction/binding pins,
scoped monitoring cursors and lost HTTP acknowledgement behavior. Provider and
HTTP boundaries are deterministic offline/ASGI fixtures. This is not hosted mode,
production authorization or full learning-storage portability proof.

The integration's outstanding output-scoped steering and historical binding/source
revision requirements remain release dependencies. The current global review-rate
override is not publication-scoped. Do not persist a second binding or membership
database in Papyrus or treat a URL as identity. Host binding revision and original
item revision/fingerprint must stay explicit until their owning shared repository
contract is complete; the console catalog's immutable item revisions do not by
themselves replace the embedded engine's latest-value decision log.

## Checkpoint validation

Full native regression after the storage extraction: **1,434 passed, one skipped**
(`make test`, 16 warnings). The separate cross-owner publication contract check
passed all 13 tests. These results validate native parity and the current consumer
contract; they do not establish a live DynamoDB runtime or a cloud deployment.
