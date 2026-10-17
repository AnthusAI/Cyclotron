#!/usr/bin/env bash
# Installs the pinned Kanbus CLI when `kbs` is missing, so cloud sessions can use the project board.
set -euo pipefail

pinned_requirement="$(python3 - <<'PY'
import re, pathlib
text = pathlib.Path("pyproject.toml").read_text()
print(re.search(r'"(kanbus==[^"]+)"', text).group(1))
PY
)"

if ! command -v kbs >/dev/null 2>&1; then
  python3 -m pip install --quiet "${pinned_requirement}"
fi

if ! command -v kbs >/dev/null 2>&1; then
  kanbus_executable="$(command -v kanbus || true)"
  if [ -n "${kanbus_executable}" ]; then
    ln -sf "${kanbus_executable}" "$(dirname "${kanbus_executable}")/kbs"
  fi
fi

kbs --version
