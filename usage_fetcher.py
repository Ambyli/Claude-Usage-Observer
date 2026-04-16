"""
usage_fetcher.py
----------------
Uses Chrome's DevTools Protocol (CDP) to read token-usage data from
claude.ai/settings/usage.  No Selenium required — communicates directly with
a visible Chrome window that the user opens by clicking "Link Browser" in the
widget popup.

Public API
----------
BrowserLinker.is_available() -> bool
BrowserLinker()
    .launch(on_update)  — open Chrome, start polling; on_update(state_dict)
                          is called from the worker thread on every change
    .fetch_now()        — trigger an immediate re-fetch
    .quit()             — terminate the Chrome process
    .get_state() -> dict
"""

import os
import threading
import time
from datetime import datetime

from config import (
    BROWSER_DEBUG_PORT,
    BROWSER_PROFILE_DIR,
    DEBUG_LOGGING,
)
from logging_setup import log

from cdp_client import run_cdp_session
from chrome_launcher import (
    find_chrome,
    start_chrome,
    session_exists,
    mark_session_ok,
    clear_session,
)
from response_parser import parse_response


class BrowserLinker:
    """
    Opens a visible Chrome window at claude.ai/settings/usage and reads usage
    data via Chrome DevTools Protocol (CDP).

    Flow:
      1. launch() opens Chrome with --remote-debugging-port and navigates to
         the usage page.  The window is fully interactive — the user logs in
         normally if prompted.
      2. The background loop connects to the open tab via WebSocket CDP,
         injects a fetch/XHR interceptor, and reads the captured responses.
      3. The state dict and on_update callback are identical to the old
         Selenium-based UsageFetcher so the rest of the app is unchanged.
    """

    USAGE_URL = "https://claude.ai/settings/usage"
    LOGIN_TIMEOUT = 300  # seconds the user has to log in
    CAPTURE_TIMEOUT = 30  # seconds to poll for usage data after page load
    CAPTURE_POLL = 2  # seconds between each poll attempt

    # Loaded once at class definition time so all instances share the same string.
    _INTERCEPTOR_JS: str = open(
        os.path.join(os.path.dirname(__file__), "interceptor.js"), encoding="utf-8"
    ).read()

    @property
    def _interceptor_script(self) -> str:
        """Returns the interceptor JS prefixed with the DEBUG_LOGGING constant."""
        flag = "true" if DEBUG_LOGGING else "false"
        return f"const DEBUG_LOGGING = {flag};\n" + self._INTERCEPTOR_JS

    def __init__(self):
        log.debug("Starting BrowserLinker.__init__")
        self._proc = None
        self._chrome_path: str | None = None
        self._data: dict | None = None
        self._error: str | None = None
        self._status = "unlinked"
        self._fetched_at: datetime | None = None
        self._headless: bool = False
        self._lock = threading.Lock()
        self._on_update = None
        self._reload_requested = threading.Event()
        log.debug("Finished BrowserLinker.__init__")

    # ── Public ────────────────────────────────────────────────────────────────

    @staticmethod
    def is_available() -> bool:
        """True if requests and websocket-client are both installed."""
        log.debug("Starting BrowserLinker.is_available")
        try:
            import requests  # noqa: F401
            import websocket  # noqa: F401
            log.debug("Finished BrowserLinker.is_available: True")
            return True
        except ImportError as exc:
            log.debug("BrowserLinker not available: %s", exc)
            return False

    def launch(self, on_update):
        """Open Chrome at the usage URL and begin the polling loop.
        on_update(state_dict) is called from the worker thread on every change."""
        log.debug("Starting BrowserLinker.launch")
        self._on_update = on_update

        chrome = find_chrome()
        if chrome is None:
            log.error("BrowserLinker.launch: Chrome not found")
            with self._lock:
                self._status = "error"
                self._error = (
                    "Chrome not found — install Google Chrome to use account stats"
                )
            self._notify()
            return

        os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
        for lf in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.remove(os.path.join(BROWSER_PROFILE_DIR, lf))
            except FileNotFoundError:
                pass

        self._chrome_path = chrome
        self._headless = session_exists(BROWSER_PROFILE_DIR)
        self._proc = start_chrome(
            chrome,
            headless=self._headless,
            debug_port=BROWSER_DEBUG_PORT,
            profile_dir=BROWSER_PROFILE_DIR,
            target_url=self.USAGE_URL,
        )
        log.debug("BrowserLinker.launch: Chrome started (pid=%s)", self._proc.pid)

        self._set_status("loading")
        self._notify()
        threading.Thread(target=self._loop, daemon=True).start()
        log.debug("Finished BrowserLinker.launch")

    def fetch_now(self):
        """Signal the live CDP session to reload the page."""
        log.debug("Starting BrowserLinker.fetch_now")
        self._reload_requested.set()
        log.debug("Finished BrowserLinker.fetch_now")

    def go_headless(self):
        """Terminate the current Chrome process and relaunch it headlessly.
        Requires a prior successful fetch (sentinel must exist)."""
        log.debug("Starting BrowserLinker.go_headless")
        if not session_exists(BROWSER_PROFILE_DIR):
            log.warning("BrowserLinker.go_headless: no session sentinel — cannot go headless")
            return
        if not self._chrome_path:
            log.warning("BrowserLinker.go_headless: chrome path not stored")
            return
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception as exc:
                log.warning("BrowserLinker.go_headless: error terminating Chrome: %s", exc)
            self._proc = None
        time.sleep(1)  # let Chrome fully exit before reopening the profile
        self._headless = True
        self._proc = start_chrome(
            self._chrome_path,
            headless=True,
            debug_port=BROWSER_DEBUG_PORT,
            profile_dir=BROWSER_PROFILE_DIR,
            target_url=self.USAGE_URL,
        )
        log.debug("BrowserLinker.go_headless: headless Chrome started (pid=%s)", self._proc.pid)

    def quit(self):
        """Terminate the managed Chrome process if one was started."""
        log.debug("Starting BrowserLinker.quit")
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception as exc:
                log.warning("BrowserLinker.quit: error terminating Chrome: %s", exc)
            self._proc = None
        log.debug("Finished BrowserLinker.quit")

    def get_state(self) -> dict:
        log.debug("Starting BrowserLinker.get_state")
        with self._lock:
            state = {
                "status": self._status,
                "data": self._data,
                "error": self._error,
                "fetched_at": self._fetched_at,
                "headless": self._headless,
            }
        log.debug("Finished BrowserLinker.get_state status=%s", state["status"])
        return state

    # ── Background loop ───────────────────────────────────────────────────────

    def _loop(self):
        log.debug("Starting BrowserLinker._loop")
        time.sleep(4)  # give Chrome time to open the tab
        while True:
            self._set_status("loading")
            self._notify()
            try:
                run_cdp_session(
                    debug_port=BROWSER_DEBUG_PORT,
                    interceptor_script=self._interceptor_script,
                    parse_fn=parse_response,
                    on_data=self._on_data,
                    on_status=self._on_cdp_status,
                    reload_event=self._reload_requested,
                    login_timeout=self.LOGIN_TIMEOUT,
                    capture_timeout=self.CAPTURE_TIMEOUT,
                    capture_poll=self.CAPTURE_POLL,
                    usage_url=self.USAGE_URL,
                )
            except TimeoutError as exc:
                # Login timed out — if we were running headless the session
                # expired. Clear the sentinel and relaunch visibly so the user
                # can log in again.
                if session_exists(BROWSER_PROFILE_DIR) and self._chrome_path:
                    log.warning(
                        "BrowserLinker._loop: headless session expired, relaunching visibly"
                    )
                    clear_session(BROWSER_PROFILE_DIR)
                    self._headless = False
                    try:
                        self._proc.terminate()
                    except Exception:
                        pass
                    self._proc = start_chrome(
                        self._chrome_path,
                        headless=False,
                        debug_port=BROWSER_DEBUG_PORT,
                        profile_dir=BROWSER_PROFILE_DIR,
                        target_url=self.USAGE_URL,
                    )
                    with self._lock:
                        self._error = "Session expired — please log in again"
                        self._status = "waiting_login"
                else:
                    with self._lock:
                        self._error = str(exc)
                        self._status = "error"
                self._notify()
            except Exception as exc:
                log.error("Error in BrowserLinker._loop: %s", exc)
                with self._lock:
                    self._error = str(exc)
                    self._status = "error"
                self._notify()
            log.debug("BrowserLinker._loop: session ended, reconnecting in 15s")
            time.sleep(15)

    # ── CDP callbacks ─────────────────────────────────────────────────────────

    def _on_data(self, parsed: dict):
        """Called by run_cdp_session whenever fresh usage data is parsed."""
        with self._lock:
            self._data = parsed
            self._error = None
            self._status = "ok"
            self._fetched_at = datetime.now()
        mark_session_ok(BROWSER_PROFILE_DIR)
        self._notify()

    def _on_cdp_status(self, status: str, error: str | None):
        """Called by run_cdp_session to report status changes."""
        with self._lock:
            self._status = status
            if error is not None:
                self._error = error
        self._notify()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, status: str):
        with self._lock:
            self._status = status

    def _notify(self):
        if self._on_update:
            try:
                self._on_update(self.get_state())
            except Exception as exc:
                log.error("Error in BrowserLinker._notify callback: %s", exc)
