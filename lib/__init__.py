#!/usr/bin/env python3
"""Shared utilities for the telegram_MCP job pipeline — no external dependencies."""

from .api import call_deepseek, extract_json
from .audit import write_audit
from .auth import load_token, save_token, ensure_valid_token
from .config import (PROJECT_DIR, STATE_DIR, DEEPSEEK_API_URL,
                     FLASH_MODEL, PRO_MODEL, GRAPH_BASE, TOKEN_ENDPOINT,
                     TOKEN_FILE, TOKEN_REFRESH_BUFFER_S,
                     USER_NAME, TELEGRAM_CHAT_ID, load_env)
from .graph import graph_get, graph_post
from .state import read_cursor, write_cursor
from .seen import SeenLedger
from .log import setup_logging
from .delivery import (load_delivery_config, deliver,
                       send_telegram, send_telegram_document,
                       send_email)
from .direction import (DIRECTION_FILE, DIRECTION_AUDIT_FILE,
                        load_current_direction,
                        load_current_direction_small,
                        compress_current_direction,
                        update_current_direction)
from .onboarding import (resolve_monitored_folder, missing_required,
                         channels_are_placeholder, INBOX_OVERRIDE, REQUIRED_ENV)
