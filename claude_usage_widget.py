"""
Claude Usage Taskbar Widget
----------------------------
Displays daily and weekly Claude Code token usage in the system tray,
read directly from ~/.claude/projects/**/*.jsonl.

Requirements:
    pip install pystray Pillow

Entry point — all logic lives in the modules below:
    config.py          — .env loading and configuration constants
    logging_setup.py   — shared logger
    usage_parser.py    — JSONL parsing and get_usage_summary()
    tray_icon.py       — make_tray_icon()
    usage_popup.py     — UsagePopup (tkinter window)
    usage_fetcher.py   — UsageFetcher (Selenium account-stats scraper)
    startup.py         — Windows registry helpers
    widget.py          — ClaudeUsageWidget (orchestrator)
"""

# config must be imported first so .env is loaded before any other module
# reads environment variables.
import config  # noqa: F401 (side-effect: .env loaded, env vars set)
from logging_setup import log
from widget import ClaudeUsageWidget

if __name__ == "__main__":
    log.info("Claude Usage Widget starting up")
    ClaudeUsageWidget().run()
