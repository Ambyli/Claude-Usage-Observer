"""
usage_fetcher.py
----------------
Drives a persistent headless Chrome session to scrape token-usage data from
claude.ai/settings/usage.  Falls back to a visible window on first run so the
user can log in — cookies are then saved in CONSOLE_PROFILE_DIR and reused on
every subsequent start.

Call UsageFetcher.is_available() first; if selenium is not installed this
class still imports safely but start() becomes a no-op.

Public API
----------
UsageFetcher.is_available() -> bool
UsageFetcher()
    .start(on_update)   — begin background loop; on_update(state_dict) is
                          called whenever data or status changes
    .fetch_now()        — trigger an immediate re-fetch
    .get_state() -> dict
"""

import os
import threading
import time
from datetime import date, datetime, timedelta

from config import (
    CONSOLE_HEADLESS,
    CONSOLE_PROFILE_DIR,
    CONSOLE_REFRESH_MINUTES,
)
from logging_setup import log


class UsageFetcher:
    """
    Drives a persistent headless Chrome session to scrape token-usage data
    from claude.ai/settings/usage.
    """

    CONSOLE_URL   = "https://claude.ai"
    USAGE_URL     = "https://claude.ai/settings/usage"
    LOGIN_TIMEOUT = 300  # seconds the user has to log in

    # JS injected before every new document load — captures fetch + XHR responses
    _INTERCEPTOR_JS = """
    window._capturedResponses = window._capturedResponses || [];
    if (!window._fetchInterceptorActive) {
        window._fetchInterceptorActive = true;
        const _origFetch = window.fetch.bind(window);
        window.fetch = async function(input, init) {
            const url = (typeof input === 'string') ? input : (input.url || '');
            let response;
            try { response = await _origFetch(input, init); }
            catch(e) { throw e; }
            const ct = response.headers.get('content-type') || '';
            if (ct.includes('json')) {
                try {
                    const clone = response.clone();
                    const json  = await clone.json();
                    window._capturedResponses.push({url: url, body: json});
                } catch(_) {}
            }
            return response;
        };
        const _origOpen = XMLHttpRequest.prototype.open;
        const _origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(m, url, ...a) {
            this._xurl = url; return _origOpen.call(this, m, url, ...a);
        };
        XMLHttpRequest.prototype.send = function(...a) {
            this.addEventListener('load', function() {
                try {
                    const json = JSON.parse(this.responseText);
                    window._capturedResponses.push({url: this._xurl || '', body: json});
                } catch(_) {}
            });
            return _origSend.call(this, ...a);
        };
    }
    """

    def __init__(self):
        log.debug("Starting UsageFetcher.__init__")
        self._driver              = None
        self._data: dict | None   = None
        self._error: str | None   = None
        self._status              = "loading"   # loading | waiting_login | ok | error
        self._fetched_at: datetime | None = None
        self._lock                = threading.Lock()
        self._on_update           = None        # callable(state_dict)
        log.debug("Finished UsageFetcher.__init__")

    # ── Public ────────────────────────────────────────────────────────────────

    @staticmethod
    def is_available() -> bool:
        """True if selenium is installed (chromedriver need not be pre-installed;
        Selenium 4.6+ will auto-download it via selenium-manager)."""
        log.debug("Starting UsageFetcher.is_available")
        try:
            import selenium  # noqa: F401
            log.debug("Finished UsageFetcher.is_available: True")
            return True
        except ImportError as exc:
            log.debug("Selenium not available: %s", exc)
            log.debug("Finished UsageFetcher.is_available: False")
            return False

    def start(self, on_update):
        """Start the background fetch loop.  on_update(state_dict) is called
        from the worker thread whenever data or status changes."""
        log.debug("Starting UsageFetcher.start")
        self._on_update = on_update
        threading.Thread(target=self._loop, daemon=True).start()
        log.debug("Finished UsageFetcher.start")

    def fetch_now(self):
        """Trigger an immediate re-fetch without waiting for the interval."""
        log.debug("Starting UsageFetcher.fetch_now")
        threading.Thread(target=self._fetch, daemon=True).start()
        log.debug("Finished UsageFetcher.fetch_now")

    def get_state(self) -> dict:
        log.debug("Starting UsageFetcher.get_state")
        with self._lock:
            state = {
                "status":     self._status,
                "data":       self._data,
                "error":      self._error,
                "fetched_at": self._fetched_at,
            }
        log.debug("Finished UsageFetcher.get_state status=%s", state["status"])
        return state

    # ── Background loop ───────────────────────────────────────────────────────

    def _loop(self):
        log.debug("Starting UsageFetcher._loop")
        while True:
            self._fetch()
            time.sleep(CONSOLE_REFRESH_MINUTES * 60)

    def _fetch(self):
        log.debug("Starting UsageFetcher._fetch")
        try:
            self._set_status("loading")
            self._notify()

            driver = self._get_driver(headless=CONSOLE_HEADLESS)

            driver.get(self.CONSOLE_URL)
            time.sleep(2)

            if self._needs_login(driver):
                if CONSOLE_HEADLESS:
                    self._quit_driver()
                    driver = self._get_driver(headless=False)
                    driver.get(self.CONSOLE_URL)
                    time.sleep(4)

                self._set_status("waiting_login")
                self._notify()

                if not self._wait_for_login(driver):
                    raise TimeoutError("Login timed out — reopen the widget to retry")

                if CONSOLE_HEADLESS:
                    self._quit_driver()
                    driver = self._get_driver(headless=True)

            data = self._capture_usage(driver)
            if data is None:
                raise RuntimeError(
                    "Could not extract usage data — the console page layout "
                    "may have changed, or no data is available for this account"
                )

            with self._lock:
                self._data       = data
                self._error      = None
                self._status     = "ok"
                self._fetched_at = datetime.now()

        except Exception as exc:
            log.error("Error in UsageFetcher._fetch: %s", exc)
            with self._lock:
                self._error  = str(exc)
                self._status = "error"
        finally:
            log.debug("Entering finally block in UsageFetcher._fetch")
            self._notify()
        log.debug("Finished UsageFetcher._fetch")

    # ── Driver management ─────────────────────────────────────────────────────

    def _get_driver(self, headless: bool = True):
        """Return the live driver or create a fresh one."""
        log.debug("Starting UsageFetcher._get_driver headless=%s", headless)
        if self._driver is not None:
            try:
                _ = self._driver.title
                log.debug("Finished UsageFetcher._get_driver (reusing existing driver)")
                return self._driver
            except Exception as exc:
                log.warning("Existing driver is dead (%s), creating new one", exc)
                self._driver = None

        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        os.makedirs(CONSOLE_PROFILE_DIR, exist_ok=True)

        for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = os.path.join(CONSOLE_PROFILE_DIR, lock_file)
            try:
                os.remove(lock_path)
            except FileNotFoundError:
                pass

        opts = Options()
        opts.add_argument(f"--user-data-dir={CONSOLE_PROFILE_DIR}")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--log-level=3")
        opts.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})
        if headless:
            opts.add_argument("--headless=new")

        self._driver = webdriver.Chrome(options=opts)
        self._driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": self._INTERCEPTOR_JS},
        )
        log.debug("Finished UsageFetcher._get_driver (new driver created)")
        return self._driver

    def _quit_driver(self):
        log.debug("Starting UsageFetcher._quit_driver")
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception as exc:
                log.warning("Error quitting driver: %s", exc)
            self._driver = None
        log.debug("Finished UsageFetcher._quit_driver")

    # ── Login helpers ─────────────────────────────────────────────────────────

    def _needs_login(self, driver) -> bool:
        log.debug("Starting UsageFetcher._needs_login")
        url    = driver.current_url.lower()
        result = any(kw in url for kw in ("login", "signin", "accounts.google", "/auth", "claude.ai/login"))
        log.debug("Finished UsageFetcher._needs_login: %s", result)
        return result

    def _wait_for_login(self, driver) -> bool:
        """Block until the user finishes OAuth or LOGIN_TIMEOUT elapses."""
        log.debug("Starting UsageFetcher._wait_for_login timeout=%ds", self.LOGIN_TIMEOUT)
        deadline = time.time() + self.LOGIN_TIMEOUT
        while time.time() < deadline:
            if not self._needs_login(driver):
                time.sleep(2)
                log.debug("Finished UsageFetcher._wait_for_login: True")
                return True
            time.sleep(2)
        log.warning("UsageFetcher._wait_for_login timed out after %ds", self.LOGIN_TIMEOUT)
        log.debug("Finished UsageFetcher._wait_for_login: False")
        return False

    # ── Usage capture ─────────────────────────────────────────────────────────

    def _capture_usage(self, driver) -> dict | None:
        """Navigate to the usage page and retrieve intercepted XHR responses."""
        log.debug("Starting UsageFetcher._capture_usage")
        from selenium.webdriver.support.ui import WebDriverWait

        driver.get(self.USAGE_URL)
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(4)

        captured = driver.execute_script("return window._capturedResponses || []") or []

        url_keywords = ("usage", "billing", "cost", "token", "organization", "metric")
        for item in sorted(captured,
                           key=lambda i: any(kw in i.get("url","").lower() for kw in url_keywords),
                           reverse=True):
            body = item.get("body")
            if not isinstance(body, dict):
                continue
            parsed = self._parse_response(body)
            if parsed:
                log.debug("Finished UsageFetcher._capture_usage (found data)")
                return parsed

        log.warning("UsageFetcher._capture_usage found no parseable usage data")
        log.debug("Finished UsageFetcher._capture_usage: None")
        return None

    # ── Response parser ───────────────────────────────────────────────────────

    def _parse_response(self, body: dict) -> dict | None:
        """Normalise a captured API response into our display format.
        Handles the documented Admin API shape as well as reasonable variants.
        Also computes daily_total and weekly_total from bucketed timestamps."""
        log.debug("Starting UsageFetcher._parse_response")
        if not isinstance(body, dict):
            log.debug("Finished UsageFetcher._parse_response: None (not a dict)")
            return None

        today      = date.today()
        week_start = today - timedelta(days=today.weekday())

        # ── Format A: {results|data|buckets: [{token fields, ...}]} ──
        for key in ("results", "data", "buckets", "items"):
            results = body.get(key)
            if results and isinstance(results, list):
                totals        = {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0}
                daily_total   = 0
                weekly_total  = 0
                period_start  = period_end = None
                found         = False

                for bucket in results:
                    if not isinstance(bucket, dict):
                        continue
                    inp = (bucket.get("uncached_input_tokens")
                           or bucket.get("input_tokens") or 0)
                    cc  = bucket.get("cache_creation_input_tokens") or 0
                    cr  = bucket.get("cache_read_input_tokens") or 0
                    out = bucket.get("output_tokens") or 0
                    tok = inp + cc + cr + out
                    if tok > 0:
                        found = True
                    totals["input"]        += inp
                    totals["cache_create"] += cc
                    totals["cache_read"]   += cr
                    totals["output"]       += out

                    bucket_date = None
                    for tk_key in ("start_time", "period_start", "from", "start", "timestamp"):
                        ts_str = bucket.get(tk_key)
                        if ts_str:
                            try:
                                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                                bucket_date = ts.date()
                                break
                            except Exception:
                                pass
                    if bucket_date == today:
                        daily_total  += tok
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
                    log.debug("Finished UsageFetcher._parse_response (Format A, key=%r)", key)
                    return {**totals,
                            "total":        sum(totals.values()),
                            "daily_total":  daily_total,
                            "weekly_total": weekly_total,
                            "period_start": period_start,
                            "period_end":   period_end}

        # ── Format B: token fields directly on the object ──
        inp = body.get("input_tokens") or body.get("uncached_input_tokens") or 0
        out = body.get("output_tokens") or 0
        if inp + out > 0:
            cc = body.get("cache_creation_input_tokens") or 0
            cr = body.get("cache_read_input_tokens") or 0
            log.debug("Finished UsageFetcher._parse_response (Format B)")
            return {"input": inp, "cache_create": cc, "cache_read": cr, "output": out,
                    "total": inp + cc + cr + out,
                    "daily_total": 0, "weekly_total": 0,
                    "period_start": None, "period_end": None}

        log.debug("Finished UsageFetcher._parse_response: None (no matching format)")
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, status: str):
        log.debug("Starting UsageFetcher._set_status status=%s", status)
        with self._lock:
            self._status = status
        log.debug("Finished UsageFetcher._set_status")

    def _notify(self):
        log.debug("Starting UsageFetcher._notify")
        if self._on_update:
            try:
                self._on_update(self.get_state())
            except Exception as exc:
                log.error("Error in UsageFetcher._notify callback: %s", exc)
        log.debug("Finished UsageFetcher._notify")
