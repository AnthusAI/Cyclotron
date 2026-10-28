# Runtime learning storage handoff

This completes the native learning, feature, hypothesis and control repository
conversion following `0b90377`. It also converts forward-evaluation persistence
and the remaining direct learning-event reads. It does not select DynamoDB or S3
coordination authority or enable a hosted runtime.

## Shared algorithms and native repositories

| Domain | Public contract | Native implementation | Consumers |
| --- | --- | --- | --- |
| Learning artifacts and control-cycle progress | `LearningStorage` | `SQLiteLearningStorage` | Classifier fitting, question/example measurement, staged optimization and control scheduler |
| Feature hypotheses, lineage, attempts and deployment view | `FeatureStorage` | `SQLiteFeatureStorage` | Existing `FeatureBank` validation/algorithms |
| Retained control ideas | `ControlStorage` | `SQLiteControlStorage` | Control scheduler and shared hypothesis lookup |
| Hypothesis/diagnosis evidence records | `HypothesisStorage` | `SQLiteHypothesisStorage` | Existing `HypothesisLedger` statistics, comparison, lineage and diagnosis algorithms |
| Forward-evaluation membership, scoring and looks | `ShadowStorage` | `SQLiteShadowStorage` | Existing `ShadowEvaluator` admission, alpha spending and promotion policy |
| Runtime evidence/cycle reads | `RuntimeStateStorage.events_of` and `next_cycle_number` | `SQLiteRuntimeStateStorage` | Cycle replay, operational cycles and diagnosis |

The core composes these repositories on its existing SQLite connection. Learning,
feature, hypothesis, control, diagnosis, screening, proposal, cycle and forward-
evaluation algorithms no longer query the wheel connection. `wheel.db` remains
for native composition, existing embedded snapshots and local compatibility.
There is no alternate partial-cloud constructor or second classifier algorithm.

The learning interface accepts five fixed artifact kinds (`classifier`,
`questions`, `examples`, `rubric`, `controls`), never caller-supplied SQL table
names. It retains raw progress/results, explicit retry behavior, newest-first
artifact discovery and counts. Completion, promotion aliases and clearing an older
pending-evaluation marker commit together. A failed alias insert rolls back all
three changes. Existing answer-dependency validation remains in the shared engine.

`FeatureBank(db)` and `HypothesisLedger(db)` remain valid local constructors;
`storage=` accepts the corresponding domain repository. Feature validation,
lineage and diagnostics remain in `FeatureBank`. Statistical calculations,
held-out exclusions, contextual evidence selection, verdict interpretation and
operator/cause credit assignment remain in `HypothesisLedger`. Native adapters
retain evaluation timestamp/row-order precedence and append-only verdict order.
Absence of the optional control-ideas table still returns no idea; unexpected
storage failures now propagate rather than being mistaken for missing evidence.

Forward-scored identities, joined order, old incumbents, statuses, and unique
look identities remain native records. This extraction changes no screening
partitions, review eligibility, request budgets, alpha spending or promotion rules.
A cloud implementation must preserve the actual execution owner and existing
uncertain-request rules; native transaction wrappers are not a distributed claim.

## Complete per-classifier runtime migration

`runtime_snapshot.py` implements `cyclotron-runtime/v1` for all **20 known native
runtime tables**: state/events, answers and retained history, learning trials,
five learning-artifact kinds, features, control ideas, hypothesis evaluations and
verdicts, diagnosis causes/elements and four forward-evaluation tables.

```python
# Source is a quiescent native runtime store, or wheel.runtime_storage.
snapshot = source.export_storage()

from decision_flywheel.runtime_storage import SQLiteRuntimeStateStorage
restored = SQLiteRuntimeStateStorage(destination_path)
restored.restore_storage(snapshot)
restored.close()
# Open DecisionFlywheel with the matching task/model contract afterwards.
```

Lower-level `export_runtime(db, ...)` and `restore_runtime(db, snapshot, ...)`
accept a native connection with no open transaction. Snapshot tables have fixed
columns plus `_rowid`; `present_tables` retains lazy-table absence. Raw JSON stays
text, preserving whitespace, Unicode, large integers, explanations and hashes.
Physical row ordinals preserve equal-time evaluation precedence, append-only
verdict order and newest-first artifact reuse. Runtime event and answer-history
integer IDs preserve their rowids; these tables do not use AUTOINCREMENT and have
no independent sequence watermark.

Export uses one SQLite snapshot with no record or byte budget. Import first
copies/validates the snapshot, column types, JSON, ordinals, table presence
and every native unique/key constraint in a trusted scratch schema. It never
executes imported SQL. Unknown or changed table definitions, extra unique
constraints, views and triggers fail closed. Restore requires an empty destination
with compatible table presence, and creates/inserts the entire composition in one
transaction. A mid-restore exception rolls back both new tables and rows. Restore
is not a live merge or an authorization grant. Pending answers/trials remain
pending; import does not replay or clear them.

`RuntimeArchive` uses the same `CatalogArchive` immutable per-record/manifest
transport already used by console/catalog migrations, with a runtime-specific
codec and table-presence metadata. File and injected-S3 round trips pass. Returned
references are fixed to the authorized account/workspace scope; manifests are
migration transport, not mutable runtime heads or DynamoDB authority.

This snapshot covers **one classifier runtime database**. It does not include the
embedded Cyclotron root database, shared-batch database, console catalog/runs,
reviewer source database, frozen-evaluation database, JSONL sidecars or sessions.
Those require matching snapshots under host quiescence. A runtime export alone
must never be advertised as a complete Cyclotron/workspace cloud migration.

## Verification and remaining boundary

Focused consumer/repository regression: **199 tests passed**. The final migration
specifications passed **14 tests** in a separate final run after adding injected-S3 transport and actual
cache-only reopen coverage. These include complete 20-table round trips, raw JSON,
row-order/timestamp ties, lazy schemas, interrupted restore rollback, concurrent
multi-domain export consistency, bad schemas/identities, snapshots past the old budgets, pending work and
provider-free restart. A control/training integration test replaces `wheel.db`
with a rejecting object and completes the three-control experiment using only the
repositories.

An additional **100 native/embedded consumer tests** passed. Final repository/
diagnosis/firewall checks passed **34 tests**, including the first-human-feedback
boundary with direct wheel SQL forbidden.

The integration owner's read-only execution harness passed **83 tests** against
this core and Papyrus source, including the public post-lock guard. No full suite,
heavy build, cloud activation, IAM/OAuth change, public push or deployment was run.

The next independent conversions are embedded root operations/derived cursors,
reviewer source state and frozen-evaluation clock/trace composition. Live cloud
domain implementations still await the authority decision. Distributed ownership,
worker lifecycle and session durability remain separate implementation/release
requirements. Native history is intentionally not given a new silent cloud cap;
cloud read/retention budgets must fail explicitly when selected.
