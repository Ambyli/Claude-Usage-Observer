"""
chrome_launcher.py
------------------
Handles finding, launching, and managing the Chrome process used to load
claude.ai/settings/usage via CDP.  Also manages the session sentinel file
that determines whether Chrome should launch headlessly.

Public API
----------
CHROME_PATHS        — ordered list of candidate Chrome executable paths
find_chrome()       — returns the first existing Chrome path, or None
start_chrome(chrome, headless, debug_port, profile_dir, target_url) -> Popen
session_exists(profile_dir) -> bool
mark_session_ok(profile_dir)
clear_session(profile_dir)
"""

import os
import subprocess

from logging_setup import log

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(f"%CHROME_PATHS_VAR%") if os.environ.get("CHROME_PATHS_VAR") else None,
]

_SESSION_SENTINEL = "session_ok"


def find_chrome() -> str | None:
    """Return the first Chrome executable path that exists, or None."""
    return next((p for p in CHROME_PATHS if os.path.exists(p)), None)


def start_chrome(
    chrome: str,
    headless: bool,
    debug_port: int,
    profile_dir: str,
    target_url: str,
) -> subprocess.Popen:
    args = [
        chrome,
        f"--remote-debugging-port={debug_port}",
        f"--remote-allow-origins=http://localhost:{debug_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        args += [
            "--headless=new",
            "--disable-gpu",
            "--window-size=1920,1080",
            # Suppress headless indicators that sites use to detect automation
            "--disable-blink-features=AutomationControlled",
            (
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/134.0.0.0 Safari/537.36"
            ),
        ]
        log.debug("chrome_launcher.start_chrome: launching headless")
    else:
        log.debug("chrome_launcher.start_chrome: launching visible")
    args.append(target_url)
    return subprocess.Popen(args, stderr=subprocess.DEVNULL)


def session_exists(profile_dir: str) -> bool:
    """True if a previous successful fetch left a sentinel file."""
    return os.path.exists(os.path.join(profile_dir, _SESSION_SENTINEL))


def mark_session_ok(profile_dir: str):
    """Write the sentinel file so future launches can be headless."""
    try:
        open(os.path.join(profile_dir, _SESSION_SENTINEL), "w").close()
    except Exception as exc:
        log.warning("chrome_launcher.mark_session_ok: could not write sentinel: %s", exc)


def clear_session(profile_dir: str):
    """Remove the sentinel file (session expired / login required)."""
    try:
        os.remove(os.path.join(profile_dir, _SESSION_SENTINEL))
    except FileNotFoundError:
        pass
