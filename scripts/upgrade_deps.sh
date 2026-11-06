#!/bin/bash
# Upgrade all dependencies, raise the minimum versions in pyproject.toml to the
# newly locked versions, update pre-commit hook revisions, then run all checks.
# Usage: scripts/upgrade_deps.sh
set -euo pipefail
cd "$(dirname "$0")/.."

uv lock --upgrade

# Direct dependencies as "name>=locked_version", split into core and dev extras.
TREE=$(uv tree --depth 1 --frozen | tail -n +2)
mapfile -t CORE_DEPS < <(echo "$TREE" | awk '!/extra: dev/ { print $2 ">=" substr($3, 2) }')
mapfile -t DEV_DEPS < <(echo "$TREE" | awk '/extra: dev/ { print $2 ">=" substr($3, 2) }')

uv add --no-sync "${CORE_DEPS[@]}"
uv add --no-sync --optional dev "${DEV_DEPS[@]}"

uv run pre-commit autoupdate
uv sync --extra dev

make check-all
uv run pre-commit run --all-files
