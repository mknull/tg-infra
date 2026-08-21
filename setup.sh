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
# Fill in the value after each '='. Lines starting with # are comments;
# do NOT put a comment on the same line as a value.
#
# --- REQUIRED ---
# from @BotFather
TELEGRAM_BOT_TOKEN=
# both from https://my.telegram.org/apps
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
# leave blank — set automatically by: ./setup_verify.py pair
TELEGRAM_CHAT_ID=
# from https://platform.deepseek.com
DEEPSEEK_API_KEY=
# your name, used in agent prompts
USER_NAME=
#
# --- OUTLOOK (optional — leave blank to disable email triage) ---
# Azure AD app registration ID
OUTLOOK_CLIENT_ID=
# your email address
OUTLOOK_EMAIL=
# mail FOLDER to triage, e.g. "mailing lists". Blank = email off.
# Your whole Inbox is refused unless you also uncomment the CONFIRM line.
OUTLOOK_FOLDER=
# OUTLOOK_FOLDER_CONFIRM=usemyinboxitsfine
#
# --- OPTIONAL ---
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
    log "To regenerate: $PYTHON generate_profile.py --docs <dir> --name NAME --force"
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
            "$PYTHON" "$PROJECT_DIR/generate_profile.py" \
                --docs "$DOCS_DIR" --name "$USER_NAME" || log "WARNING: generation failed"
        else
            log "Skipped. Run later: $PYTHON generate_profile.py --docs <dir> --name NAME"
        fi
    else
        log "Set DEEPSEEK_API_KEY in .env first, then run:"
        log "  $PYTHON generate_profile.py --docs <dir> --name NAME"
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
        "$PYTHON" "$PROJECT_DIR/it_jobs_poller.py" || log "WARNING: auth failed — re-run setup.sh"
    else
        log "Run manually: $PYTHON it_jobs_poller.py"
    fi
fi

# ---------- step 9: Outlook auth ----------

step "Outlook authentication"

TOKEN_FILE="$STATE_DIR/outlook-token.json"
if [[ -f "$TOKEN_FILE" ]]; then
    skip "outlook-token.json exists"
    log "To re-auth: $PYTHON outlook_auth.py"
else
    log ""
    log "Outlook email access requires an OAuth token."
    log "You need OUTLOOK_CLIENT_ID set in state/.env first."
    log ""
    read -p "Authenticate Outlook now? (y/N) " -r OAUTH_REPLY
    if [[ "$OAUTH_REPLY" =~ ^[Yy]$ ]]; then
        "$PYTHON" "$PROJECT_DIR/outlook_auth.py" || log "WARNING: auth failed — re-run setup.sh"
    else
        log "Run manually: $PYTHON outlook_auth.py"
    fi
fi

# ---------- step 10: systemd units ----------

step "Systemd units"
mkdir -p "$SYSTEMD_DIR"

declare -A UNITS
# Each entry: "name" => "description|schedule|exec|optional TimeoutStartSec"
UNITS=(
    [bot-commands]="Process Telegram bot commands|*:*:00,30|$PYTHON $PROJECT_DIR/bot_commands.py"
    # telegram-poll needs the timeout: if the connection drops mid-request,
    # Telethon's send() awaits a response that never comes (no timeout), the
    # oneshot unit then stays "activating" forever, and the timer skips every
    # later fire. systemd killing the hung run lets the next fire backfill.
    [telegram-poll]="Poll Telegram groups|*-*-* *:05,35:00|$PYTHON $PROJECT_DIR/it_jobs_poller.py|15min"
    [job-triage]="Run triage on queued messages|*-*-* *:20,50:00|$PYTHON $PROJECT_DIR/it_jobs_triage.py"
    [email-ingest]="Fetch and triage Outlook emails|*-*-* 0/2:45:00|$PROJECT_DIR/email-ingest-wrap"
    [weekly-trend]="Weekly market trend report|Sun *-*-* 10:00:00|$PYTHON $PROJECT_DIR/weekly_trend.py"
    [weekly-recovery]="Retry the weekly report until confirmed sent (runs 5 min before email-ingest)|*-*-* 0/2:40:00|$PYTHON $PROJECT_DIR/weekly_recovery.py"
    [feedback-poller]="Poll Outlook for replies to weekly reports|*-*-* *:15,45:00|$PYTHON $PROJECT_DIR/feedback_poller.py"
    [delivery-canary]="Synthetic delivery check (audit --health alerts if it breaks)|*-*-* *:10:00|$PYTHON $PROJECT_DIR/delivery_canary.py"
)

for name in "${!UNITS[@]}"; do
    IFS='|' read -r desc schedule exec_cmd timeout <<< "${UNITS[$name]}"
    timeout_line=""
    [[ -n "$timeout" ]] && timeout_line="TimeoutStartSec=$timeout"

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
$timeout_line
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

# ---------- step 11: pair, verify, and (only then) activate ----------

step "Verify & activate"

CHAT_ID=$("$PYTHON" -c "from lib import load_env; print(load_env().get('TELEGRAM_CHAT_ID','').strip())" 2>/dev/null || true)
if [[ -z "${CHAT_ID:-}" ]]; then
    log "No Telegram chat paired yet."
    read -p "Pair now (you'll send your bot a message)? (y/N) " -r PAIR_REPLY
    if [[ "$PAIR_REPLY" =~ ^[Yy]$ ]]; then
        "$PYTHON" "$PROJECT_DIR/setup_verify.py" pair \
            || log "pairing failed — run later: $PYTHON setup_verify.py pair"
    fi
fi

ACTIVATED=false
if "$PYTHON" "$PROJECT_DIR/setup_verify.py" verify; then
    log "verify passed — activating timers"
    for name in "${!UNITS[@]}"; do
        if systemctl --user enable --now "$name.timer" 2>/dev/null; then
            log "  activated $name.timer"
        else
            skip "could not activate $name.timer"
        fi
    done
    ACTIVATED=true
else
    log "verify did NOT pass — timers left disabled (the system is not live)."
fi

# ---------- summary ----------

log ""
log "========================================"
if $ACTIVATED; then
    log "Setup complete — the system is LIVE (timers enabled, canary delivering)."
    log "Re-run ./setup.sh any time; it is idempotent."
    log "Check anytime:  $PYTHON setup_verify.py status"
else
    log "Setup scaffolded — NOT live yet. Finish these, then re-run ./setup.sh"
    log "(idempotent; it will pair, verify, and activate automatically):"
    log "  1. Fill in $ENV_FILE (leave TELEGRAM_CHAT_ID blank)"
    log "  2. Edit $CHANNELS_FILE with YOUR Telegram channels"
    if [[ ! -f "$SOURCE_DIR/interests.txt" ]]; then
        log "  3. Generate your profile: $PYTHON generate_profile.py --docs <dir> --name NAME"
    fi
    if [[ ! -f "$SESSION_FILE" ]]; then
        log "  4. Authenticate Telethon: $PYTHON it_jobs_poller.py"
    fi
    if [[ ! -f "$TOKEN_FILE" ]]; then
        log "  5. (email only) Authenticate Outlook: $PYTHON outlook_auth.py"
    fi
    log "  Check status anytime: $PYTHON setup_verify.py status"
fi
log "========================================"
