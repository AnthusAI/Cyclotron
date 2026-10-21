# Pending lab stack changes

Reviewed, committed, not yet deployed. `scripts/dev/check_lab_template.sh` and
`tests/test_lab_template.py` prove that `template.yaml` equals
`deployed-template.snapshot.json` plus exactly the add-only operations in
`pending-changes.json`, and nothing else. After a deploy is executed, refresh the
snapshot (`aws cloudformation get-template --template-stage Original`), empty
`operations`, and empty this list.

## Lab verifier role (kanbus I5; contract: AnthusAI/Prospectus#72)

The next deploy of `cyclotron-console` changes exactly these paths:

| Path (JSON Pointer into the template) | Change |
|---|---|
| `/Parameters/LabVerifierEnabled` | New parameter, `'true'`/`'false'`, default `'false'`. |
| `/Conditions/HasLabVerifier` | New condition: `LabVerifierEnabled == 'true'`. |
| `/Resources/LabVerifierRole` | New IAM role `cyclotron-lab-verifier` (no path). Trust: `sts:AssumeRole` only from `cyclotron-console-bff-runtime`. Policy `cyclotron-lab-verify`: `execute-api:Invoke` on `GET /v1/identities/me` and `POST /v1/accounts/*/authorize` of API `bnh9mctvo9` (account 712236451410, stage `api`) and nothing else. |
| `/Resources/BffRole/Properties/Policies/0/PolicyDocument/Statement/-` | New statement on the BFF role: `sts:AssumeRole` on exactly `arn:<partition>:iam::<account>:role/cyclotron-lab-verifier`. |
| `/Resources/BffFunction/Properties/Environment/Variables/CYCLOTRON_LAB_VERIFIER_ROLE_ARN` | Set to the verifier role ARN only when `LabVerifierEnabled` is `true`; otherwise absent. |
| `/Resources/BffFunction/Properties/Environment/Variables/CYCLOTRON_ACCOUNTS_API_URL` | Set to `https://bnh9mctvo9.execute-api.us-east-1.amazonaws.com/api/` only when `LabVerifierEnabled` is `true`; otherwise absent. |

With the default `LabVerifierEnabled=false` the deploy creates the role and the
AssumeRole grant but leaves the BFF environment untouched, so hosted curation
routes keep answering `503 authority_unavailable`. The worker function and role
are not changed. No other resource, output, or parameter changes.

Deploy order and approvals: see "Lab verifier role" in `README.md`.
