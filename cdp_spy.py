"""
cdp_spy.py
----------
Connects to a running Chrome instance (--remote-debugging-port=9222),
injects interceptor.js into the active tab, and prints every payload
that window.__cdpNotify receives.

window.__cdpNotify is defined as a plain JS function (via
addScriptToEvaluateOnNewDocument) that pushes payloads into a queue.
Python polls that queue every second — no Runtime.addBinding needed.

Usage
-----
1. Launch Chrome with remote debugging enabled (or let the widget do it):
       chrome.exe --remote-debugging-port=9222

2. In a separate terminal:
       python cdp_spy.py

Press Ctrl-C to stop.
"""

import json
import os
import time

import requests
import websocket

# ── Config ────────────────────────────────────────────────────────────────────

DEBUG_PORT = 9222
POLL_INTERVAL = 1  # seconds between queue drain polls

# Load interceptor.js from the same directory as this script.
_here = os.path.dirname(os.path.abspath(__file__))
_interceptor_src = open(os.path.join(_here, "interceptor.js"), encoding="utf-8").read()

# JS that defines window.__cdpNotify as a plain function which pushes each
# payload into window.__cdpNotifyQueue.  This runs before any page script via
# addScriptToEvaluateOnNewDocument so the interceptor always finds it ready.
_NOTIFY_SHIM = """
window.__cdpNotifyQueue = window.__cdpNotifyQueue || [];
window.__cdpNotify = function(payload) {
    window.__cdpNotifyQueue.push(payload);
};
"""

# Full script injected on every (re)load: shim first, then interceptor.
FULL_SCRIPT = _NOTIFY_SHIM + "\nconst DEBUG_LOGGING = true;\n" + _interceptor_src

# ── Helpers ───────────────────────────────────────────────────────────────────

_msg_id = 0

def _send(ws, method, params=None):
    global _msg_id
    _msg_id += 1
    ws.send(json.dumps({"id": _msg_id, "method": method, "params": params or {}}))
    return _msg_id


def _rpc(ws, method, params=None, timeout=10):
    """Send a CDP command and wait for its response."""
    my_id = _send(ws, method, params)
    ws.settimeout(1)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if msg.get("id") == my_id:
                return msg.get("result", {})
    finally:
        ws.settimeout(None)
    return {}


def _eval(ws, expr):
    """Evaluate a JS expression and return its string value."""
    result = _rpc(ws, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
    return result.get("result", {}).get("value")


def _drain_queue(ws):
    """Read and clear window.__cdpNotifyQueue, return list of raw payload strings."""
    raw = _eval(ws, "JSON.stringify(window.__cdpNotifyQueue || [])")
    if not raw:
        return []
    items = json.loads(raw)
    if items:
        _rpc(ws, "Runtime.evaluate", {"expression": "window.__cdpNotifyQueue = []"})
    return items

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # 1. Find an open tab
    print(f"Connecting to Chrome on port {DEBUG_PORT}...")
    for attempt in range(10):
        try:
            tabs = requests.get(f"http://localhost:{DEBUG_PORT}/json", timeout=3).json()
            break
        except Exception:
            if attempt == 9:
                raise RuntimeError(
                    "Cannot reach Chrome — make sure it was launched with "
                    f"--remote-debugging-port={DEBUG_PORT}"
                )
            time.sleep(2)

    pages = [t for t in tabs if t.get("type") == "page"]
    if not pages:
        raise RuntimeError("No page tabs found in Chrome.")

    tab = pages[0]
    print(f"Using tab: {tab.get('url')}")

    # 2. Open a WebSocket to that tab
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=15)

    # 3. Enable Page domain
    _rpc(ws, "Page.enable")

    # 4. Pre-register the full script (shim + interceptor) for every future load
    _rpc(ws, "Page.addScriptToEvaluateOnNewDocument", {"source": FULL_SCRIPT})

    # 5. Reload so the new document starts with __cdpNotify already defined
    print("Reloading page so __cdpNotify is available from document start...")
    _rpc(ws, "Page.reload", {}, timeout=15)

    # Wait for load event
    ws.settimeout(20)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            msg = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            break
        if msg.get("method") == "Page.loadEventFired":
            break
    ws.settimeout(None)

    # 6. Verify __cdpNotify is present
    binding_type = _eval(ws, "typeof window.__cdpNotify")
    if binding_type == "function":
        print("Verified: window.__cdpNotify is a function.")
    else:
        print(f"WARNING: window.__cdpNotify is '{binding_type}' — something went wrong.")

    print("Polling for notifications (Ctrl-C to stop)...\n")

    # 7. Poll the queue
    try:
        while True:
            time.sleep(POLL_INTERVAL)
            for raw_payload in _drain_queue(ws):
                try:
                    payload = json.loads(raw_payload)
                    url = payload.get("url", "(no url)")
                    body = payload.get("body", payload)
                    print(f"[__cdpNotify] URL: {url}")
                    print(json.dumps(body, indent=2))
                    print()
                except json.JSONDecodeError:
                    print(f"[__cdpNotify] raw: {raw_payload}\n")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        ws.close()


if __name__ == "__main__":
    main()
