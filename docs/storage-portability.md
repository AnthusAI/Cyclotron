# Cyclotron storage portability: inventory and conversion boundary

Ryan selected full SQLite + DynamoDB/S3 portability. This supersedes the earlier
checkpoint-first proposal. SQLite remains a complete zero-cloud default; no
server-hosted SQLite deployment is planned. Hosted activation is separate and
must stay unavailable until every authoritative path below is converted.

Tracking: existing story `decision-flywheel-9b2a6314-00c9-4485-b76f-9e3378fabead`,
console integration `decision-flywheel-73a50b5e-7f10-4908-aed5-45f87e02da28`.
The isolated console checkout starts at `785ebf8`; concurrent priming checkout
`f5ef70a` is not modified. This work keeps Python and does not encode runtime data
as Kanbus issues. Remote/embedded Papyrus initiatives are separate planning.

## Persistence inventory

| Data | Current authority and entry points | Required correctness boundary |
| --- | --- | --- |
| Classifier revisions, cyclotron definitions and activation | `WorkspaceCatalog` facade / `SQLiteCatalogStorage`, `CyclotronDefinitions`; SQLite `classifiers`, `cyclotron_catalog`, `cyclotron_definitions`, `run_cyclotron_definitions` | Compare active revision; freeze definitions with run creation |
| Item lists and immutable item revisions | `CatalogStorage` / `catalog_storage.py`: `item_lists`, `list_item_revisions` | Stable item identity, input hash, revision, ordering and bounded discovery |
| Labels, explanations, corrections and retractions | `CatalogStorage`: `item_feedback`, `item_feedback_retractions`, `item_predictions`; separate `reviewer_store.py` review events/presentations | Idempotent request identity; original displayed prediction/version; preserve explanations, eligibility and evaluation partitions |
| Runs, predictions, current item and summaries | `WebStore`: `web_runs`, `web_items`, `web_summaries` | Run+frozen inputs+initial events; command result/terminal-state consistency |
| Commands and recovery | `web_jobs`, `WebWorker` | One active command/run; atomic claim; fenced completion; no automatic replay of uncertain paid work |
| API trace/history and comparison inputs | `web_events`, `replay_feedback`, `cyclotron_checkpoints`, `matched_evaluation_inputs`, `matched_evaluation_authorizations` | Stable event source IDs, ordered resumable cursor, immutable approved plan/budget |
| Core learned state and caches | `DecisionFlywheel`: `runtime_state`, `runtime_events`, `runtime_answers`, `runtime_answer_history`, `runtime_rounds` | Contract/active head, exact answer identity, attempt reservation, prior generations and promotion evidence |
| Cycles and learning evidence | `cycles.py`, `cycle_replay.py`, `classifier_training.py`, `control_scheduler.py`, `diagnosis.py`, `example_attribution.py`, `hypothesis_ledger.py`, `idea_screening.py`, `proposal_operators.py`, `question_measurement.py`, `shadow_evaluation.py`, `staged_optimization.py` use the wheel's SQLite connection | Audit/learning cursors, training/development/held-out firewalls and reproducible fingerprints are authoritative, not disposable indexes |
| Feature bank | `feature_bank.py`, in wheel database | Exact feature identities/provenance; no silently dropped candidate history |
| Embedded multi-classifier state | `Cyclotron`: `meta`, `items`, `decisions`, `reviews`, `events`, `learning_queue`, `usage`, `usage_shares`; per-classifier wheel DBs | Review identity, per-classifier source cursor+derived state atomicity, learning-queue ownership, usage accounting |
| Shared request cache and paid ceiling | `SharedDecisions`: `batches`, `attempts`, `batch_history` | Atomically reserve attempt and budget; exclusive in-flight owner; retained generations; stale completion rejected |
| Frozen evaluation execution | `matched_run_evaluation.py`: `evaluation.sqlite3` execution clock/trace and before/after shared caches | Frozen clock/plan, cumulative budget across endpoints, no accidental fitting or duplicate calls on recovery |
| Reviewer application source state | `ReviewStore`: study metadata, articles, presentations, review events | Article/feedback import fidelity, stable study seed and presentation identities |
| Embedding cache | `VectorCache` in `embedding_retrieval.py`: append-only JSONL with embedder/hash/vector | Optional reconstructible acceleration; distinguish replay-sensitive stored embeddings from disposable indexes |
| Event/run files | `events.py`, `run_ledger.py` append JSONL | Stable ordering, append identity and crash recovery; imports/exports remain supported |
| Bundles, snapshots and trace exports | `bundle.py`, `embedded_cyclotron.snapshot/restore`, `trace_artifact.py`, `trace_review_history.py`, demo artifacts | Portable raw payload/schema/hash fidelity; backups do not replace live persistence |
| Writer ownership and process state | `FileLease` flock; worker session/sink maps and stopping event | Cloud ownership needs fencing and recovery; a lease timeout cannot cancel an external request |
| OAuth state/sessions | `OAuthSessions`: bounded memory, hashed opaque IDs, nonce/state/PKCE; restart signs out | One-use browser state, TTL and revocation; distributed sessions need a distinct adapter before multiple BFF processes |
| Central identities/accounts/memberships | Prospectus service; console stores no duplicate membership authority | SigV4 service identity and verified human evidence; authorize selected account on every operation |

The `S3VectorsStore`/`DynamoDBVectorStore` classes are retrieval design stubs, not
workspace persistence. The recently implemented central Prospectus Virtuus
adapters do not replace any Cyclotron engine database. The priming branch still
uses these same SQLite ownership points; new priming records must be carried
through later conversion without rebasing or overwriting concurrent work.

## First cohesive slice and storage semantics

The first domain contract is shared decision batches: reserve/read/settle,
request totals, generations/history, bounded discovery and portable export/import.
The complete physical request cache and its paid-attempt ceiling move together;
a cache-only wrapper over an authoritative SQLite counter is not sufficient.

SQLite retains its existing native tables and file compatibility. The agreed
canonical-payload convention is S3 with a rebuildable DynamoDB catalog/index.
Implementing authoritative DynamoDB claims/counters would be a distinct semantic
choice, **not approved by approving a DynamoDB index**. No such coordinator is
silently enabled. Interface/SQLite parity and immutable S3 payload work proceed
independently while this choice is reconciled.

Correctness needs an atomic boundary combining the run's paid ceiling, request
generation and exclusive dispatch claim. Minimal alternatives are:

1. A bounded per-run canonical S3 coordination document, updated with native
   conditional CAS, plus immutable response payloads; DynamoDB stays rebuildable.
   This preserves the convention, but introduces a hot object, explicit size and
   history limits, and uncertainty/recovery work. It must never overwrite/revert
   an old ledger or automatically replay a claimed provider call.
2. DynamoDB transactional coordination plus canonical S3 payloads. Small atomic
   counters/claims scale independently, but the coordination data is additional
   authority requiring backup/migration/reconciliation with S3. This changes the
   S3-only source-of-truth convention and needs an explicit decision.

A rebuildable eventually consistent index alone cannot safely reserve a paid
attempt or fence duplicate workers. The independent payload/index adapters do
not pretend to implement that coordination protocol.

Pending work cannot be stolen by timeout. A stable operation/owner ID supports
reconciling an uncertain reservation before any provider call. A new process may
reuse completed data; failed work needs explicit retry. A pending call with an
uncertain provider outcome requires operator reconciliation, never an automatic
paid retry. Stale owners cannot overwrite a successor's result.

No SQL-emulation layer over DynamoDB and no account-wide JSON megadocument are
planned. Domain operations must define their transactions and bounded payloads.
The initial slice does not change the public console entry point to claim a
complete cloud backend. Next conversions are catalog/feedback/run/command
repositories, core runtime/feature/learning state, embedded/reviewer/evaluation
compositions, then hosted session and execution wiring.

## Explicit unresolved choices

- Select and review the canonical S3 coordination-document or authoritative
  DynamoDB transaction alternative above, including recovery/export semantics.
- Decide whether future catalog JSON uses Virtuus's ordinary-path canonical
  publication or immutable S3 payloads with transactional heads. Existing runtime
  caches have no ordinary-file JSON format to preserve, but public bundle formats do.
- Set production payload/read/history/audit/retention budgets and index sizing;
  fail explicitly at limits rather than truncate evidence.
- Long-running worker ownership, provider ambiguity and session durability remain
  separate from storing records. No claim is made that a cloud adapter alone
  supplies a safe ephemeral or multi-worker runner.

All tests use synthetic inputs and injected/local cloud boundaries. No EC2 host,
paid resources, cloud IAM/OAuth changes, deployment or publication is authorized
by this implementation increment.

## Local implementation checkpoint

`batch_storage.py` defines `BatchStorage` and implements it natively in SQLite.
`SharedDecisions` consumes that interface: reservation commits before provider
work, and a separate conditional claim permits dispatch once. Current snapshots
retain the same native tables; additive ownership columns preserve legacy raw
payloads and request generations. Export/import carries attempts, current state,
retained history and exact JSON text; restore requires an empty destination and
rejects inconsistent generations or reused operation IDs before writing.

`storage_payloads.py` supplies local-file and injected-S3 immutable byte storage.
References bind account/workspace, SHA256 and byte length. Reads enforce scope,
configured byte budgets, length and digest; S3 calls pin the expected bucket
owner. Native create-only publication makes identical-byte retries safe after an
uncertain response. Local publication uses a fully written temporary file and
atomic link; power-loss durability of newly created directory entries is not
claimed. Neither adapter configures credentials, grants or cloud resources.

`payload_index.py` supplies optional DynamoDB payload discovery. It indexes only
verified canonical references, with bounded additive rebuilds and keyset pages.
A partial rebuild can be safely repeated; loss/staleness of the index never
authorizes a read, declares a current generation or grants a provider dispatch.
Canonical readers must verify every candidate. This intentionally implements no
mutable heads, budget counter, reservation or ownership authority. Domain heads
and reference discovery remain part of the unresolved cloud storage contract.

Verification: 82 focused synthetic tests pass across these modules, shared
consumers, embedded snapshot compatibility, matched evaluation and workspace
sessions. They cover two-process dispatch races, restart without lease stealing,
concurrent budget reservation, stale completion, retained generations, migration
fidelity, bounded pages, scope denial, canonical corruption, lost S3 response,
index rebuild/staleness, real SDK request serialization, and default local open/
restart with AWS SDK imports and network connections forbidden. A concurrent
refresh regression also verifies that payload and generation come from one
SQLite statement snapshot. S3/DynamoDB tests use
injected fake clients or SDK Stubber, not real services or emulators. These are
boundary/contract tests, not actual-cloud or full cross-backend domain parity.

Remaining conversion is substantial: catalog, feedback, run/command storage,
core/cycle/learning/feature state, reviewer/embedded/evaluation composition,
leases and distributed sessions still retain their existing authority. The
console remains in its existing local mode. A hosted backend is not selectable
on the strength of this checkpoint, and the story stays in progress.

The full Python regression run against the final implementation passed
**1,398 tests with one skipped** (16 existing pytest-bdd deprecation warnings). The
additional no-AWS subprocess specification was added after that run collected;
it passed separately and in the final 82-test focused run. `git diff --check`
passed. No browser/deployment claim follows from these storage tests.

## Catalog domain checkpoint

The existing `WorkspaceCatalog` API now delegates all fifteen catalog operations
to `CatalogStorage`. `WebStore(path)` selects `SQLiteCatalogStorage` by default;
its optional injected repository is a composition/test seam, not a hosted backend
selector. The native adapter retains the original tables, validation, ordering,
200-item browsing/upsert bounds, current-label semantics and immutable displayed
predictions. No SQL emulation or duplicate membership authority is introduced.

Classifier saves still advance cyclotron definition pins **inside the same native
SQLite transaction**. The SQLite-specific host hook remains deliberately inside
that adapter. Cloud coordination must eventually provide the same cross-domain
invariant. Definition/run stores themselves are not converted by this hook.
`item_results` also retains its native join to run names; migrating catalog records
alone does not migrate the referenced runs or their display metadata.

Catalog export uses one SQLite read snapshot. `cyclotron-catalog/v1` retains all
six tables: classifiers, item lists, every item revision, feedback history,
predictions and retraction tombstones. JSON database columns remain raw strings,
including whitespace, Unicode escapes and large integers in item values. The
feedback sequence watermark is separate from the highest retained row, so an
import does not reuse source identities. Configurable record and byte ceilings
fail explicitly. Restore validates/copies the complete snapshot before any
mutation, requires an empty catalog and empty run/definition composition, and
inserts all rows plus the watermark in one transaction. It is not a live merge,
a complete workspace restore or an online migration protocol.

`CatalogArchive` transports those records through the existing immutable
`FilePayloads`/`S3Payloads` boundary. Each record is a separate content-addressed
object; a final immutable manifest contains exact membership, schema and sequence
watermark. Publication checks aggregate limits before putting records, then puts
the manifest last. Retrying the same snapshot after an uncertain row publication
is safe. Partial publication can leave orphan immutable objects, never an active
head. Manifest membership and payload hashes govern import; a rebuildable
DynamoDB discovery index can contain only hints. Canonical reads check fixed
scope, aggregate byte/record budgets, duplicate membership, row envelopes and
record-level invariants. No mutable catalog head, CAS coordinator, account-wide
live JSON aggregate, runtime cloud configuration or hosting activation exists.

Scope prefixes and a manifest are not authorization. The host must authorize the
human/account and migration operation before supplying the fixed repository
scope. Run references, definition activation, command receipts and current heads
remain separate conversion work. The pending S3-only versus additional DynamoDB
coordination decision is unchanged; catalog work did not select either option.

Catalog checkpoint focused validation: **76 passed**, covering existing catalog,
definition, GraphQL/API and worker behavior plus new native migration and injected
file/S3 transport tests. New scenarios include same-request races between two
SQLite clients, export concurrent with a committed writer, rollback after a native
insertion failure, raw-text/history/tombstone/sequence fidelity, pinned-reference
validation, no-AWS/no-network local save and restart, scope denial, missing payload,
manifest corruption, additive index rebuilding, aggregate budgets and lost S3
publication response. S3/DynamoDB boundaries are injected fakes, not real services.

Catalog checkpoint full Python regression: **1,419 passed, one skipped**, with
16 existing pytest-bdd deprecation warnings. `git diff --check` passed. Both
priming worktree heads remain unchanged (`f5ef70a`, `bad2b5b`). No cloud service,
IAM/OAuth/DNS configuration, public push or deployment was changed.
