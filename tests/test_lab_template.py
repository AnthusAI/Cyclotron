import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts/dev/check_lab_template.py"
_SPEC = importlib.util.spec_from_file_location("check_lab_template", _PATH)
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)


def test_lab_template_matches_the_committed_deployed_snapshot():
    assert check.diff_paths(check.load_template(), check.load_snapshot()) == []


def test_lab_template_comparison_reports_a_changed_value():
    template = check.load_template()
    template["Resources"]["BffFunction"]["Properties"]["Timeout"] = 29
    problems = check.diff_paths(template, check.load_snapshot())
    assert any("Timeout" in problem for problem in problems)


def test_lab_template_keeps_the_machine_route_on_iam_auth():
    paths = check.load_template()["Resources"]["ConsoleApi"]["Properties"]["Body"]["paths"]
    assert paths["/machine/{proxy+}"]["x-amazon-apigateway-any-method"]["security"] == [{"AWS_IAM": []}]
