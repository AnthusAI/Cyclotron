#!/usr/bin/env bash
# Offline: template.yaml must equal the committed deployed-template snapshot.
# Add --live (needs AWS_PROFILE with read-only access) to compare the deployed stack too.
set -euo pipefail
cd "$(dirname "$0")/../.."
exec "${PYTHON:-python3}" scripts/dev/check_lab_template.py "$@"
