"""Onboarding/setup validation — pure, side-effect-free checks.

These are the bits of setup that have real logic worth testing: which folder
is safe to monitor, whether required config is actually filled in, whether
channels.json is still the scaffolded template. Kept dependency-light so both
setup and the pipeline can call them, and so they unit-test without touching
real services. The *live* checks (getMe, a DeepSeek ping, a canary delivery)
deliberately live elsewhere and run against real services — they are not faked.
"""

INBOX_OVERRIDE = "usemyinboxitsfine"

REQUIRED_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH",
                "DEEPSEEK_API_KEY")


def resolve_monitored_folder(name: str, override: str = "") -> str:
    """Return the Outlook folder to monitor, guarding the Inbox.

    Monitoring the entire Inbox would triage every email the user receives, so
    it is refused unless the caller explicitly opts in with INBOX_OVERRIDE.
    Raises ValueError on an empty name or an unconfirmed Inbox.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError(
            "no Outlook folder configured — set OUTLOOK_FOLDER to a dedicated "
            "folder (e.g. 'mailing lists'), not your whole mailbox")
    if name.lower() == "inbox" and override.strip() != INBOX_OVERRIDE:
        raise ValueError(
            "refusing to monitor your entire Inbox — that would triage ALL "
            "incoming mail. If you really mean it, set "
            f"OUTLOOK_FOLDER_CONFIRM={INBOX_OVERRIDE}")
    return name


def missing_required(env: dict) -> list[str]:
    """Return required config keys that are absent, blank, or still a CHANGE_ME."""
    missing = []
    for k in REQUIRED_ENV:
        v = (env.get(k) or "").strip()
        if not v or v.upper().startswith("CHANGE"):
            missing.append(k)
    return missing


def channels_are_placeholder(channels: list[dict]) -> bool:
    """True if channels.json still holds the scaffolded CHANGE_ME template."""
    if not channels:
        return True
    return any((ch.get("username") or "").upper().startswith("CHANGE")
               for ch in channels)
