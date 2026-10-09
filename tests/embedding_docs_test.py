"""Every code sample in docs/embedding.md runs, and the quoted ones match their sources."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).parents[1]
DOC = (ROOT / "docs" / "embedding.md").read_text()


def blocks(language):
    return re.findall(rf"```{language}\n(.*?)```", DOC, re.DOTALL)


def test_the_python_samples_run_in_order_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    namespace = {"__name__": "__main__"}
    samples = blocks("python")
    assert len(samples) >= 10
    for index, sample in enumerate(samples):
        exec(compile(sample, f"docs/embedding.md python sample {index + 1}", "exec"), namespace)
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "include 0.9 1"
    assert "another writer holds the store" in out
    assert out[-1] == "1 labels replayed"
    assert namespace["cyclotron"].db is None and namespace["restored"].db is None  # closed
    assert (tmp_path / "var" / "snapshots" / "papyrus-relevance.tar.gz").exists()


def test_the_tsx_sample_is_the_tested_example_file():
    source = (ROOT / "trace-ui" / "src" / "examples" / "embedding.tsx").read_text()
    quoted = source.split("// example:start\n", 1)[1].split("// example:end", 1)[0]
    assert blocks("tsx") == [quoted]


def test_the_pinned_tarball_matches_the_package_version():
    version = json.loads((ROOT / "package.json").read_text())["version"]
    pin = json.loads("{" + blocks("json")[0] + "}")["dependencies"]["cyclotron"]
    assert pin.endswith(f"/ui-v{version}/cyclotron-{version}.tgz")
