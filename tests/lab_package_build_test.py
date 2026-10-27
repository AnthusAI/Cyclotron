import re
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_lab_package_lock_pins_every_distribution_exactly():
    lines = [
        line for line in (ROOT / "infra/lab/requirements.lock").read_text().splitlines() if line and not line.startswith("#")
    ]
    assert lines
    assert all(re.fullmatch(r"[a-z0-9-]+==[0-9][0-9A-Za-z.]*", line) for line in lines)
    names = [line.split("==")[0] for line in lines]
    assert names == sorted(names)
    assert "decision-flywheel" not in names


def test_lab_package_build_script_is_executable_and_offline_of_aws():
    script = ROOT / "scripts/dev/build_lab_package.sh"
    assert script.stat().st_mode & stat.S_IXUSR
    text = script.read_text()
    assert "--only-binary=:all:" in text
    assert "--no-deps" in text
    assert "aws " not in text
