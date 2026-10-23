"""Offline call counts for one hosted lab request: Prospectus calls, S3 GETs and catalog decodes.

Run as a script (``python tests/lab_request_efficiency_test.py``) to print the counts. Pointing PYTHONPATH at an
older source tree prints that tree's counts, which is how the before and after figures were taken.
"""

import json
from uuid import uuid4

from decision_flywheel.account_sessions_test import CLIENT, ISSUER, identity
from decision_flywheel.prospectus_accounts import AccountAccessDenied
from decision_flywheel.s3_console_storage_test import seed
from decision_flywheel.s3_journal_test import ConditionalS3
from decision_flywheel.s3_sessions_test import Cipher

RUN_DETAILS = (
    "query($id:ID!){run(runId:$id){id name mode status createdAt config counts labelingAccess cyclotronId}"
    " currentItem(runId:$id) jobs(runId:$id){id kind status result}}"
)
RUNS_LIST = "{runs{id name mode status createdAt config counts}}"


def classify(key):
    if key.endswith("/head.json") or key.endswith("/anchor.json"):
        return "coordination"
    return "payload"


def build(verifier, token):
    from decision_flywheel.hosted_runtime import HostedConfig, HostedRuntime
    from decision_flywheel.s3_console_storage import S3ConsoleStorage

    person_id, account_id = str(uuid4()), str(uuid4())
    prospectus = []
    s3 = ConditionalS3()
    validations = []
    original_validate = S3ConsoleStorage._validate

    def counting_validate(self, value):
        validations.append(1)
        return original_validate(self, value)

    S3ConsoleStorage._validate = counting_validate
    summary = {
        "id": account_id,
        "name": "Synthetic tenant",
        "role": "owner",
        "entitlement": {
            "accountId": account_id,
            "product": "cyclotron",
            "status": "active",
            "capabilities": ["console:read", "console:write"],
        },
    }

    def central(method, path, human, body=None, key=None, **kwargs):
        verifier.access(human)
        prospectus.append(path.replace(account_id, "{account}"))
        if path == "/v1/identities/me":
            return {"person": {"id": person_id, "createdAt": "synthetic"}}
        if path == "/v1/accounts":
            return {"person": {"id": person_id}, "accounts": [summary]}
        if path == f"/v1/accounts/{account_id}/authorize":
            return {
                "allowed": True,
                "accountId": account_id,
                "personId": person_id,
                "product": "cyclotron",
                "role": "owner",
                "operation": body["operation"],
                "revision": 1,
            }
        raise AccountAccessDenied("not authorized")

    config = HostedConfig(
        issuer=ISSUER,
        client_id=CLIENT,
        authorization_origin="https://synthetic.auth.example.invalid",
        public_origin="https://console.example.test",
        account="111111111111",
        region="us-east-1",
        books_bucket="synthetic-books",
        auth_bucket="synthetic-auth",
        session_key_arn="arn:aws:kms:us-east-1:111111111111:key/11111111-1111-4111-8111-111111111111",
        queue_url="https://sqs.us-east-1.amazonaws.com/111111111111/synthetic",
        central_endpoint="https://central.example.test/api",
    )
    runtime = HostedRuntime(config, s3=s3, cipher=Cipher(), central=central, verifier=verifier, send=lambda message: None)
    store = runtime.console(account_id, initialize=True)
    seed(store)
    run = store.create_run("Synthetic run", "recorded", {})
    for number in range(4):
        store.create_run(f"Other run {number}", "recorded", {})
    access = token()
    cookie, session = runtime.sessions.create(access, verifier.access(access))
    session.person_id = person_id
    runtime.sessions.save(session)
    return runtime, config, account_id, run["id"], cookie, prospectus, s3, validations, original_validate


def tally(prospectus, s3, validations, mark):
    gets = [request for kind, request in s3.calls[mark[1] :] if kind == "get"]
    books = [request for request in gets if request["Bucket"] == "synthetic-books"]
    return {
        "prospectus_calls": len(prospectus) - mark[0],
        "identities_me": prospectus[mark[0] :].count("/v1/identities/me"),
        "authorize": sum(path.endswith("/authorize") for path in prospectus[mark[0] :]),
        "s3_gets": len(gets),
        "s3_book_gets": len(books),
        "catalog_payload_gets_each_a_decode": sum(classify(r["Key"]) == "payload" for r in books),
        "catalog_validations": len(validations) - mark[2],
    }


def measure(verifier, token):
    from fastapi.testclient import TestClient

    runtime, config, account_id, run_id, cookie, prospectus, s3, validations, original = build(verifier, token)
    results = {}
    try:
        with TestClient(runtime.app(), base_url=config.public_origin) as client:
            client.cookies.set("__Host-cyclotron-session", cookie)
            headers = {"Origin": config.public_origin}
            requests = {
                "run_details_first_in_process": lambda: client.post(
                    "/graphql",
                    params={"account": account_id},
                    headers=headers,
                    json={"query": RUN_DETAILS, "variables": {"id": run_id}},
                ),
                "run_details_second": lambda: client.post(
                    "/graphql",
                    params={"account": account_id},
                    headers=headers,
                    json={"query": RUN_DETAILS, "variables": {"id": run_id}},
                ),
                "events_page": lambda: client.get(f"/api/runs/{run_id}/events", params={"account": account_id}),
                "runs_list": lambda: client.post(
                    "/graphql", params={"account": account_id}, headers=headers, json={"query": RUNS_LIST}
                ),
            }
            for name, send in requests.items():
                mark = (len(prospectus), len(s3.calls), len(validations))
                response = send()
                assert response.status_code == 200, (name, response.text)
                assert not response.json().get("errors") if name != "events_page" else True, response.text
                results[name] = tally(prospectus, s3, validations, mark)
    finally:
        from decision_flywheel.s3_console_storage import S3ConsoleStorage

        S3ConsoleStorage._validate = original
    return results


def test_a_hosted_request_makes_one_identity_check_one_authorize_and_at_most_one_catalog_decode(identity):
    results = measure(*identity)
    for name, counts in results.items():
        assert counts["identities_me"] == 1 and counts["authorize"] == 1, (name, counts)
        assert counts["prospectus_calls"] == 2, (name, counts)
        assert counts["catalog_validations"] <= 1, (name, counts)
    assert results["run_details_first_in_process"]["catalog_payload_gets_each_a_decode"] <= 1
    for name in ("run_details_second", "events_page", "runs_list"):
        assert results[name]["catalog_payload_gets_each_a_decode"] == 0, (name, results[name])


if __name__ == "__main__":
    print(json.dumps(measure(*identity.__wrapped__()), indent=2))
