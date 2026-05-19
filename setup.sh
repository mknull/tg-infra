#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/jobsmcp"
PYTHON="$VENV/bin/python3"
STATE_DIR="$HOME/.it-jobs"
SYSTEMD_DIR="$HOME/.config/systemd/user"

echo "==> Creating venv at $VENV"
if [[ ! -x "$PYTHON" ]]; then
    python3 -m venv "$VENV"
fi

echo "==> Installing dependencies"
"$PYTHON" -m pip install -q -r "$PROJECT_DIR/requirements.txt"

echo "==> Creating state directory $STATE_DIR"
mkdir -p "$STATE_DIR/message_queue"

echo "==> Copying criteria templates"
for f in it-jobs-criteria.md email-triage-criteria.md; do
    if [[ ! -f "$STATE_DIR/$f" ]]; then
        cp "$PROJECT_DIR/$f" "$STATE_DIR/$f"
        echo "    Copied $f — edit to match your profile."
    else
        echo "    $f already exists, skipping."
    fi
done

echo "==> Checking $STATE_DIR/.env"
if [[ ! -f "$STATE_DIR/.env" ]]; then
    cat > "$STATE_DIR/.env" <<'EOF'
TELEGRAM_BOT_TOKEN=
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
DEEPSEEK_API_KEY=
OUTLOOK_CLIENT_ID=
EOF
    chmod 600 "$STATE_DIR/.env"
    echo "    Created empty .env — fill in your keys: $STATE_DIR/.env"
else
    echo "    .env already exists, skipping."
fi

echo "==> Checking Outlook token"
if [[ ! -f "$STATE_DIR/outlook-token.json" ]]; then
    echo "    WARNING: $STATE_DIR/outlook-token.json not found."
    echo "    Copy it from your previous machine, or re-run the Outlook OAuth flow."
    echo "    email-triage.py will fail until this file exists."
else
    echo "    outlook-token.json present."
fi

echo "==> Installing systemd units"
mkdir -p "$SYSTEMD_DIR"

cat > "$SYSTEMD_DIR/it-jobs.service" <<EOF
[Unit]
Description=IT-Jobs Cyprus — Telegram group poll, job triage, email triage
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON $PROJECT_DIR/it-jobs-poller.py
ExecStart=$PYTHON $PROJECT_DIR/it-jobs-triage.py
ExecStart=$PYTHON $PROJECT_DIR/email-triage.py
StandardOutput=append:$STATE_DIR/it-jobs.log
StandardError=append:$STATE_DIR/it-jobs.log
EOF

cp "$PROJECT_DIR/it-jobs.timer" "$SYSTEMD_DIR/it-jobs.timer"

systemctl --user daemon-reload
systemctl --user enable it-jobs.timer

echo ""
echo "Done. Next steps:"
echo "  1. Fill in $STATE_DIR/.env"
echo "  2. On first install, copy outlook-token.json from your previous machine:"
echo "     scp old-host:~/.it-jobs/outlook-token.json $STATE_DIR/outlook-token.json"
echo "  3. Authenticate Telethon (one-time interactive):"
echo "     $PYTHON $PROJECT_DIR/it-jobs-poller.py"
echo "  4. Start the timer:"
echo "     systemctl --user start it-jobs.timer"
