# Approved Cyclotron cloud activation — in progress

Ryan explicitly approved the previously consolidated scope via the parent thread
on 2026-10-15. Paid model commands remain disabled. Sole Cyclotron writer is this
isolated task-4 worktree; sole central/client writer is CRM owner
01a137a5-6214-76d2-b8f5-bd3db565e004, with central owner validating read-only.

Verified target: cyclotron-production, account 682849160744, us-east-1, Endymion
SSO. Existing deployment artifact bucket is cdk-hnb659fds-assets-682849160744-us-east-1.
No Legacy profile, new hosted zone, EC2, NAT or broad runtime grants used.

Created Amplify static app cyclotron-console, ID d2lya5yok0779c, production branch.
Normal manual Amplify artifact deployment avoids a new GitHub token or public
source push. Backend deployment still awaits the dedicated console client ID.

Existing anth.us zone is Z04979341DPRV5BDQNZMG in papyrus-production. Created only:

- lab.cyclotron.anth.us CNAME d1rab2h8u5j1hk.cloudfront.net
- _89a0e42e1f0ebdc9fef687ac1395ce41.cyclotron.anth.us CNAME
  _edda7ff777e05849b3db30de1dee3138.wzccmgtwzk.acm-validations.aws

DNS change C0538753106NE5ZSA9VUL and Amplify certificate/domain association were
pending verification at this checkpoint. Existing marketing A/AAAA and CRM
records were not changed. No public running-application claim is made.

Deployment review caught S3's missing-object 403 behavior without ListBucket.
Hosted journals now explicitly permit ambiguous creation probes, but authority
comes only from confirmed create-only anchor AND head receipts. Normal reads
still fail closed. Existing anchors with missing heads cannot reset; existing
readable heads prevent anchor recreation; even unreadable existing heads cannot
be overwritten by conditional creation. Lost anchor receipts never grant a head
publication. No ListBucket grant is added. 31 journal/session/hosted tests pass.

Committed code and Kanbus remain local; no remote branch or PR has been pushed.
Preserve this branch and coordinate landing with concurrent priming/publication
work. scripts/verify_cloud_storage.py is the bounded synthetic actual-AWS trial
for CAS, unconditional-write denial, lost receipt and KMS context binding.

## Foundation and actual-AWS checkpoint

Foundation stack cyclotron-console is CREATE_COMPLETE:
`arn:aws:cloudformation:us-east-1:682849160744:stack/cyclotron-console/8da58320-c8e0-11f1-984f-0afff73fed87`.
Both exact BFF/worker role ARNs were retrieved and their trusts contain only
lambda.amazonaws.com. Key ARN:
`arn:aws:kms:us-east-1:682849160744:key/3d92f6f1-640e-474f-843a-cdc49e4301b8`.
Command queue URL:
`https://sqs.us-east-1.amazonaws.com/682849160744/cyclotron-console-commands`.

Amplify production job 1 SUCCEED; DNS change INSYNC; domain association remains
PENDING_DEPLOYMENT. Static assets alone are not functional authentication.

Actual S3/KMS trial PASS under operator temporary Endymion credentials: isolated
synthetic account namespace f568c308-4edd-4e2f-ae3a-9fb1e350a5fa, 11 GET/9 PUT,
stale CAS denied, unconditional head overwrite denied by bucket policy, simulated
lost response remains uncertain, wrong KMS context denied. No provider calls or
object deletion. This does not prove deployed runtime IAM or Google sign-in.
AWS CLI temporary credentials were consumed in memory, never printed or saved;
the older Python SDK SSO cache could not refresh independently.

## New quota blocker — action stopped

Actual Lambda account concurrency quota is 10. AWS requires 100 unreserved units
alongside the approved BFF5/worker2 reserved caps. A request for 107 was rejected
by AWS because this quota API requires a desired value above its nominal default
1000. The subsequent proposed request for the minimum accepted 1001 was rejected
before execution by automatic approval review: account-wide future compute/cost
blast radius exceeds the specified 5/2 function scope. No1001 request was sent.
Explicit user approval has been requested; do not retry or bypass this denial,
including by dropping the approved function caps. Continue unaffected central,
DNS and storage work. Functions/API still await dedicated client ID plus quota
resolution. No private/public source push or PR has occurred.

Public TLS request to https://lab.cyclotron.anth.us/ returned HTTP200 with
certificate verification enabled. Actual browser loaded the Cyclotron static
shell and correctly displayed “The account request failed. Try again.” because
the backend/API is not deployed/configured yet. No private workspace appeared.
Screenshot: deployment/cloud-static-blocked-proof.jpg. This is explicitly a
partial static deployment, not working Google sign-in or account integration.

## Dedicated client and runtime preparation

Parent relayed CRM PR7 merged f8463f3, Amplify job9 SUCCEED; PR8 central role-pair
rollout remains owned solely by the CRM writer. Dedicated console public client
is now 2uhgmi3c69nobpiauichdsdmo3, existing pool us-east-1_hlSyajw9S, login origin
https://prospectus-712236451410.auth.us-east-1.amazoncognito.com.
Independent describe-user-pool-client read (non-secret fields only) confirms:
Google only; OAuth code flow; openid/email/profile; exact lab callback and root
logout; token revocation enabled. No directory/CRM client was reused.

Prepared runtime UPDATE change set approved-runtime-client-20261015:
`arn:aws:cloudformation:us-east-1:682849160744:changeSet/approved-runtime-client-20261015/c15b292f-c3b7-423c-bf83-9f9cd52d6d83`.
Status CREATE_COMPLETE means prepared, NOT executed. Its seven Add actions are
the BFF/worker functions, two API invoke permissions, HTTP API/stage and SQS event
mapping. No foundation resource replacement or privilege change is included.
Processed template independently checked: exact separate roles, reserved caps
BFF5/worker2, real client parameter and no live/provider environment toggle.
Do not execute while the quota1001 request remains unapproved. The prior automatic
approval rejection remains in force; no retry or cap-dropping workaround occurred.

## User correction: remove unrequested function reservations

Ryan explicitly rejected reserved concurrency: “Why are you configuring reserved
concurrency?? I never wanted that!” The parent authorized removal and continued
deployment at the unchanged account quota. This supersedes the earlier quota
pause; no quota increase will be requested. The two ReservedConcurrentExecutions
properties have been removed from renderer/template; exact template comparison
verifies no other change. SQS MaximumConcurrency2, batch1, existing API throttles,
scoped roles and disabled paid commands remain. cfn-lint passes.

The last verified applied account limit was10/unreserved10. Fresh read-only
reconfirmation failed because Endymion SSO expired. There was no observed
PutFunctionConcurrency error: the earlier block was inferred from the documented
100-unreserved rule before creating functions. The actual observed AWS quota
request error was IllegalArgumentException: “You must provide a quota value
greater than the default quota value of 1000.0”. This distinction corrects any
implication that function reservation creation had been attempted and failed.

Existing Endymion CLI login renewal has started and reached Ryan's password step
in the opened AWS sign-in tab. No password was read or requested in chat. Revised
packaged-unreserved template is ready with the same verified artifact pointer;
create/review/execute of a NEW changeset awaits completed SSO, not quota approval.
The older prepared reserved-concurrency changeset must never be executed.

## Unreserved runtime deployed — 2026-10-16

After Ryan renewed Endymion SSO, the new change set
`approved-runtime-unreserved-20261016` was reviewed and executed. Stack
`cyclotron-console` is UPDATE_COMPLETE. The old reserved-concurrency change set
was not executed. Applied account concurrency remains10/unreserved10; both
functions have no reserved concurrency. SQS batch1/max2 and HTTP stage5rps/burst10
remain, with paid commands disabled.

BFF and worker use the approved separate Lambda-only runtime roles. Backend
origin is https://o01d28uizl.execute-api.us-east-1.amazonaws.com. The deployed
artifact CodeSha256 matches `hbG+3kw6+bHRz2b2XvedIXh4DldDnlKvM8A7ASnNbP4=`.
Amplify app d2lya5yok0779c now proxies auth/API/GraphQL/run requests to that
backend. Its lab domain association is AVAILABLE and verified; the public
signed-out session returns200/no-store, and GraphQL plus tenant event requests
return401. `scripts/verify_runtime_http.py --signed-out-only` passes against
https://lab.cyclotron.anth.us with zero provider calls.

Login initiation currently returns503. Matching central API metrics show a2xx
response; the central owner confirmed a composition bug passing fractional Unix
seconds into challenge expiry, violating the integer timestamp contract. Their
fix330713f135b6fefa061e5118e42776a2c1967f79 floors issuedAt only and retains
fractional cryptographic freshness checks. Its actual-factory/native-file
regression and27 identity checks/typecheck pass. The sole central deployment
owner is rolling it through normal CI. Python validation remains strict; no IAM
expansion or central source edits occurred here.

Human Google sign-in, account creation/switching and queued human authorization
are not yet proven live. Browser inventory also reports the Mac locked; normal
manual unlock is required before browser verification. No password, token,
nonce or cookie was printed. Existing local synthetic proofs remain separate
from this partial cloud checkpoint.

The in-app browser remains usable despite the native Mac lock. It independently
loaded the actual public lab and displayed “Welcome to Cyclotron” / “Continue
with Google”, replacing the earlier proxy-error screen. Screenshot:
`deployment/cloud-signed-out-proof.jpg`. Native Chrome/credential handoff may
still need unlock; do not treat the lock as blocking all browser observation.

## Standing AWS operation boundary

Ryan's standing instruction: never operate the AWS console from any browser.
All AWS sign-in, provisioning and configuration work uses CLI/API only. Product
browser verification at lab remains permitted. The former browser SSO handoff
must not be resumed. Existing CLI SSO is renewed; no browser console action is
needed for the current deployment.

## Central fix deployed and actual Google onboarding verified

Parent confirmed Prospectus main3c90214afd312ed7dd9ef5912fedfe8eb7913da5,
Amplify job11 BUILD/DEPLOY/VERIFY succeeded, with live integer expiry/300-second
TTL checked by the sole central writer. One bounded public HTTP probe now passes
signed-out denial and actual login initiation: dedicated client/callback,
code+PKCE S256, nonce/state and browser-binding cookie. Proof values are never
printed. No provider calls.

Actual in-app browser Continue with Google completed the Google redirect using
its existing authenticated session and rendered signed-in account onboarding.
Exactly one create request each produced the synthetic accounts
`Cyclotron verification 2026-10-16 Alpha` and
`Cyclotron verification 2026-10-16 Beta`; both opened their empty account-scoped
workspace with owner/Can edit workspace. No fabricated identity, human labels,
provider call or retry of an uncertain account create was used. These are retained
verification records, not fabricated customer accounts. UI error log was empty.

A signed-in unowned synthetic-tenant GET was attempted in a separate product
browser tab, but navigation returned net::ERR_BLOCKED_BY_CLIENT. This is not
accepted as API denial proof. Live viewer/revocation/queued-execution denial
remain separate from existing synthetic regression evidence; no permissions
were expanded to produce such fixtures.

Beta-to-Alpha switching completed in the actual browser: both named accounts
remain in the shared selector and Alpha's owner workspace is selected. During
the request the previous private workspace unmounted and controls disabled.
Signed-in screenshot: `deployment/cloud-google-account-proof.jpg`. Only empty
workspace navigation was displayed; no human labels were submitted.

Sign-out completed and removed selector/private workspace. A full page reload
still rendered only Welcome/Continue with Google. Final screenshot:
`deployment/cloud-logout-proof.jpg`. This completes the first live Google ->
central account create/select -> account-scoped workspace -> switch -> logout
slice. HTTP unauthenticated GraphQL/events denial also passed. Live viewer,
revocation and queued-worker human authorization remain unproven; existing
synthetic specs cover those boundaries, and paid commands remain disabled.

## Current account loading regression — 2026-10-16

Ryan reported an iPad account-switch freeze and long loading followed by failure.
Earlier morning success does not establish the current state. Recent deployed
BFF reports confirm three28-second Lambda timeouts and successful calls around
22–27seconds. Session loading serialized central me/list/read-authorize/write-
authorize. Switching then triggered another full session request; repeated window
focus could start overlapping refreshes. No user data was changed by diagnosis.

The lab fix concurrently awaits independent listing/read/write authority reads,
keeps fresh me and actual execution authorization, and returns verified full
session state from selection to avoid the duplicate GET. Selection is persisted
only after authority checks; denied/outage paths remain closed. UI requests now
abort after20seconds, coalesce in-flight focus refreshes and suppress focus during
mutations. An uncertain switch clears the workspace and offers state-only retry,
not automatic mutation replay; create retains its idempotency key. Browser and
BFF emit only fixed paths/status/duration, with BFF Lambda correlation ID—no
actor/account IDs, cookies, proofs, bodies or exception text.

Validation:72 backend specs,9 UI checks and TypeScript/production builds pass.
Regression barriers prove concurrent authority reads; tests cover no duplicate
GET, focus coalescing, bounded stalled request, uncertain-switch retry, route
redaction and existing CSRF/viewer/foreign-account boundaries. No network or
credentials in specs. Artifact SHA256
575199f9e0c6cd95a75c97d23037a6c0342130ac530b908f8caa467299f82e21.
Prepared change set account-latency-fix-20261016 changes BFF Code only in the
source template; worker remains on its previous artifact. CloudFormation also
reports ConsoleApi Body modification, but processed old/new API bodies compare
exactly equal. No IAM/storage/queue/quota replacement or permission change.

The unwanted “Ryan at Capacity” entry is being traced by the central owner using
exact identity/account provenance and associations. Lab code does not generate
that name. No deletion, email-based linking or identity inference is authorized
by these diagnostics; recovery/approval checks remain with the parent.

First mitigation deployed: BFF UPDATE_COMPLETE/Active/Successful; Amplify job2
and final UI job4 SUCCEED. Existing account listing returned200 in14915ms and
selection200 in15261ms end-to-end, with no duplicate GET after selection. These
are measured partial improvement, not acceptable low latency. Central timings
showed me6570ms plus list8231ms. Workspace execution still invokes fresh guards;
those fences were not removed to hide the storage latency. The central owner
identified15 native helper invocations per identity lookup, each reinitializing
S3/bucket checks, and owns bounded immutable-read batching in its own branch.

The identity owner separately confirmed from source plus four actual RSA/native
checks that fresh central list/authorize already enforce the committed enrolled
session family, auth_time, issuer/client/policy/principal and original Person.
Therefore the next narrow lab optimization skips the redundant me ONLY for
GET/api/session and POST/account-select: local signed JWT/sub/session/CSRF checks
remain; finite nonfuture auth_time and nonempty bounded ASCII origin_jti are
validated locally. Every success awaits a fresh listing whose nonnull person ID
matches the encrypted original session, plus exact selected-account authorization.
Null/mismatch revokes the local session; outage is never a positive fallback.
Worker/workspace me and execution guards remain unchanged. New regressions cover
null/mismatched Person, unknown/new-family representation, outage, malformed
binding/future time, revoked selection and no mutation replay. No positive cache
or email-based identity inference is introduced.

The redundant-call optimization deployed successfully as BFF-only change set
account-authority-once-20261016 (artifact6b60bee8a244a6a6f60a72b90a5eabac873cd54b05a69daeefbb448359076376).
Warm selected-account reload now measures8535ms; Alpha-to-Beta selection8921ms,
both200, versus14915/15261ms after the first mitigation. Current cold reload was
15058ms, with Lambda14005ms versus central max8504ms. Additional redacted local
session-read/token-verify/session-write phase timing is being deployed to isolate
that gap.94 backend tests include proof-redaction coverage.

The touch-menu fix now removes window-focus loading entirely: only a throttled
return from a background document can refresh account display. A regression
proves native window focus even after60seconds cannot start loading, while a
visibility return can.11 UI checks and production build pass. This is a tested
browser-event fix, not a claim of physical iPad/Safari verification.

Central owner independently tested its storage-helper batching fix with138
identity/durability/delegation/RSA checks plus75 application checks and production
bundle build; normal central PR/CI/deployment remains theirs. No receipt/family
or authorization fence is relaxed here. The unwanted account's exact canonical
creator/retained version/associations are under their read-only review; no
account or identity has been removed by the console owner.

Final lab checkpoint246338f is deployed: BFF artifact
846e110e240290c1a3a624584c69b3c025aa8e79c304d73d92f71547a519244e,
UPDATE_COMPLETE/Active/Successful, Amplify job5 SUCCEED. Actual native menu open
and Escape-close left Beta's workspace mounted, without Loading or a new account
request log. Final cold trace: browser14635ms, Lambda13514.95ms; session-read220ms,
JWT100ms; parallel central list8326ms and authorizations6888/6997ms. The remaining
roughly4.9s cold overhead is not attributed by these phase logs; do not claim it
is KMS, JWKS, network or framework work without further measurement. Warm
reload8535ms and switch8921ms remain the current measured performance.

Machine-readable evidence: deployment/account-latency-evidence.json; current
browser screenshot: deployment/cloud-account-latency-fix.jpg. Physical iPad/Safari
is not verified. Central PR10 is open with CI in progress at this checkpoint;
central helper-read batching remains needed for materially faster account and
workspace operations. No further broad testing or permission changes is needed
to wait for that normal rollout. The console owner generated no extra accounts,
labels, model calls, identity links or deletions during this incident.

Final sign-out passed in 7148ms; subsequent signed-out session read returned200
in116ms and the browser showed only Welcome/Continue with Google. Anonymous
public HTTP security checks pass again after the final rollout. The test browser
is signed out to avoid ongoing background workspace reads while PR10 is pending.
