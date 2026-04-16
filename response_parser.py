"""
response_parser.py
------------------
Parses raw API response bodies captured from claude.ai/settings/usage into
the normalised display dict consumed by the rest of the app.

Public API
----------
parse_response(body: dict) -> dict | None
"""

from datetime import date, datetime, timedelta

from logging_setup import log


def parse_response(body: dict) -> dict | None:
    """Normalise a captured API response into our display format.
    Handles the documented Admin API shape as well as reasonable variants.
    Also computes daily_total and weekly_total from bucketed timestamps."""
    log.debug("Starting parse_response")
    log.debug("parse_response body keys: %s | sample: %.300s", list(body.keys()) if isinstance(body, dict) else type(body), str(body))
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
                log.debug("Finished parse_response (Format A, key=%r)", key)
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
        log.debug("Finished parse_response (Format B)")
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

    # ── Format C: utilization-based response (five_hour / seven_day) ──
    # e.g. {"five_hour": {"utilization": 30.0, "resets_at": "..."}, "seven_day": {...}}
    five_hour = body.get("five_hour")
    seven_day = body.get("seven_day")
    if isinstance(five_hour, dict) or isinstance(seven_day, dict):
        def _util_block(block) -> dict | None:
            if not isinstance(block, dict):
                return None
            util = block.get("utilization")
            if util is None:
                return None
            return {
                "utilization": float(util),
                "resets_at": block.get("resets_at"),
            }

        fh = _util_block(five_hour)
        sd = _util_block(seven_day)
        if fh is not None or sd is not None:
            extra = body.get("extra_usage")
            log.debug("Finished parse_response (Format C)")
            return {
                "format": "utilization",
                "five_hour": fh,
                "seven_day": sd,
                "extra_usage": extra if isinstance(extra, dict) else None,
                # Legacy fields so any code that reads them doesn't crash.
                "daily_total": 0,
                "weekly_total": 0,
                "total": 0,
                "period_start": None,
                "period_end": (sd or fh or {}).get("resets_at"),
            }

    log.debug("Finished parse_response: None")
    return None
