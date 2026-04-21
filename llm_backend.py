"""
llm_backend.py
--------------
Helpers for toggling Claude Code between the Anthropic API and a local LLM.

Affected files:
  ~/.claude/settings.json  — adds/removes env overrides and disableLoginPrompt
  ~/.claude.json           — adds/removes a dummy API key and onboarding flag
"""

import json
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Callable

from logging_setup import log

_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
_CLAUDE_JSON = Path.home() / ".claude.json"

_LOCAL_LLM_SETTINGS_KEYS = {
    "claudeCode.disableLoginPrompt": True,
    "env": {
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
        "ANTHROPIC_BASE_URL": "http://localhost:8001",
    },
}
_LOCAL_LLM_JSON_KEYS = {
    "primaryApiKey": "sk-dummy-key",
    "hasCompletedOnboarding": True,
}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception as exc:
        log.error("Error reading %s: %s", path, exc)
        return {}


def _write_json(path: Path, data: dict):
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.error("Error writing %s: %s", path, exc)


def is_local_llm_active() -> bool:
    """Return True if the local LLM overrides are currently applied."""
    s = _read_json(_CLAUDE_SETTINGS)
    return s.get("env", {}).get("ANTHROPIC_BASE_URL") == "http://localhost:8001"


def activate_local_llm():
    """Write local-LLM overrides into ~/.claude/settings.json and ~/.claude.json."""
    s = _read_json(_CLAUDE_SETTINGS)
    s["claudeCode.disableLoginPrompt"] = True
    env = s.get("env", {})
    env.update(_LOCAL_LLM_SETTINGS_KEYS["env"])
    s["env"] = env
    _write_json(_CLAUDE_SETTINGS, s)

    j = _read_json(_CLAUDE_JSON)
    j.update(_LOCAL_LLM_JSON_KEYS)
    _write_json(_CLAUDE_JSON, j)
    log.debug("Activated local LLM backend")


def deactivate_local_llm():
    """Remove local-LLM overrides from ~/.claude/settings.json and ~/.claude.json."""
    s = _read_json(_CLAUDE_SETTINGS)
    s.pop("claudeCode.disableLoginPrompt", None)
    env = s.get("env", {})
    env.pop("CLAUDE_CODE_ATTRIBUTION_HEADER", None)
    env.pop("ANTHROPIC_BASE_URL", None)
    if not env:
        s.pop("env", None)
    else:
        s["env"] = env
    _write_json(_CLAUDE_SETTINGS, s)

    j = _read_json(_CLAUDE_JSON)
    j.pop("primaryApiKey", None)
    j.pop("hasCompletedOnboarding", None)
    _write_json(_CLAUDE_JSON, j)
    log.debug("Deactivated local LLM backend (restored Claude API)")


# ── llama-server process management ──────────────────────────────────────────

_server_proc: subprocess.Popen | None = None
_server_lock = threading.Lock()


def _build_server_cmd() -> list[str] | None:
    import config

    cmd_str = config.LLAMA_SERVER_CMD.strip()
    cmd = shlex.split(cmd_str, posix=False) if cmd_str else None
    log.debug("Built llama-server command: %s", cmd)
    return cmd


def is_server_running() -> bool:
    with _server_lock:
        return _server_proc is not None and _server_proc.poll() is None


def launch_server(on_line: Callable[[str], None] | None = None) -> str:
    """Start llama-server in a background subprocess.

    Returns an error string on failure, or empty string on success.
    *on_line* is called (from a daemon thread) with each line of server output.
    """
    global _server_proc
    with _server_lock:
        if _server_proc is not None and _server_proc.poll() is None:
            return "Server is already running."
        cmd = _build_server_cmd()
        if not cmd:
            return "LLAMA_SERVER_PATH or LLAMA_SERVER_CMD not set in .env"
        try:
            _server_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            log.debug("Launched llama-server pid=%s cmd=%s", _server_proc.pid, cmd)
        except Exception as exc:
            log.error("Failed to launch llama-server: %s", exc)
            return str(exc)

    if on_line is not None:

        def _reader():
            proc = _server_proc
            if proc is None:
                return
            for line in proc.stdout:
                on_line(line.rstrip())
            on_line("<server process ended>")

        t = threading.Thread(target=_reader, daemon=True, name="llama-server-reader")
        t.start()

    return ""


def stop_server():
    """Terminate the llama-server subprocess if it is running."""
    global _server_proc
    with _server_lock:
        proc = _server_proc
        _server_proc = None
    if proc is not None and proc.poll() is None:
        proc.terminate()
        log.debug("Terminated llama-server pid=%s", proc.pid)
