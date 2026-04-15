// interceptor.js
// Patches fetch + XHR so every JSON response is recorded in
// window._capturedResponses before the page script can read it.
//
// DEBUG_LOGGING is injected as a boolean constant by usage_fetcher.py
// immediately before this script is evaluated. When true, every captured
// request URL and its response body are printed to the browser console.

// Shared list where every captured JSON response is stored.
// The `|| []` guard preserves any responses already collected if this
// script is injected more than once (e.g. after a manual reload).
window._capturedResponses = window._capturedResponses || [];

const _log = (...args) => {
  if (DEBUG_LOGGING) console.log(...args);
};

_log("[interceptor] script injected — DEBUG_LOGGING is active");

// Only patch fetch/XHR once per page lifetime.  Without this guard, each
// injection would wrap the already-wrapped APIs again, producing nested
// interceptors that push duplicate entries into _capturedResponses.
if (!window._fetchInterceptorActive) {
  window._fetchInterceptorActive = true;
  _log("[interceptor] first injection — patching fetch and XHR");

  // ── Patch window.fetch ────────────────────────────────────────────────
  // Save a reference to the real fetch before we overwrite it.
  const _origFetch = window.fetch.bind(window);
  _log("[interceptor] fetch: original saved, installing wrapper");

  window.fetch = async function (input, init) {
    // Normalise the request target to a plain URL string.
    const url = typeof input === "string" ? input : input.url || "";
    _log("[interceptor] fetch: request outgoing →", url);

    // Let the real request go out exactly as the page intended.
    let response;
    try {
      response = await _origFetch(input, init);
    } catch (e) {
      _log("[interceptor] fetch: request failed →", url, e);
      throw e;
    }

    // Only bother with JSON responses — those are the API calls we
    // care about (usage data, billing info, org metrics, etc.).
    const ct = response.headers.get("content-type") || "";
    if (ct.includes("json")) {
      try {
        // Response bodies can only be consumed once, so we clone
        // before reading.  The original response is returned to the
        // page untouched so normal rendering is unaffected.
        const clone = response.clone();
        const json = await clone.json();
        window._capturedResponses.push({ url: url, body: json });
        _log("[interceptor] fetch captured:", url, json);
        // Notify the CDP fetcher if it's listening.  The fetcher may have navigated away and lost the original binding, so this is a best-effort attempt to keep it updated with new captures after a navigation.
        if (typeof window.__cdpNotify === "function") {
          _log("[interceptor] fetch notifying:", url, json);
          try {
            window.__cdpNotify(JSON.stringify({ url: url, body: json }));
          } catch (_) {}
        }
      } catch (_) {
        _log("[interceptor] fetch: failed to parse JSON body for", url);
      }
    } else {
      _log(
        "[interceptor] fetch: skipped non-JSON response for",
        url,
        "(content-type:",
        ct,
        ")",
      );
    }

    // Always return the original so the page behaves normally.
    return response;
  };

  _log("[interceptor] fetch: wrapper installed");

  // ── Patch XMLHttpRequest ──────────────────────────────────────────────
  // Some older or non-fetch paths still use XHR; we patch both to be safe.
  const _origOpen = XMLHttpRequest.prototype.open;
  const _origSend = XMLHttpRequest.prototype.send;
  _log("[interceptor] XHR: originals saved, installing wrappers");

  // open() is where the URL is set — stash it on the instance so send()
  // can reference it later when the response arrives.
  XMLHttpRequest.prototype.open = function (m, url, ...a) {
    _log("[interceptor] XHR open:", m, url);
    this._xurl = url;
    return _origOpen.call(this, m, url, ...a);
  };

  XMLHttpRequest.prototype.send = function (...a) {
    // Attach a load listener before the request fires so we catch the
    // response regardless of when it arrives.
    this.addEventListener("load", function () {
      try {
        const json = JSON.parse(this.responseText);
        window._capturedResponses.push({ url: this._xurl || "", body: json });
        _log("[interceptor] XHR captured:", this._xurl || "", json);
        // Notify the CDP fetcher if it's listening.  The fetcher may have navigated away and lost the original binding, so this is a best-effort attempt to keep it updated with new captures after a navigation.
        if (typeof window.__cdpNotify === "function") {
          _log("[interceptor] XHR notifying:", this._xurl || "", json);
          try {
            window.__cdpNotify(
              JSON.stringify({ url: this._xurl || "", body: json }),
            );
          } catch (_) {}
        }
      } catch (_) {
        _log(
          "[interceptor] XHR: skipped non-JSON response for",
          this._xurl || "",
        );
      }
    });
    _log("[interceptor] XHR send:", this._xurl || "");
    return _origSend.call(this, ...a);
  };

  _log("[interceptor] XHR: wrappers installed — interceptor fully active");
} else {
  _log(
    "[interceptor] already active (re-injection skipped), existing captures:",
    window._capturedResponses.length,
  );
}
