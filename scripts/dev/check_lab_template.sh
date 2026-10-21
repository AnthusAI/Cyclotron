#!/usr/bin/env bash
# Offline: template.yaml must equal the committed snapshot plus infra/lab/pending-changes.json.
# Add --live (needs AWS_PROFILE with read-only access) to compare the snapshot with the deployed stack too.
set -euo pipefail
cd "$(dirname "$0")/../.."
exec "${PYTHON:-python3}" scripts/dev/check_lab_template.py "$@"
