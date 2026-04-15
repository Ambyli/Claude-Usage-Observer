"""
usage_parser.py
---------------
Reads ~/.claude/projects/**/*.jsonl and aggregates token usage into daily
totals, a weekly summary, per-project breakdowns, and rolling averages that
serve as dynamic daily/weekly limits.

Public API
----------
get_usage_summary() -> dict
    Returns a dict with keys: today, week_start, user, daily, weekly,
    daily_limit, weekly_limit, last_exec, project_breakdown.
"""

import json as _json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from config import CLAUDE_DIR, INCLUDE_PATHS, EXCLUDE_WEEKDAYS
from logging_setup import log


# ── Internal helpers ──────────────────────────────────────────────────────────

def _build_daily_totals(since: date) -> tuple[dict, dict | None, dict]:
    """
    Scan ~/.claude/projects/**/*.jsonl once and return:
      - {date: (input, output)} for every day on or after *since*
      - the most recent completed assistant turn (or None)
      - {project_name: {input, output, total}} for today only
    """
    log.debug("Starting _build_daily_totals since=%s", since)

    totals: dict        = defaultdict(lambda: [0, 0])
    since_dt            = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
    today_date          = date.today()
    last_exec: dict | None = None
    project_today: dict = defaultdict(lambda: [0, 0])
    project_cwds: dict[str, str] = {}

    for project in os.scandir(CLAUDE_DIR):
        if not project.is_dir():
            continue
        for entry in os.scandir(project.path):
            if not entry.name.endswith(".jsonl"):
                continue
            try:
                with open(entry.path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj   = _json.loads(line)
                        usage = obj.get("message", {}).get("usage")
                        if not usage or not usage.get("output_tokens"):
                            continue
                        cwd = obj.get("cwd", "")
                        if INCLUDE_PATHS:
                            if not any(cwd.lower().startswith(p) for p in INCLUDE_PATHS):
                                continue
                        ts_str = obj.get("timestamp", "")
                        if not ts_str:
                            continue
                        ts      = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        in_tok  = (usage.get("input_tokens", 0)
                                   + usage.get("cache_creation_input_tokens", 0)
                                   + usage.get("cache_read_input_tokens", 0))
                        out_tok = usage.get("output_tokens", 0)
                        day     = ts.date()

                        if last_exec is None or ts > last_exec["ts"]:
                            last_exec = {
                                "ts":           ts,
                                "input":        usage.get("input_tokens", 0),
                                "cache_create": usage.get("cache_creation_input_tokens", 0),
                                "cache_read":   usage.get("cache_read_input_tokens", 0),
                                "output":       out_tok,
                            }

                        if day == today_date:
                            project_today[project.name][0] += in_tok
                            project_today[project.name][1] += out_tok
                            if project.name not in project_cwds and cwd:
                                project_cwds[project.name] = cwd

                        if ts < since_dt:
                            continue
                        totals[day][0] += in_tok
                        totals[day][1] += out_tok
            except Exception as exc:
                log.error("Error reading %s: %s", entry.path, exc)
                continue

    # Build human-readable project names from cwd or folder name
    proj_breakdown: dict[str, dict] = {}
    for folder, (inp, out) in project_today.items():
        cwd = project_cwds.get(folder, "")
        if cwd:
            name = os.path.basename(cwd.rstrip("/\\")) or folder
        else:
            parts = folder.split("--")
            name  = parts[-1].replace("-", " ").strip() or folder
        proj_breakdown[name] = {"input": inp, "output": out, "total": inp + out}

    log.debug("Finished _build_daily_totals: %d days, %d projects", len(totals), len(proj_breakdown))
    return {d: (v[0], v[1]) for d, v in totals.items()}, last_exec, proj_breakdown


def _day_total(totals: dict, d: date) -> int:
    v = totals.get(d, (0, 0))
    return v[0] + v[1]


def _week_total(totals: dict, week_end: date) -> int:
    """Sum tokens for the 7-day window ending on *week_end* (inclusive)."""
    return sum(_day_total(totals, week_end - timedelta(days=i)) for i in range(7))


# ── Public API ────────────────────────────────────────────────────────────────

def get_usage_summary() -> dict:
    """Return a full usage summary dict ready for the UI to consume."""
    log.debug("Starting get_usage_summary")
    today     = date.today()
    yesterday = today - timedelta(days=1)

    # Scan far enough back for 7 full prior calendar weeks (up to 56 days before last Monday)
    since = today - timedelta(days=63)
    totals, last_exec, project_breakdown = _build_daily_totals(since)

    # ── Today ──
    d_in, d_out = totals.get(today, (0, 0))

    # ── This calendar week (Monday → today) ──
    week_start = today - timedelta(days=today.weekday())  # Monday
    w_in  = sum(totals.get(week_start + timedelta(days=i), (0, 0))[0] for i in range(today.weekday() + 1))
    w_out = sum(totals.get(week_start + timedelta(days=i), (0, 0))[1] for i in range(today.weekday() + 1))

    # ── Rolling daily limit: average of previous 7 days, excluding configured weekdays ──
    prev_7_days    = [
        (yesterday - timedelta(days=i), _day_total(totals, yesterday - timedelta(days=i)))
        for i in range(7)
        if (yesterday - timedelta(days=i)).weekday() not in EXCLUDE_WEEKDAYS
    ]
    days_with_data = [t for _, t in prev_7_days if t > 0]
    daily_limit    = int(sum(days_with_data) / len(days_with_data)) if days_with_data else 0

    # ── Weekly limit: average of the 7 previous complete calendar weeks ──
    last_week_monday = week_start - timedelta(weeks=1)

    def _cal_week_total(mon: date) -> int:
        return sum(
            _day_total(totals, mon + timedelta(days=i))
            for i in range(7)
            if (mon + timedelta(days=i)).weekday() not in EXCLUDE_WEEKDAYS
        )

    prev_7_cal_weeks    = [_cal_week_total(last_week_monday - timedelta(weeks=i)) for i in range(7)]
    cal_weeks_with_data = [t for t in prev_7_cal_weeks if t > 0]
    weekly_limit        = int(sum(cal_weeks_with_data) / len(cal_weeks_with_data)) if cal_weeks_with_data else 0

    log.debug(
        "Finished get_usage_summary: daily=%d, weekly=%d, daily_limit=%d, weekly_limit=%d",
        d_in + d_out, w_in + w_out, daily_limit, weekly_limit,
    )
    return {
        "today":             today.strftime("%A, %b %d"),
        "week_start":        week_start.strftime("%b %d"),
        "user":              None,
        "daily":             {"input": d_in,  "output": d_out,  "total": d_in  + d_out},
        "weekly":            {"input": w_in,  "output": w_out,  "total": w_in  + w_out},
        "daily_limit":       daily_limit,
        "weekly_limit":      weekly_limit,
        "last_exec":         last_exec,
        "project_breakdown": project_breakdown,
    }
