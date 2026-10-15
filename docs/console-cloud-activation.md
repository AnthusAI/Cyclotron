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
