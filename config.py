"""
config.py
---------
Loads .env from the project directory and exposes all configuration constants
used throughout the widget.
"""

import os
from pathlib import Path

# ── .env loader ───────────────────────────────────────────────────────────────

_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Refresh ───────────────────────────────────────────────────────────────────

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes

# ── Local JSONL data ──────────────────────────────────────────────────────────

# Local Claude Code session data written by the Claude Code CLI/desktop app
CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")

# Path filter — only count sessions whose cwd starts with one of these
# (case-insensitive). Loaded from INCLUDE_PATHS in .env. Empty list = include
# everything.
_raw_paths = os.environ.get("INCLUDE_PATHS", "")
INCLUDE_PATHS: list[str] = [p.strip().lower() for p in _raw_paths.split(",") if p.strip()]

# Days excluded from limit averaging (0=Mon … 6=Sun). Defaults to Sat+Sun.
_raw_exclude = os.environ.get("EXCLUDE_WEEKDAYS", "5,6")
EXCLUDE_WEEKDAYS: set[int] = {int(d.strip()) for d in _raw_exclude.split(",") if d.strip()}

# ── Usage fetcher (Selenium) ──────────────────────────────────────────────────

# Set CONSOLE_FETCHER_ENABLED=true to enable; all other CONSOLE_* settings are
# ignored when it is disabled.  Set CONSOLE_HEADLESS=false to show the browser.
CONSOLE_FETCHER_ENABLED = os.environ.get("CONSOLE_FETCHER_ENABLED", "false").lower() == "true"
CONSOLE_REFRESH_MINUTES = int(os.environ.get("CONSOLE_REFRESH_MINUTES", "30"))
CONSOLE_HEADLESS        = os.environ.get("CONSOLE_HEADLESS", "true").lower() != "false"
CONSOLE_PROFILE_DIR     = os.path.join(os.path.expanduser("~"), ".claude_widget", "chrome_profile")

# ── Logging ───────────────────────────────────────────────────────────────────

DEBUG_LOGGING = os.environ.get("DEBUG_LOGGING", "false").lower() == "true"
