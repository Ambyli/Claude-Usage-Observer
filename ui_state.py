"""
ui_state.py
-----------
Persists UI state (e.g. which sections are open/closed) to
~/.claude_widget/ui_state.json between app launches.
"""

import json
from pathlib import Path

from logging_setup import log

_STATE_FILE = Path.home() / ".claude_widget" / "ui_state.json"


def load() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("ui_state: failed to load state: %s", exc)
    return {}


def save(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(
            json.dumps(state, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        log.warning("ui_state: failed to save state: %s", exc)
