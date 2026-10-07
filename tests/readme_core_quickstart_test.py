"""The documented headless-core quickstart stays runnable without the web app."""
from pathlib import Path


def test_the_headless_core_quickstart_runs_without_fastapi_or_react(tmp_path, monkeypatch, capsys):
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    code = readme.split("<!-- core-quickstart:start -->", 1)[1].split("<!-- core-quickstart:end -->", 1)[0]
    code = code.removeprefix("\n```python\n").removesuffix("\n```\n")
    monkeypatch.chdir(tmp_path)
    namespace = {"__name__": "__main__"}
    exec(compile(code, "README core quickstart", "exec"), namespace)
    assert capsys.readouterr().out.splitlines() == ["include 0.8", "prediction"]
