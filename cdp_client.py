"""
cdp_client.py
-------------
Chrome DevTools Protocol (CDP) session management.  Opens a persistent
WebSocket connection to an already-running Chrome debug endpoint, injects
the interceptor script, and delivers parsed usage data via a callback.

Public API
----------
run_cdp_session(
    debug_port,
    interceptor_script,
    parse_fn,
    on_data,
    on_status,
    reload_event,
    login_timeout,
    capture_timeout,
    capture_poll,
    usage_url,
) -> None   (blocks until the WebSocket dies, then returns)
"""

import json as _json
import time

from logging_setup import log

_LOGIN_KEYWORDS = ("login", "signin", "/auth", "claude.ai/login")
_URL_KEYWORDS = ("usage", "billing", "cost", "token", "organization", "metric")


def run_cdp_session(
    debug_port: int,
    interceptor_script: str,
    parse_fn,
    on_data,
    on_status,
    reload_event,
    login_timeout: int,
    capture_timeout: int,
    capture_poll: int,
    usage_url: str,
):
    """Persistent CDP session: initial capture then live binding-event loop.
    Blocks until the WebSocket connection dies, then returns so the caller
    can reconnect.

    Callbacks
    ---------
    parse_fn(body: dict) -> dict | None
        Parse a raw response body; return None if not a usage response.
    on_data(parsed: dict)
        Called whenever new usage data is successfully parsed.
    on_status(status: str, error: str | None)
        Called to report status changes ("waiting_login", "error", …).
    reload_event : threading.Event
        Caller sets this to request a page reload.
    """
    import websocket as _ws_mod
    import requests as _req

    # ── Connect to Chrome's debug endpoint ────────────────────────────────────
    tabs = None
    for attempt in range(15):
        try:
            tabs = _req.get(
                f"http://localhost:{debug_port}/json", timeout=3
            ).json()
            break
        except Exception:
            if attempt == 14:
                raise RuntimeError(
                    "Cannot connect to Chrome — make sure the window is still open"
                )
            time.sleep(2)

    tab = next(
        (t for t in tabs if t.get("type") == "page" and "claude.ai" in t.get("url", "")),
        None,
    )
    if tab is None:
        raise RuntimeError("No claude.ai tab found — keep the Chrome window open")

    ws = _ws_mod.create_connection(tab["webSocketDebuggerUrl"], timeout=15)
    _id = [0]

    # ── Low-level RPC ─────────────────────────────────────────────────────────

    def rpc(method, params=None, _timeout=10):
        _id[0] += 1
        my_id = _id[0]
        ws.send(_json.dumps({"id": my_id, "method": method, "params": params or {}}))
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
            ws.settimeout(None)
        return {}

    def eval_str(expr: str) -> str:
        result = rpc("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return result.get("result", {}).get("value", "") or ""

    # ── Usage-finding helpers ─────────────────────────────────────────────────

    def _find_usage(captured: list) -> dict | None:
        for item in sorted(
            captured,
            key=lambda i: any(kw in i.get("url", "").lower() for kw in _URL_KEYWORDS),
            reverse=True,
        ):
            body = item.get("body")
            if isinstance(body, dict):
                parsed = parse_fn(body)
                if parsed:
                    return parsed
        return None

    def _navigate_and_capture(target_url: str) -> dict:
        """Pre-register the interceptor, navigate/reload, then poll until
        usage data appears in _capturedResponses or capture_timeout expires."""
        # Register the interceptor to run before any page script on the
        # next navigation so we never miss early API calls.
        rpc(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": interceptor_script},
        )
        log.debug("cdp_client: interceptor pre-registered for next document")

        href = eval_str("location.href")
        if "settings/usage" not in href:
            log.debug("cdp_client: navigating to usage page")
            rpc("Page.navigate", {"url": target_url})
        else:
            log.debug("cdp_client: already on usage page, reloading")
            rpc("Page.reload", {})

        # Also inject immediately into whatever is currently loaded so any
        # already-open page gets the interceptor without waiting for a reload.
        rpc("Runtime.evaluate", {"expression": interceptor_script})

        # Poll until usage data appears or we time out.
        deadline = time.time() + capture_timeout
        attempt = 0
        while time.time() < deadline:
            time.sleep(capture_poll)
            attempt += 1
            raw = eval_str("JSON.stringify(window._capturedResponses || [])")
            captured = _json.loads(raw) if raw else []
            log.debug("cdp_client: poll #%d — %d response(s) captured", attempt, len(captured))
            result = _find_usage(captured)
            if result:
                log.debug("cdp_client: usage data found on poll #%d", attempt)
                return result

        raise RuntimeError(
            f"No usage data found after {capture_timeout}s — the page may "
            "have changed or no data is available for this account"
        )

    # ── Main session body ─────────────────────────────────────────────────────

    try:
        href = eval_str("location.href")
        if any(kw in href for kw in _LOGIN_KEYWORDS):
            on_status("waiting_login", None)
            log.debug("cdp_client: waiting for user to log in")
            deadline = time.time() + login_timeout
            while time.time() < deadline:
                time.sleep(3)
                href = eval_str("location.href")
                if not any(kw in href for kw in _LOGIN_KEYWORDS):
                    break
            else:
                raise TimeoutError("Login timed out (5 min) — click Link Browser to retry")

        # Enable the Page domain so addScriptToEvaluateOnNewDocument is
        # honoured by Chrome and Page.loadEventFired fires in the live loop.
        rpc("Page.enable")
        # Runtime.enable is required for Runtime.addBinding to actually
        # expose the function on window.__cdpNotify.  Without it the binding
        # is registered in Chrome but never injected into the page's JS
        # context, so the typeof check in interceptor.js always fails.
        # Runtime.enable also causes Chrome to flood the socket with
        # executionContext* events; the live loop already tolerates unknown
        # messages so this is safe — only the rpc() drain loop is affected,
        # and that runs before the live loop starts.
        rpc("Runtime.enable")
        # Register the binding BEFORE navigating so window.__cdpNotify
        # exists when the interceptor runs on the first page load.
        rpc("Runtime.addBinding", {"name": "__cdpNotify"})
        # Hide navigator.webdriver so the site doesn't detect headless/automation.
        rpc(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
            },
        )

        data = _navigate_and_capture(usage_url)
        on_data(data)

        # ── Persistent live-update loop ───────────────────────────────────────
        # Two complementary paths deliver live updates:
        #   1. Runtime.bindingCalled — fast path when window.__cdpNotify is
        #      available (Chrome doesn't guarantee the binding is injected
        #      before addScriptToEvaluateOnNewDocument scripts run, so this
        #      may silently miss early-load API calls).
        #   2. _capturedResponses polling — reliable fallback; on every
        #      5-second keep-alive tick we read the full array and process
        #      any items beyond the last index we already handled.
        log.debug("cdp_client: entering live-event loop")

        def _send(method, params=None):
            """Fire-and-forget CDP command — no response waiting, safe inside the event loop."""
            _id[0] += 1
            ws.send(_json.dumps({"id": _id[0], "method": method, "params": params or {}}))

        def _poll_captured():
            """Read window._capturedResponses via eval and process any new items."""
            nonlocal _last_captured_idx
            try:
                raw = eval_str("JSON.stringify(window._capturedResponses || [])")
                captured = _json.loads(raw) if raw else []
            except Exception:
                return
            for item in captured[_last_captured_idx:]:
                parsed = parse_fn(item.get("body", {}))
                if parsed:
                    log.debug("cdp_client: live update via poll (idx=%d)", _last_captured_idx)
                    on_data(parsed)
            _last_captured_idx = len(captured)

        _last_captured_idx = 0
        try:
            raw = eval_str("JSON.stringify(window._capturedResponses || [])")
            _last_captured_idx = len(_json.loads(raw)) if raw else 0
        except Exception:
            pass

        ws.settimeout(5)
        while True:
            if reload_event.is_set():
                reload_event.clear()
                _last_captured_idx = 0
                log.debug("cdp_client: reload requested — navigating")
                _send("Page.addScriptToEvaluateOnNewDocument", {"source": interceptor_script})
                _send("Page.navigate", {"url": usage_url})

            try:
                msg = _json.loads(ws.recv())
            except _ws_mod.WebSocketTimeoutException:
                # Keep-alive tick — poll _capturedResponses for anything the
                # binding may have missed (e.g. early-load API calls).
                _poll_captured()
                continue

            method = msg.get("method", "")

            # Re-register the binding and re-inject the interceptor after
            # each full page load.  Reset the poll index so we re-scan from
            # the start of the fresh document's captures.
            if method == "Page.loadEventFired":
                log.debug("cdp_client: page loaded — re-registering binding and interceptor")
                _last_captured_idx = 0
                _send("Runtime.addBinding", {"name": "__cdpNotify"})
                _send("Runtime.evaluate", {"expression": interceptor_script})
                continue

            if (
                method == "Runtime.bindingCalled"
                and msg.get("params", {}).get("name") == "__cdpNotify"
            ):
                try:
                    payload = _json.loads(msg["params"].get("payload", "{}"))
                    parsed = parse_fn(payload.get("body", {}))
                    if parsed:
                        log.debug("cdp_client: live update via binding")
                        on_data(parsed)
                        # Advance poll index to match so the next tick
                        # doesn't re-process the same item.
                        try:
                            raw = eval_str("JSON.stringify(window._capturedResponses || [])")
                            _last_captured_idx = len(_json.loads(raw)) if raw else _last_captured_idx
                        except Exception:
                            pass
                except Exception as exc:
                    log.warning("cdp_client: error processing binding event: %s", exc)

    finally:
        try:
            ws.close()
        except Exception:
            pass
