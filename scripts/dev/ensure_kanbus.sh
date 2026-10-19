#!/usr/bin/env bash
# Ensures the pinned Kanbus CLI is available as `kbs`, so cloud sessions can use the project board.
#
# Kanbus goes into a dedicated virtualenv rather than the system Python: in Debian-based
# containers `pip install` fails to upgrade distro-owned packages such as PyYAML
# ("Cannot uninstall PyYAML ... RECORD file not found"). An existing `kbs` is never replaced.
#
# Environment overrides: KANBUS_VENV (default ~/.cache/cyclotron/kanbus-venv),
# KANBUS_BIN_DIR (default ~/.local/bin).
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
pinned_requirement="$(sed -n 's/.*"\(kanbus==[^"]*\)".*/\1/p' "${project_root}/pyproject.toml" | head -n 1)"
if [ -z "${pinned_requirement}" ]; then
  echo "ensure_kanbus: no kanbus==<version> pin found in ${project_root}/pyproject.toml" >&2
  exit 1
fi
pinned_version="${pinned_requirement#kanbus==}"

venv_dir="${KANBUS_VENV:-${HOME}/.cache/cyclotron/kanbus-venv}"
bin_dir="${KANBUS_BIN_DIR:-${HOME}/.local/bin}"

if command -v kbs >/dev/null 2>&1; then
  installed_version="$(kbs --version 2>&1 || true)"
  case "${installed_version}" in
    *"${pinned_version}"*) ;;
    *) echo "ensure_kanbus: warning: kbs reports '${installed_version}' but pyproject.toml pins ${pinned_requirement}; leaving the existing install alone" >&2 ;;
  esac
  echo "${installed_version}"
  exit 0
fi

if [ ! -x "${venv_dir}/bin/python" ]; then
  mkdir -p "$(dirname "${venv_dir}")"
  python3 -m venv "${venv_dir}"
fi
"${venv_dir}/bin/python" -m pip install --quiet --disable-pip-version-check "${pinned_requirement}"

kanbus_executable=""
for candidate in kbs kanbus; do
  if [ -x "${venv_dir}/bin/${candidate}" ]; then
    kanbus_executable="${venv_dir}/bin/${candidate}"
    break
  fi
done
if [ -z "${kanbus_executable}" ]; then
  echo "ensure_kanbus: ${pinned_requirement} installed in ${venv_dir} but it has no kbs or kanbus executable" >&2
  exit 1
fi

mkdir -p "${bin_dir}"
ln -sf "${kanbus_executable}" "${bin_dir}/kbs"

case ":${PATH}:" in
  *":${bin_dir}:"*) ;;
  *) echo "ensure_kanbus: ${bin_dir} is not on PATH; add it (export PATH=\"${bin_dir}:\$PATH\") to use kbs" >&2 ;;
esac

"${bin_dir}/kbs" --version
