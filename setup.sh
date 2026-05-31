#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/jobsmcp"
PYTHON="$VENV/bin/python3"
STATE_DIR="$PROJECT_DIR/state"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
LOG_FILE="$STATE_DIR/setup.log"

mkdir -p "$STATE_DIR"

log() { echo "[setup] $*" | tee -a "$LOG_FILE"; }
skip() { log "SKIP: $*"; }
step() { log "----"; log "STEP: $*"; }

# ---------- step 1: prerequisites ----------

step "Checking prerequisites"

if ! command -v python3 &>/dev/null; then
    log "ERROR: python3 not found. Install Python >= 3.10."
    exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log "Python $PY_VER"

if ! command -v docker &>/dev/null; then
    log "WARNING: docker not found. SearXNG step skipped. Install docker.io."
else
    docker ps &>/dev/null || log "WARNING: docker installed but not accessible — SearXNG step skipped. Add yourself to docker group."
fi
HAS_DOCKER=false
command -v docker &>/dev/null && docker ps &>/dev/null && HAS_DOCKER=true

# ---------- step 2: venv ----------

step "Virtual environment"

if [[ ! -x "$PYTHON" ]]; then
    python3 -m venv "$VENV"
    log "Created venv at $VENV"
else
    skip "venv already exists"
fi

log "Installing pip dependencies"
"$PYTHON" -m pip install -q -r "$PROJECT_DIR/requirements.txt"
if ! "$PYTHON" -c "import playwright" 2>/dev/null; then
    skip "playwright not installed"
else
    "$PYTHON" -m playwright install chromium >/dev/null 2>&1 || log "WARNING: playwright chromium install failed"
fi

# ---------- step 3: SearXNG ----------

step "SearXNG"

if $HAS_DOCKER; then
if docker ps -a --format '{{.Names}}' | grep -q '^searxng$'; then
    if docker ps --format '{{.Names}}' | grep -q '^searxng$'; then
        skip "SearXNG already running"
    else
        docker start searxng
        log "SearXNG started"
    fi
else
    mkdir -p "$STATE_DIR/searxng"
    cat > "$STATE_DIR/searxng/settings.yml" <<'YML'
use_default_settings: true
server:
  secret_key: "briefme-search-secret-change-in-production"
  bind_address: "0.0.0.0"
  limiter: false
  public_instance: false
search:
  formats:
    - html
    - json
YML
    docker run -d --name searxng --restart unless-stopped \
        -p 127.0.0.1:8080:8080 \
        -v "$STATE_DIR/searxng/settings.yml:/etc/searxng/settings.yml:ro" \
        searxng/searxng
    log "SearXNG container created and started"
fi
fi  # HAS_DOCKER

# ---------- step 4: state directories ----------

step "State directories"

mkdir -p "$STATE_DIR/message_queue"
mkdir -p "$STATE_DIR/messages"
mkdir -p "$STATE_DIR/audit"
mkdir -p "$PROJECT_DIR/workspace/briefs"
log "Created state directory structure"

# ---------- step 5: .env template ----------

step "Environment file"

ENV_FILE="$STATE_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<'EOF'
# REQUIRED
TELEGRAM_BOT_TOKEN=     # from @BotFather
TELEGRAM_API_ID=        # from https://my.telegram.org/apps
TELEGRAM_API_HASH=      # from https://my.telegram.org/apps
TELEGRAM_CHAT_ID=       # your Telegram chat ID (from @userinfobot)
DEEPSEEK_API_KEY=       # from https://platform.deepseek.com
USER_NAME=              # your name, used in agent prompts

# OUTLOOK (optional — skip if not using email triage)
OUTLOOK_CLIENT_ID=      # Azure AD app registration ID
OUTLOOK_EMAIL=          # your email address

# OPTIONAL
SEARXNG_URL=http://localhost:8080/search
EOF
    chmod 600 "$ENV_FILE"
    log "Created $ENV_FILE — fill in your keys"
else
    skip ".env already exists"
fi

# ---------- step 6: channels config ----------

step "Channel configuration"

CHANNELS_FILE="$STATE_DIR/channels.json"
if [[ ! -f "$CHANNELS_FILE" ]]; then
    cat > "$CHANNELS_FILE" <<'JSON'
{
  "channels": [
    {
      "username": "CHANGE_ME",
      "queue_prefix": "jobs",
      "description": "What this channel posts — describe it",
      "message_format": "How messages look — length, structure, typical content",
      "desired_roles": "Roles you want to find here",
      "acceptable_roles": "Roles you will also consider"
    }
  ]
}
JSON
    log "Created $CHANNELS_FILE — edit with your Telegram channels"
else
    skip "channels.json already exists"
fi

# ---------- step 7: profile generation ----------

step "Profile generation"

if [[ -f "$STATE_DIR/.env" ]]; then
    DEEPSEEK_KEY=$("$PYTHON" -c "
import sys; sys.path.insert(0, '$PROJECT_DIR')
from lib.config import load_env
print(load_env().get('DEEPSEEK_API_KEY', ''))
" 2>/dev/null || true)
fi

SOURCE_DIR="$PROJECT_DIR/source"
if [[ -d "$SOURCE_DIR" ]] && [[ -f "$SOURCE_DIR/interests.txt" ]]; then
    skip "source/ already exists"
    log "To regenerate: $PYTHON generate-profile.py --docs <dir> --name NAME --force"
else
    if [[ -n "${DEEPSEEK_KEY:-}" ]]; then
        log ""
        log "Profile generation creates source/interests.txt, skills.txt, tech_stack.txt"
        log "from your documents (CV, transcripts, theses, letters)."
        log ""
        read -p "Generate profile now? (y/N) " -r GEN_REPLY
        if [[ "$GEN_REPLY" =~ ^[Yy]$ ]]; then
            read -p "Path to documents directory: " -r DOCS_DIR
            read -p "Your full name: " -r USER_NAME
            "$PYTHON" "$PROJECT_DIR/generate-profile.py" \
                --docs "$DOCS_DIR" --name "$USER_NAME" || log "WARNING: generation failed"
        else
            log "Skipped. Run later: $PYTHON generate-profile.py --docs <dir> --name NAME"
        fi
    else
        log "Set DEEPSEEK_API_KEY in .env first, then run:"
        log "  $PYTHON generate-profile.py --docs <dir> --name NAME"
    fi
fi

# ---------- step 8: Telethon auth ----------

step "Telethon authentication"

SESSION_FILE="$STATE_DIR/it-jobs-session.session"
if [[ -f "$SESSION_FILE" ]]; then
    skip "Telethon session exists"
else
    log ""
    log "Telethon needs to log in to your Telegram account once."
    log "You will be prompted for your phone number and a verification code."
    log ""
    read -p "Authenticate now? (y/N) " -r AUTH_REPLY
    if [[ "$AUTH_REPLY" =~ ^[Yy]$ ]]; then
        "$PYTHON" "$PROJECT_DIR/it-jobs-poller.py" || log "WARNING: auth failed — re-run setup.sh"
    else
        log "Run manually: $PYTHON it-jobs-poller.py"
    fi
fi

# ---------- step 9: Outlook auth ----------

step "Outlook authentication"

TOKEN_FILE="$STATE_DIR/outlook-token.json"
if [[ -f "$TOKEN_FILE" ]]; then
    skip "outlook-token.json exists"
    log "To re-auth: $PYTHON outlook-auth.py"
else
    log ""
    log "Outlook email access requires an OAuth token."
    log "You need OUTLOOK_CLIENT_ID set in state/.env first."
    log ""
    read -p "Authenticate Outlook now? (y/N) " -r OAUTH_REPLY
    if [[ "$OAUTH_REPLY" =~ ^[Yy]$ ]]; then
        "$PYTHON" "$PROJECT_DIR/outlook-auth.py" || log "WARNING: auth failed — re-run setup.sh"
    else
        log "Run manually: $PYTHON outlook-auth.py"
    fi
fi

# ---------- step 10: systemd units ----------

step "Systemd units"
mkdir -p "$SYSTEMD_DIR"

declare -A UNITS
# Each entry: "name" => "description|schedule|exec"
UNITS=(
    [bot-commands]="Process Telegram bot commands|*:*:00,30|$PYTHON $PROJECT_DIR/bot-commands.py"
    [telegram-poll]="Poll Telegram groups|*-*-* *:05,35:00|$PYTHON $PROJECT_DIR/it-jobs-poller.py"
    [job-triage]="Run triage on queued messages|*-*-* *:20,50:00|$PYTHON $PROJECT_DIR/it-jobs-triage.py"
    [email-ingest]="Fetch and triage Outlook emails|*-*-* */2:45:00|$PROJECT_DIR/email-ingest-wrap"
    [weekly-trend]="Weekly market trend report|Sun *-*-* 10:00:00|$PYTHON $PROJECT_DIR/weekly-trend.py"
    [feedback-poller]="Poll Outlook for replies to weekly reports|*-*-* *:15,45:00|$PYTHON $PROJECT_DIR/feedback-poller.py"
)

for name in "${!UNITS[@]}"; do
    IFS='|' read -r desc schedule exec_cmd <<< "${UNITS[$name]}"

    svc="$SYSTEMD_DIR/$name.service"
    if [[ ! -f "$svc" ]]; then
        cat > "$svc" <<UNIT
[Unit]
Description=$desc
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$PROJECT_DIR
ExecStart=$exec_cmd
StandardOutput=append:$STATE_DIR/$name.log
StandardError=append:$STATE_DIR/$name.log
UNIT
        log "Created $svc"
    else
        skip "$name.service exists"
    fi

    tmr="$SYSTEMD_DIR/$name.timer"
    if [[ ! -f "$tmr" ]]; then
        cat > "$tmr" <<UNIT
[Unit]
Description=Run $name every interval

[Timer]
OnCalendar=$schedule
Persistent=true

[Install]
WantedBy=timers.target
UNIT
        log "Created $tmr"
    else
        skip "$name.timer exists"
    fi
done

systemctl --user daemon-reload 2>/dev/null || true

# ---------- summary ----------

log ""
log "========================================"
log "Setup complete."
log ""
log "Next steps:"
log "  1. Fill in $ENV_FILE with your keys"
log "  2. Edit $CHANNELS_FILE with your Telegram channels"
if [[ ! -f "$SOURCE_DIR/interests.txt" ]]; then
    log "  3. Generate your profile: $PYTHON generate-profile.py --docs <dir> --name NAME"
fi
if [[ ! -f "$SESSION_FILE" ]]; then
    log "  4. Authenticate Telethon: $PYTHON it-jobs-poller.py"
fi
if [[ ! -f "$TOKEN_FILE" ]]; then
    log "  5. Authenticate Outlook: $PYTHON outlook-auth.py"
fi
log "  6. Enable timers:"
for name in "${!UNITS[@]}"; do
    log "     systemctl --user enable --now $name.timer"
done
log "========================================"
