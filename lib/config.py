"""Project constants, paths, and environment config."""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_DIR / "state"

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
FLASH_MODEL = "deepseek-v4-flash"
PRO_MODEL = "deepseek-v4-pro"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_ENDPOINT = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
TOKEN_FILE = STATE_DIR / "outlook-token.json"
TOKEN_REFRESH_BUFFER_S = 300


def load_env(path: Path | None = None) -> dict:
    env = {}
    env_file = path or STATE_DIR / ".env"
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def _get_config(key: str, default: str) -> str:
    """Read config from environment, falling back to .env file."""
    val = os.environ.get(key)
    if val:
        return val
    try:
        return load_env().get(key, default)
    except Exception:
        return default


USER_NAME = _get_config("USER_NAME", "the user")
TELEGRAM_CHAT_ID = _get_config("TELEGRAM_CHAT_ID", "")
