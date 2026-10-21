#!/usr/bin/env bash
# Build the cyclotron-console Lambda package (BffFunction and WorkerFunction share it).
#
#   scripts/dev/build_lab_package.sh [COMMIT] [OUTPUT_DIR]
#
# COMMIT      git revision whose src/decision_flywheel is packaged (default HEAD)
# OUTPUT_DIR  default build/lab (gitignored contents; never commit the zip)
#
# Third-party packages come from infra/lab/requirements.lock, installed as
# manylinux (2_34, else 2_28, else 2014) x86_64 / cp313 wheels only; Lambda python3.13 is
# Amazon Linux 2023 (glibc 2.34) (no builds, no network access to
# anything but the package index). The zip is deterministic for a given
# commit, lock and set of resolved wheels: sorted entries, fixed timestamps and modes.
# Makes no AWS calls. Prints the zip path and its sha256.
set -euo pipefail
cd "$(dirname "$0")/../.."
PYTHON="${PYTHON:-python3}"
COMMIT="${1:-HEAD}"
OUT="${2:-build/lab}"
LOCK=infra/lab/requirements.lock

SHA="$(git rev-parse --verify "$COMMIT^{commit}")"
rm -rf "$OUT/stage" "$OUT/lab.zip"
mkdir -p "$OUT/stage"

"$PYTHON" -m pip install --quiet --disable-pip-version-check --no-deps \
  --platform manylinux_2_34_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 \
  --python-version 3.13 --implementation cp --no-compile \
  --only-binary=:all: --target "$OUT/stage" -r "$LOCK"

# The application is copied from the commit (no dist-info), without specs.
git archive "$SHA" src/decision_flywheel LICENSE | tar -x -C "$OUT"
rm -rf "$OUT/stage/decision_flywheel"
mkdir -p "$OUT/stage/decision_flywheel"
cp -R "$OUT/src/decision_flywheel/." "$OUT/stage/decision_flywheel/"
cp "$OUT/LICENSE" "$OUT/stage/LICENSE.decision-flywheel"
rm -rf "$OUT/src" "$OUT/LICENSE"
find "$OUT/stage" -name '*_test.py' -path '*/decision_flywheel/*' -delete
rm -f "$OUT/stage/decision_flywheel/conftest.py"
find "$OUT/stage" \( -name __pycache__ -o -name '*.pyc' \) -prune -exec rm -rf {} +
rm -rf "$OUT/stage/bin"

"$PYTHON" -I - "$OUT/stage" "$OUT/lab.zip" <<'PY'
import os, sys, zipfile
root, dest = sys.argv[1:3]
names = []
for base, dirs, files in os.walk(root):
    dirs.sort()
    names += [os.path.relpath(os.path.join(base, f), root) for f in files]
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for n in sorted(names):
        info = zipfile.ZipInfo(n, (2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o644 << 16
        with open(os.path.join(root, n), "rb") as f:
            z.writestr(info, f.read(), zipfile.ZIP_DEFLATED, 6)
PY

echo "commit: $SHA"
echo "package: $(cd "$OUT" && pwd)/lab.zip"
echo "sha256: $(shasum -a 256 "$OUT/lab.zip" | cut -d' ' -f1)"
