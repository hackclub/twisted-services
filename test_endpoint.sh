#!/usr/bin/env bash
set -euo pipefail

# Compatibility wrapper (singular name).
# For usage, run: ./test_endpoints.sh --help

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
exec "$SCRIPT_DIR/test_endpoints.sh" "$@"
