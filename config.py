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

# ── Browser linker (CDP) ──────────────────────────────────────────────────────

# Set CONSOLE_FETCHER_ENABLED=true to enable the "Account stats" section.
# The user clicks "Link Browser" to open Chrome; no headless mode, no Selenium.
CONSOLE_FETCHER_ENABLED = os.environ.get("CONSOLE_FETCHER_ENABLED", "false").lower() == "true"
CONSOLE_REFRESH_MINUTES = int(os.environ.get("CONSOLE_REFRESH_MINUTES", "30"))
BROWSER_PROFILE_DIR     = os.path.join(os.path.expanduser("~"), ".claude_widget", "chrome_profile")
BROWSER_DEBUG_PORT      = int(os.environ.get("BROWSER_DEBUG_PORT", "9222"))

# ── Logging ───────────────────────────────────────────────────────────────────

DEBUG_LOGGING = os.environ.get("DEBUG_LOGGING", "false").lower() == "true"
