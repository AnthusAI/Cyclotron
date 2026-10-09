"""The documented embedded-cyclotron quickstart stays runnable offline."""
from pathlib import Path


def test_the_embedded_quickstart_decides_reviews_and_reports(tmp_path, monkeypatch, capsys):
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    code = readme.split("<!-- embedded-quickstart:start -->", 1)[1].split("<!-- embedded-quickstart:end -->", 1)[0]
    code = code.removeprefix("\n```python\n").removesuffix("\n```\n")
    monkeypatch.chdir(tmp_path)
    exec(compile(code, "README embedded quickstart", "exec"), {"__name__": "__main__"})
    assert capsys.readouterr().out.splitlines() == ["include 0.8", "0.0", "['decision', 'review']"]
