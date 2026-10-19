"""scripts/dev/ensure_kanbus.sh installs the pinned Kanbus into a venv and links kbs, offline."""

import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "dev" / "ensure_kanbus.sh"
PIN = re.search(r'"kanbus==([^"]+)"', (ROOT / "pyproject.toml").read_text()).group(1)


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _sandbox(tmp_path: Path):
    """A fake python3 whose venv's pip 'installs' a stub kanbus; no network is touched."""
    shims = tmp_path / "shims"
    _executable(
        shims / "python3",
        f"""[ "$1 $2" = "-m venv" ] || exit 9
mkdir -p "$3/bin"
cat > "$3/bin/python" <<'PY'
#!/usr/bin/env bash
echo "$@" >> "$(dirname "$0")/pip.log"
[ "$1 $2 $3" = "-m pip install" ] || exit 9
cat > "$(dirname "$0")/kanbus" <<'K'
#!/usr/bin/env bash
echo "Kanbus {PIN}"
K
chmod +x "$(dirname "$0")/kanbus"
PY
chmod +x "$3/bin/python"
""",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": f"{shims}:{home}/.local/bin:/usr/bin:/bin",
    }
    return home, env


def _run(env):
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)


def test_a_missing_kbs_is_installed_into_a_venv_and_symlinked_onto_the_path(tmp_path):
    home, env = _sandbox(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"Kanbus {PIN}"
    venv = home / ".cache" / "cyclotron" / "kanbus-venv"
    assert f"-m pip install --quiet --disable-pip-version-check kanbus=={PIN}" in (venv / "bin" / "pip.log").read_text()
    link = home / ".local" / "bin" / "kbs"
    assert link.is_symlink() and link.resolve() == (venv / "bin" / "kanbus").resolve()
    assert result.stderr == ""


def test_a_bin_dir_missing_from_path_is_reported_on_stderr(tmp_path):
    _, env = _sandbox(tmp_path)
    env["PATH"] = f"{tmp_path / 'shims'}:/usr/bin:/bin"
    result = _run(env)
    assert result.returncode == 0
    assert "is not on PATH" in result.stderr


def test_an_off_path_bin_dir_is_exported_once_through_claude_env_file(tmp_path):
    home, env = _sandbox(tmp_path)
    env["PATH"] = f"{tmp_path / 'shims'}:/usr/bin:/bin"
    env_file = tmp_path / "claude-env"
    env["CLAUDE_ENV_FILE"] = str(env_file)
    first, second = _run(env), _run(env)
    assert first.returncode == 0 and second.returncode == 0
    assert env_file.read_text() == f'export PATH="{home}/.local/bin:$PATH"\n'
    assert "not on PATH" not in first.stderr + second.stderr


def test_an_existing_kbs_is_left_alone_and_a_version_mismatch_warns(tmp_path):
    home, env = _sandbox(tmp_path)
    _executable(tmp_path / "mine" / "kbs", 'echo "Kanbus 0.0.1"\n')
    env["PATH"] = f"{tmp_path / 'mine'}:{env['PATH']}"
    result = _run(env)
    assert result.returncode == 0
    assert "warning" in result.stderr and PIN in result.stderr
    assert not (home / ".cache").exists()
    assert not (home / ".local").exists()


def test_a_failed_install_exits_nonzero_with_the_reason_on_stderr(tmp_path):
    _, env = _sandbox(tmp_path)
    _executable(tmp_path / "shims" / "python3", 'echo "venv unavailable" >&2\nexit 1\n')
    result = _run(env)
    assert result.returncode != 0
    assert "venv unavailable" in result.stderr
