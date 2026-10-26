import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts/dev/check_lab_template.py"
_SPEC = importlib.util.spec_from_file_location("check_lab_template", _PATH)
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)


# Record-lake prefix (S1): three list appends. Emptied again after the S3 deploy refreshes the snapshot.
REVIEWED_PENDING_PATHS = ["/Resources/MachineInvokerRole/Properties/Policies/0/PolicyDocument/Statement/0/Resource/-"] * 2
VERIFIER_ARN = {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/cyclotron-lab-verifier"}


def expected():
    return check.expected_template(check.load_snapshot(), check.load_pending())


def test_lab_template_equals_the_deployed_snapshot_plus_the_reviewed_pending_changes():
    assert check.diff_paths(check.load_template(), expected()) == []


def test_pending_changes_are_exactly_the_reviewed_add_only_set():
    pending = check.load_pending()
    assert [operation["path"] for operation in pending] == REVIEWED_PENDING_PATHS
    assert {operation["op"] for operation in pending} <= {"add"}


def test_pending_changes_are_each_described_in_the_reviewer_note():
    note = (Path(check.PENDING).with_suffix(".md")).read_text()
    for path in REVIEWED_PENDING_PATHS:
        assert path in note


def test_an_unlisted_change_to_the_template_cannot_hide():
    template = check.load_template()
    template["Resources"]["BffFunction"]["Properties"]["Timeout"] = 29
    assert any("Timeout" in problem for problem in check.diff_paths(template, expected()))
    template = check.load_template()
    template["Resources"]["LabVerifierRole"]["Properties"]["MaxSessionDuration"] = 43200
    assert any("MaxSessionDuration" in problem for problem in check.diff_paths(template, expected()))


def test_pending_operations_may_only_add_new_paths():
    snapshot = check.load_snapshot()
    for bad in (
        {"op": "replace", "path": "/Description", "value": "x"},
        {"op": "add", "path": "/Description", "value": "x"},
        {"op": "add", "path": "/Resources/BffRole/Properties/Policies/0/PolicyDocument/Statement/0", "value": {}},
        {"op": "add", "path": "/Parameters/Extra", "value": 1, "extra": True},
    ):
        with pytest.raises(ValueError):
            check.apply_pending(snapshot, [bad])
    assert check.apply_pending(snapshot, []) == snapshot


def test_live_check_compares_the_deployed_stack_with_the_snapshot_not_the_pending_template(monkeypatch, capsys):
    snapshot = check.load_snapshot()
    monkeypatch.setattr(check, "fetch_live", lambda stack, region: snapshot)
    assert check.main(["--live"]) == 0
    assert "snapshot vs deployed cyclotron-console (Original): MATCH" in capsys.readouterr().out
    drifted = json.loads(json.dumps(snapshot))
    drifted["Resources"]["BffFunction"]["Properties"]["Timeout"] = 29
    monkeypatch.setattr(check, "fetch_live", lambda stack, region: drifted)
    assert check.main(["--live"]) == 1
    assert "live" in capsys.readouterr().out


def test_lab_verifier_role_trusts_only_the_bff_role_and_may_invoke_only_two_routes():
    role = check.load_template()["Resources"]["LabVerifierRole"]["Properties"]
    assert role["RoleName"] == "cyclotron-lab-verifier" and "Path" not in role
    assert role["AssumeRolePolicyDocument"]["Statement"] == [
        {
            "Effect": "Allow",
            "Principal": {"AWS": {"Fn::GetAtt": ["BffRole", "Arn"]}},
            "Action": "sts:AssumeRole",
        }
    ]
    (policy,) = role["Policies"]
    assert policy["PolicyDocument"]["Statement"] == [
        {
            "Effect": "Allow",
            "Action": "execute-api:Invoke",
            "Resource": [
                "arn:aws:execute-api:us-east-1:712236451410:bnh9mctvo9/api/GET/v1/identities/me",
                "arn:aws:execute-api:us-east-1:712236451410:bnh9mctvo9/api/POST/v1/accounts/*/authorize",
            ],
        }
    ]


def test_only_the_bff_role_may_assume_the_verifier_and_the_worker_may_not():
    resources = check.load_template()["Resources"]
    grants = {}
    for name, resource in resources.items():
        if resource["Type"] != "AWS::IAM::Role":
            continue
        for policy in resource["Properties"].get("Policies", []):
            for statement in policy["PolicyDocument"]["Statement"]:
                actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
                if "sts:AssumeRole" in actions:
                    grants.setdefault(name, []).append(statement["Resource"])
    assert grants == {"BffRole": [VERIFIER_ARN]}


def test_verifier_configuration_is_absent_from_the_bff_environment_unless_enabled_and_never_reaches_the_worker():
    template = check.load_template()
    assert template["Parameters"]["LabVerifierEnabled"]["Default"] == "false"
    bff = template["Resources"]["BffFunction"]["Properties"]["Environment"]["Variables"]
    worker = template["Resources"]["WorkerFunction"]["Properties"]["Environment"]["Variables"]
    for name in ("CYCLOTRON_LAB_VERIFIER_ROLE_ARN", "CYCLOTRON_ACCOUNTS_API_URL"):
        assert bff[name]["Fn::If"][0] == "HasLabVerifier" and bff[name]["Fn::If"][2] == {"Ref": "AWS::NoValue"}
        assert name not in worker
    assert bff["CYCLOTRON_LAB_VERIFIER_ROLE_ARN"]["Fn::If"][1] == VERIFIER_ARN
    assert bff["CYCLOTRON_ACCOUNTS_API_URL"]["Fn::If"][1] == "https://bnh9mctvo9.execute-api.us-east-1.amazonaws.com/api/"


def test_lab_template_keeps_the_machine_route_on_iam_auth():
    paths = check.load_template()["Resources"]["ConsoleApi"]["Properties"]["Body"]["paths"]
    assert paths["/machine/{proxy+}"]["x-amazon-apigateway-any-method"]["security"] == [{"AWS_IAM": []}]
