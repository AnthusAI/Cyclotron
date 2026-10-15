# Embedded execution authorization

`Cyclotron` exposes a per-call ownership boundary for trusted host authorization.
This is independent of cloud storage selection. Native storage and existing local
call signatures remain compatible; the SDK does not supply identity, membership
or publication grants.

## Public contract

```python
from decision_flywheel import ExecutionGuard

async def guard() -> None:
    # Host-owned functions freshly verify the session/account/person and the
    # explicit publication capability and current binding, pinned to this call.
    await reauthorize_pinned_account_and_publication()

await engine.decide(item, execution_guard=guard)
await engine.review(decision_id, label, execution_guard=guard)
await engine.undo_review(decision_id, execution_guard=guard)
await engine.learn(limit=1, execution_guard=guard)
await engine.replay(records, execution_guard=guard)

# Synchronous engine reads/steering, and host-owned provenance operations:
async with engine.execution(execution_guard=guard):
    snapshot = engine.status()
    page = engine.subscribe(after=cursor)
```

`ExecutionGuard` is `Callable[[], None | Awaitable[None]]`. The callback is
optional for existing trusted local callers. A hosted boundary must require and
supply it for protected operations. It is never derived from a browser callback,
boolean header, account-role assertion or serialized command. Capture the trusted
request's expected account/person/publication/capability/binding in its closure;
fresh resolution must match those pins, not silently switch authority.

The SDK first acquires its existing execution lock, then invokes the guard and
awaits it if needed, then begins the operation. This occurs before reading native
state, resetting batch bookkeeping, dispatching provider requests or committing
labels. The callback must return `None` to succeed. Non-callable guards and
non-None results raise `TypeError`; denial, authorization outage and cancellation
propagate unchanged. The callback is a local call argument and is not stored on
the engine, in a context variable, in native state or in a global setting.

`learn` invokes the guard for every separately owned pass, before even reading
its next queue entry. `replay` forwards it to every separately owned decision and
review. Authorization from an earlier request or learning pass is not reused.
Successful earlier operations remain committed if a subsequent guard denies;
this interface supplies no batch rollback, provider cancellation, or distributed
transaction across central authorization and engine stores.

## Host obligations and scope

Fresh central `console:read`/`console:write` authorization alone does not confer
publication capabilities. The trusted host guard must also resolve explicit
publication grants and current binding revision for the pinned account/person.
Session expiry/revocation and authorization outages must fail closed. A central
membership revision is evidence from that check, not a reusable authorization
lease. Revocation after an operation has begun is not cancellation of an already
dispatched external request.

Publication-host state reads performed before a native `decide`/`review` call are
not retroactively protected by that method's guard. Move protected pre-dispatch
validation into its guard closure after fresh authorization, or use a separately
guarded read scope. Host provenance writes after a native call require their own
guarded ownership scope. If a later guard denies, the earlier committed decision
may need host reconciliation; do not invent a successful provenance receipt.

The execution lock is non-reentrant. Do not call `decide`, `review`, `undo_review`,
`learn`, `replay` or another `execution` scope from inside `execution`, or from a
guard. Pass the callback keyword to lock-owning async operations instead. No
private lock access or replacement persistence adapter is needed by consumers.
Synchronous engine methods remain usable by trusted local callers and do not
automatically authorize themselves; hosted reads must use the guarded scope.

The current owner is the native per-open-engine lock plus the existing writer
lease. This hook does not establish a cloud lease/fence, durable queue or cloud
storage authority. A future execution backend must preserve the same ordering:
acquire actual ownership, run the trusted fresh guard, then access protected state
or dispatch. No cloud activation follows from this API.

## Offline verification

`embedded_execution_test.py` covers queued revocation, authorization outages,
callback cancellation, cancellation while waiting for ownership, independent
sync/async callbacks, rejected boolean guards, replay rechecks and per-pass
learning reauthorization. A SQLite authorizer on every native engine/shared/wheel
connection rejects even a SELECT in denied executions, proving ordering before
reads as well as mutations. Separate cases use the real `AccountsClient` with an
injected synthetic transport, then revoke membership, explicit publication grant,
binding revision, selected account or person while waiting for the lock. No real
credentials, provider calls, cloud resources or email are used.

Validation: **113 focused native execution/embedded/account tests passed**. The
read-only cross-owner publication checkout also passed **14 contract tests**
against this engine source. No full build or full regression was run for this
small execution-boundary change while the Accounts build is active.
