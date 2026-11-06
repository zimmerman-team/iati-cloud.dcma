#!/bin/bash
# Upgrade specific packages (e.g. ones with reported CVEs) in uv.lock, then run all checks.
# Usage: scripts/upgrade_affected_dep.sh <package> [<package> ...]
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -eq 0 ]; then
    echo "Usage: $0 <package> [<package> ...]" >&2
    exit 1
fi

UPGRADE_ARGS=()
for PACKAGE in "$@"; do
    UPGRADE_ARGS+=(--upgrade-package "$PACKAGE")
done

uv lock "${UPGRADE_ARGS[@]}"
uv sync --extra dev

make check-all
uv run pre-commit run --all-files
