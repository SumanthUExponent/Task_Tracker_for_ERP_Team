"""Per-person settings, kept outside the repo so the code is the same for everyone.

~/.config/personal-assistant/config.env (mode 600) holds KEY=VALUE lines:
    SLACK_BOT_TOKEN      the Daily Brief bot token (xoxb-…)
    SLACK_USER_ID        your Slack member id (U…), the DM the briefs go to
    OWNER_NAME           your full name as it appears in Slack, Gmail and meeting notes
    MEETINGS_DIR         optional: where meeting notes are archived (a Google Drive synced folder)
    MEETINGS_DRIVE_URL   optional: that folder's Drive link, shown in the daily MoM sync note
"""
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "personal-assistant" / "config.env"


def load(path=CONFIG_PATH):
    settings = {}
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return settings
    for line in lines:
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key.strip()] = value.strip().strip("'\"")
    return settings


def require(settings, key):
    value = settings.get(key, "")
    if not value:
        raise SystemExit(f"{key} is not set in {CONFIG_PATH} — see CLAUDE.md, step 'Configure'")
    return value
