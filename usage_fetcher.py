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
    CAPTURE_TIMEOUT = 30  # seconds to poll for usage data after page load
    CAPTURE_POLL = 2  # seconds between each poll attempt

    _CHROME_PATHS = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]

    # Loaded from interceptor.js at class definition time so all instances share
    # the same string without re-reading the file on every inject call.
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
        self._proc: subprocess.Popen | None = None
        self._data: dict | None = None
        self._error: str | None = None
        self._status = "unlinked"
        self._fetched_at: datetime | None = None
        self._lock = threading.Lock()
        self._on_update = None
        self._ws = None  # persistent CDP WebSocket
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
        """Signal the live CDP session to reload the page."""
        log.debug("Starting BrowserLinker.fetch_now")
        self._reload_requested.set()
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
            self._set_status("loading")
            self._notify()
            try:
                self._cdp_session()  # blocks until the WebSocket dies
            except Exception as exc:
                log.error("Error in BrowserLinker._loop: %s", exc)
                with self._lock:
                    self._error = str(exc)
                    self._status = "error"
                self._notify()
            log.debug("BrowserLinker._loop: session ended, reconnecting in 15s")
            time.sleep(15)

    # ── CDP communication ─────────────────────────────────────────────────────

    def _cdp_session(self):
        """Persistent CDP session: initial capture then live binding-event loop.
        Blocks until the WebSocket connection dies, then returns so _loop can
        reconnect."""
        import websocket as _ws_mod
        import requests as _req

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

        ws = _ws_mod.create_connection(tab["webSocketDebuggerUrl"], timeout=15)
        self._ws = ws
        _id = [0]

        def rpc(method, params=None, _timeout=10):
            _id[0] += 1
            my_id = _id[0]
            ws.send(
                _json.dumps({"id": my_id, "method": method, "params": params or {}})
            )
            # Drain CDP messages until we get the response matching our request id.
            # Use a time-based deadline so a flood of Page/Runtime events never
            # causes us to miss our own response (unlike a fixed 100-message cap).
            ws.settimeout(1)
            deadline = time.time() + _timeout
            try:
                while time.time() < deadline:
                    try:
                        msg = _json.loads(ws.recv())
                    except _ws_mod.WebSocketTimeoutException:
                        continue
                    if msg.get("id") == my_id:
                        return msg.get("result", {})
            finally:
                ws.settimeout(None)  # restore blocking mode; live loop sets its own timeout
            return {}

        def eval_str(expr: str) -> str:
            result = rpc(
                "Runtime.evaluate", {"expression": expr, "returnByValue": True}
            )
            return result.get("result", {}).get("value", "") or ""

        url_keywords = ("usage", "billing", "cost", "token", "organization", "metric")

        def _find_usage(captured: list) -> dict | None:
            """Return parsed usage from a captured-responses list, or None."""
            for item in sorted(
                captured,
                key=lambda i: any(
                    kw in i.get("url", "").lower() for kw in url_keywords
                ),
                reverse=True,
            ):
                body = item.get("body")
                if isinstance(body, dict):
                    parsed = self._parse_response(body)
                    if parsed:
                        return parsed
            return None

        def _navigate_and_capture(target_url: str) -> dict:
            """Pre-register the interceptor, navigate/reload, then poll until
            usage data appears in _capturedResponses or CAPTURE_TIMEOUT expires."""

            # Register the interceptor to run before any page script on the
            # next navigation so we never miss early API calls.
            rpc(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": self._interceptor_script},
            )
            log.debug(
                "BrowserLinker._cdp_session: interceptor pre-registered for next document"
            )

            href = eval_str("location.href")
            if "settings/usage" not in href:
                log.debug("BrowserLinker._cdp_session: navigating to usage page")
                rpc("Page.navigate", {"url": target_url})
            else:
                log.debug("BrowserLinker._cdp_session: already on usage page, reloading")
                rpc("Page.reload", {})

            # Also inject immediately into whatever is currently loaded so any
            # already-open page gets the interceptor without waiting for a reload.
            rpc("Runtime.evaluate", {"expression": self._interceptor_script})

            # Poll until usage data appears or we time out.
            deadline = time.time() + self.CAPTURE_TIMEOUT
            attempt = 0
            while time.time() < deadline:
                time.sleep(self.CAPTURE_POLL)
                attempt += 1
                raw = eval_str("JSON.stringify(window._capturedResponses || [])")
                captured = _json.loads(raw) if raw else []
                log.debug(
                    "BrowserLinker._cdp_session: poll #%d — %d response(s) captured",
                    attempt,
                    len(captured),
                )
                result = _find_usage(captured)
                if result:
                    log.debug(
                        "BrowserLinker._cdp_session: usage data found on poll #%d",
                        attempt,
                    )
                    return result

            raise RuntimeError(
                f"No usage data found after {self.CAPTURE_TIMEOUT}s — the page may "
                "have changed or no data is available for this account"
            )

        try:
            si_kws = ("login", "signin", "/auth", "claude.ai/login")

            href = eval_str("location.href")
            if any(kw in href for kw in si_kws):
                self._set_status("waiting_login")
                self._notify()
                log.debug("BrowserLinker._cdp_session: waiting for user to log in")
                deadline = time.time() + self.LOGIN_TIMEOUT
                while time.time() < deadline:
                    time.sleep(3)
                    href = eval_str("location.href")
                    if not any(kw in href for kw in si_kws):
                        break
                else:
                    raise TimeoutError(
                        "Login timed out (5 min) — click Link Browser to retry"
                    )

            # Enable the Page domain so addScriptToEvaluateOnNewDocument is
            # honoured by Chrome and Page.loadEventFired fires in the live loop.
            rpc("Page.enable")

            # Register the binding BEFORE navigating so window.__cdpNotify
            # exists when the interceptor runs on the first page load.
            # Runtime.bindingCalled events that arrive during the rpc() drain
            # loop inside _navigate_and_capture are silently discarded (rpc
            # only returns on a matching id), so this doesn't break polling.
            # Runtime.enable is intentionally NOT called — it floods the
            # WebSocket with execution-context events that break rpc().
            rpc("Runtime.addBinding", {"name": "__cdpNotify"})

            # Initial navigate + polling capture.
            data = _navigate_and_capture(self.USAGE_URL)

            with self._lock:
                self._data = data
                self._error = None
                self._status = "ok"
                self._fetched_at = datetime.now()
            self._notify()

            # ── Persistent live-update loop ───────────────────────────────────
            # The interceptor calls window.__cdpNotify() on every captured
            # response, which Chrome delivers as a Runtime.bindingCalled event.
            # We also watch for fetch_now() reload requests and Page.loadEventFired
            # so we can re-inject the interceptor after a full navigation.
            log.debug("BrowserLinker._cdp_session: entering live-event loop")

            def _send(method, params=None):
                """Fire-and-forget CDP command — no response waiting, safe inside the event loop."""
                _id[0] += 1
                ws.send(_json.dumps({"id": _id[0], "method": method, "params": params or {}}))

            ws.settimeout(5)
            while True:
                # Check if a reload was requested via fetch_now()
                if self._reload_requested.is_set():
                    self._reload_requested.clear()
                    log.debug("BrowserLinker._cdp_session: reload requested — navigating")
                    # Pre-register the interceptor for the next document, then navigate.
                    # Both are fire-and-forget so the recv() timeout never applies here.
                    _send("Page.addScriptToEvaluateOnNewDocument",
                          {"source": self._interceptor_script})
                    _send("Page.navigate", {"url": self.USAGE_URL})

                try:
                    msg = _json.loads(ws.recv())
                except _ws_mod.WebSocketTimeoutException:
                    # Keep-alive: connection is idle but still open.
                    continue

                method = msg.get("method", "")

                # Re-register the binding and re-inject the interceptor after
                # each full page load.  Runtime.addBinding may not survive a
                # navigation (new execution context destroys the old binding),
                # and addScriptToEvaluateOnNewDocument already handles early
                # API calls; this eval is a belt-and-suspenders fallback.
                if method == "Page.loadEventFired":
                    log.debug(
                        "BrowserLinker._cdp_session: page loaded — re-registering binding and interceptor"
                    )
                    _send("Runtime.addBinding", {"name": "__cdpNotify"})
                    _send("Runtime.evaluate", {"expression": self._interceptor_script})
                    continue

                if (
                    method == "Runtime.bindingCalled"
                    and msg.get("params", {}).get("name") == "__cdpNotify"
                ):
                    try:
                        payload = _json.loads(msg["params"].get("payload", "{}"))
                        parsed = self._parse_response(payload.get("body", {}))
                        if parsed:
                            log.debug(
                                "BrowserLinker._cdp_session: live update received"
                            )
                            with self._lock:
                                self._data = parsed
                                self._error = None
                                self._status = "ok"
                                self._fetched_at = datetime.now()
                            self._notify()
                    except Exception as exc:
                        log.warning(
                            "BrowserLinker._cdp_session: error processing binding event: %s",
                            exc,
                        )

        finally:
            self._ws = None
            try:
                ws.close()
            except Exception:
                pass

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
