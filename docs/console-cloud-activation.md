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
