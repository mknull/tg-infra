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
cat <<'NOTICE'
Personal job alerts: follow selected sources and match jobs to your profile.
This installer currently uses Telegram for alerts and commands. Prepare a Telegram
account and dedicated bot token, a funded DeepSeek API key, and CV/notes or a written profile.
Generation and ongoing matching use paid API calls and send text to DeepSeek.
Scheduled operation needs Linux with user systemd; keep the host awake/online and the checkout in place.
Setup first prepares jobsmcp/ and shared Python packages. Review features before any services start.
Preparation details: README-v2.md and docs/installation.md. Run setup as your normal user, without sudo.

NOTICE
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
