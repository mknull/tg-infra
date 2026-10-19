#!/usr/bin/env bash
# Bootstrap only. Configuration is reusable without this Linux shell wrapper.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
if ! command -v python3 >/dev/null; then
    echo 'Install Python 3.10 or newer. Ubuntu: sudo apt-get install python3 python3-venv python3-pip'
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo 'Python 3.10 or newer is required.'
    exit 1
fi
if [[ "${1:-}" == '--help' ]]; then
    exec python3 "$PROJECT_DIR/setup_cli.py" --help
fi
PYTHON="$PROJECT_DIR/jobsmcp/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
    if ! python3 -m venv "$PROJECT_DIR/jobsmcp"; then
        echo 'Virtual environment creation failed. Ubuntu: sudo apt-get install python3-venv'
        echo 'Then re-run ./setup.sh.'
        exit 1
    fi
fi
REQUIREMENTS_HASH=$(python3 -c 'import hashlib; print(hashlib.sha256(open("requirements.txt", "rb").read()).hexdigest())')
MARKER="$PROJECT_DIR/jobsmcp/.requirements-hash"
if [[ ! -f "$MARKER" || "$(<"$MARKER")" != "$REQUIREMENTS_HASH" ]]; then
    if ! "$PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt"; then
        echo 'Python dependency installation failed. Correct the reported package/network error and re-run ./setup.sh.'
        exit 1
    fi
    printf '%s\n' "$REQUIREMENTS_HASH" > "$MARKER"
fi
exec "$PYTHON" "$PROJECT_DIR/setup_cli.py" "$@"
