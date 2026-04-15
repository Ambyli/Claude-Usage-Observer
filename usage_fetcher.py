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

import json as _json
import os
import subprocess
import threading
import time
from datetime import date, datetime, timedelta

from config import (
    BROWSER_DEBUG_PORT,
    BROWSER_PROFILE_DIR,
    CONSOLE_REFRESH_MINUTES,
    DEBUG_LOGGING,
)
from logging_setup import log


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
      3. The state dict and apply_console/on_update callback are identical to
         the old Selenium-based UsageFetcher so the rest of the app is unchanged.
    """

    USAGE_URL = "https://claude.ai/settings/usage"
    LOGIN_TIMEOUT = 300  # seconds the user has to log in

    _CHROME_PATHS = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]

    # Loaded from interceptor.js at class definition time so all instances share
    # the same string without re-reading the file on every inject call.
    _INTERCEPTOR_JS: str = (
        open(os.path.join(os.path.dirname(__file__), "interceptor.js"), encoding="utf-8")
        .read()
    )

    @property
    def _interceptor_script(self) -> str:
        """Returns the interceptor JS prefixed with the DEBUG_LOGGING constant."""
        flag = "true" if DEBUG_LOGGING else "false"
        return f"const DEBUG_LOGGING = {flag};\n" + self._INTERCEPTOR_JS

    def __init__(self):
        log.debug("Starting BrowserLinker.__init__")
        self._proc: subprocess.Popen | None = None
        self._data: dict | None = None
        self._error: str | None = None
        self._status = "unlinked"
        self._fetched_at: datetime | None = None
        self._lock = threading.Lock()
        self._on_update = None
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

        chrome = next((p for p in self._CHROME_PATHS if os.path.exists(p)), None)
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

        self._proc = subprocess.Popen(
            [
                chrome,
                f"--remote-debugging-port={BROWSER_DEBUG_PORT}",
                f"--remote-allow-origins=http://localhost:{BROWSER_DEBUG_PORT}",
                f"--user-data-dir={BROWSER_PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                self.USAGE_URL,
            ]
        )
        log.debug("BrowserLinker.launch: Chrome started (pid=%s)", self._proc.pid)

        self._set_status("loading")
        self._notify()
        threading.Thread(target=self._loop, daemon=True).start()
        log.debug("Finished BrowserLinker.launch")

    def fetch_now(self):
        """Trigger an immediate re-fetch without waiting for the interval."""
        log.debug("Starting BrowserLinker.fetch_now")
        threading.Thread(target=self._fetch, daemon=True).start()
        log.debug("Finished BrowserLinker.fetch_now")

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
            }
        log.debug("Finished BrowserLinker.get_state status=%s", state["status"])
        return state

    # ── Background loop ───────────────────────────────────────────────────────

    def _loop(self):
        log.debug("Starting BrowserLinker._loop")
        time.sleep(4)  # give Chrome time to open the tab
        while True:
            self._fetch()
            time.sleep(CONSOLE_REFRESH_MINUTES * 60)

    def _fetch(self):
        log.debug("Starting BrowserLinker._fetch")
        try:
            self._set_status("loading")
            self._notify()

            data = self._cdp_fetch()

            with self._lock:
                self._data = data
                self._error = None
                self._status = "ok"
                self._fetched_at = datetime.now()

        except Exception as exc:
            log.error("Error in BrowserLinker._fetch: %s", exc)
            with self._lock:
                self._error = str(exc)
                self._status = "error"
        finally:
            log.debug("Entering finally block in BrowserLinker._fetch")
            self._notify()
        log.debug("Finished BrowserLinker._fetch")

    # ── CDP communication ─────────────────────────────────────────────────────

    def _cdp_fetch(self) -> dict:
        """Connect to the open Chrome tab via CDP and return parsed usage data."""
        import requests as _req
        import websocket as _ws

        # Wait up to ~30 s for Chrome's debugging endpoint to respond
        tabs = None
        for attempt in range(15):
            try:
                tabs = _req.get(
                    f"http://localhost:{BROWSER_DEBUG_PORT}/json", timeout=3
                ).json()
                break
            except Exception:
                if attempt == 14:
                    raise RuntimeError(
                        "Cannot connect to Chrome — make sure the window is still open"
                    )
                time.sleep(2)

        tab = next(
            (
                t
                for t in tabs
                if t.get("type") == "page" and "claude.ai" in t.get("url", "")
            ),
            None,
        )
        if tab is None:
            raise RuntimeError("No claude.ai tab found — keep the Chrome window open")

        ws = _ws.create_connection(tab["webSocketDebuggerUrl"], timeout=15)
        _id = [0]

        def rpc(method, params=None):
            _id[0] += 1
            my_id = _id[0]
            ws.send(
                _json.dumps({"id": my_id, "method": method, "params": params or {}})
            )
            # Drain CDP events until we get the response matching our request id
            for _ in range(100):
                msg = _json.loads(ws.recv())
                if msg.get("id") == my_id:
                    return msg.get("result", {})
            return {}

        def eval_str(expr: str) -> str:
            result = rpc(
                "Runtime.evaluate", {"expression": expr, "returnByValue": True}
            )
            return result.get("result", {}).get("value", "") or ""

        try:
            # Inject the interceptor on whatever page is currently loaded
            rpc("Runtime.evaluate", {"expression": self._interceptor_script})

            href = eval_str("location.href")

            if "settings/usage" not in href:
                rpc("Page.navigate", {"url": self.USAGE_URL})
                time.sleep(5)
                rpc("Runtime.evaluate", {"expression": self._interceptor_script})
                time.sleep(4)
            else:
                # Already on the right page — reload to trigger a fresh XHR capture
                rpc("Page.reload", {})
                time.sleep(5)

            # Check whether we landed on a login page
            href = eval_str("location.href")
            if any(
                kw in href for kw in ("login", "signin", "/auth", "claude.ai/login")
            ):
                self._set_status("waiting_login")
                self._notify()
                log.debug("BrowserLinker._cdp_fetch: waiting for user to log in")
                deadline = time.time() + self.LOGIN_TIMEOUT
                while time.time() < deadline:
                    time.sleep(3)
                    href = eval_str("location.href")
                    if not any(kw in href for kw in ("login", "signin", "/auth")):
                        rpc("Page.navigate", {"url": self.USAGE_URL})
                        time.sleep(5)
                        rpc("Runtime.evaluate", {"expression": self._interceptor_script})
                        time.sleep(4)
                        break
                else:
                    raise TimeoutError(
                        "Login timed out (5 min) — click Link Browser to retry"
                    )

            # Collect everything the interceptor captured
            raw = eval_str("JSON.stringify(window._capturedResponses || [])")
            captured = _json.loads(raw) if raw else []
            log.warning(
                "BrowserLinker._cdp_fetch: captured %d responses, checking for usage data",
                len(captured),
            )

        finally:
            ws.close()

        # Try URL-hinted responses first (most likely to carry usage data)
        url_keywords = ("usage", "billing", "cost", "token", "organization", "metric")
        for item in sorted(
            captured,
            key=lambda i: any(kw in i.get("url", "").lower() for kw in url_keywords),
            reverse=True,
        ):
            body = item.get("body")
            if isinstance(body, dict):
                parsed = self._parse_response(body)
                if parsed:
                    log.debug("BrowserLinker._cdp_fetch: found usage data")
                    return parsed

        raise RuntimeError(
            "No usage data found — the page may have changed or no data is "
            "available for this account"
        )

    # ── Response parser ───────────────────────────────────────────────────────

    def _parse_response(self, body: dict) -> dict | None:
        """Normalise a captured API response into our display format.
        Handles the documented Admin API shape as well as reasonable variants.
        Also computes daily_total and weekly_total from bucketed timestamps."""
        log.debug("Starting BrowserLinker._parse_response")
        if not isinstance(body, dict):
            return None

        today = date.today()
        week_start = today - timedelta(days=today.weekday())

        # ── Format A: {results|data|buckets|items: [{token fields, ...}]} ──
        for key in ("results", "data", "buckets", "items"):
            results = body.get(key)
            if results and isinstance(results, list):
                totals = {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0}
                daily_total = 0
                weekly_total = 0
                period_start = period_end = None
                found = False

                for bucket in results:
                    if not isinstance(bucket, dict):
                        continue
                    inp = (
                        bucket.get("uncached_input_tokens")
                        or bucket.get("input_tokens")
                        or 0
                    )
                    cc = bucket.get("cache_creation_input_tokens") or 0
                    cr = bucket.get("cache_read_input_tokens") or 0
                    out = bucket.get("output_tokens") or 0
                    tok = inp + cc + cr + out
                    if tok > 0:
                        found = True
                    totals["input"] += inp
                    totals["cache_create"] += cc
                    totals["cache_read"] += cr
                    totals["output"] += out

                    bucket_date = None
                    for tk_key in (
                        "start_time",
                        "period_start",
                        "from",
                        "start",
                        "timestamp",
                    ):
                        ts_str = bucket.get(tk_key)
                        if ts_str:
                            try:
                                ts = datetime.fromisoformat(
                                    ts_str.replace("Z", "+00:00")
                                )
                                bucket_date = ts.date()
                                break
                            except Exception:
                                pass
                    if bucket_date == today:
                        daily_total += tok
                    if bucket_date and bucket_date >= week_start:
                        weekly_total += tok

                    for sk in ("start_time", "period_start", "from", "start"):
                        s = bucket.get(sk)
                        if s and (period_start is None or s < period_start):
                            period_start = s
                    for ek in ("end_time", "period_end", "to", "end"):
                        e = bucket.get(ek)
                        if e and (period_end is None or e > period_end):
                            period_end = e

                if found:
                    log.debug(
                        "Finished BrowserLinker._parse_response (Format A, key=%r)", key
                    )
                    return {
                        **totals,
                        "total": sum(totals.values()),
                        "daily_total": daily_total,
                        "weekly_total": weekly_total,
                        "period_start": period_start,
                        "period_end": period_end,
                    }

        # ── Format B: token fields directly on the object ──
        inp = body.get("input_tokens") or body.get("uncached_input_tokens") or 0
        out = body.get("output_tokens") or 0
        if inp + out > 0:
            cc = body.get("cache_creation_input_tokens") or 0
            cr = body.get("cache_read_input_tokens") or 0
            log.debug("Finished BrowserLinker._parse_response (Format B)")
            return {
                "input": inp,
                "cache_create": cc,
                "cache_read": cr,
                "output": out,
                "total": inp + cc + cr + out,
                "daily_total": 0,
                "weekly_total": 0,
                "period_start": None,
                "period_end": None,
            }

        log.debug("Finished BrowserLinker._parse_response: None")
        return None

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
