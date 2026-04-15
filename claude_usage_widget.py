"""
Claude Usage Taskbar Widget
----------------------------
Displays daily and weekly Claude Code token usage in the system tray,
read directly from ~/.claude/projects/**/*.jsonl.

Requirements:
    pip install pystray Pillow

Usage limits
    Set DAILY_TOKEN_LIMIT and WEEKLY_TOKEN_LIMIT below to match your plan.
    The bars show how much of each limit you have consumed.
    Leave as 0 to hide the limit bars.
"""

import logging
import os
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Load .env from the same directory as this script (must happen before logging setup)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Logging setup ─────────────────────────────────────────────────────────────

_log_path = Path(__file__).parent / "claude_usage_widget.log"
_debug_logging = os.environ.get("DEBUG_LOGGING", "false").lower() == "true"
_file_level    = logging.DEBUG if _debug_logging else logging.INFO
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(funcName)s: %(message)s",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
_console_level = logging.DEBUG if _debug_logging else logging.WARNING
logging.getLogger().handlers[0].setLevel(_file_level)    # file: DEBUG or INFO
logging.getLogger().handlers[1].setLevel(_console_level) # console: DEBUG or WARNING
log = logging.getLogger("claude_usage_widget")

# ── Configuration ─────────────────────────────────────────────────────────────

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes

# Local Claude Code session data written by the Claude Code CLI/desktop app
CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")

# Path filter — only count sessions whose cwd starts with one of these (case-insensitive).
# Loaded from INCLUDE_PATHS in .env. Empty list = include everything.
_raw_paths = os.environ.get("INCLUDE_PATHS", "")
INCLUDE_PATHS: list[str] = [p.strip().lower() for p in _raw_paths.split(",") if p.strip()]

# Days excluded from limit averaging (0=Mon … 6=Sun). Defaults to Sat+Sun.
_raw_exclude = os.environ.get("EXCLUDE_WEEKDAYS", "5,6")
EXCLUDE_WEEKDAYS: set[int] = {int(d.strip()) for d in _raw_exclude.split(",") if d.strip()}

# Console stats via Selenium (optional).
# Set CONSOLE_FETCHER_ENABLED=true to enable; all other CONSOLE_* settings are
# ignored when it is disabled.  Set CONSOLE_HEADLESS=false to show the browser.
CONSOLE_FETCHER_ENABLED = os.environ.get("CONSOLE_FETCHER_ENABLED", "false").lower() == "true"
CONSOLE_REFRESH_MINUTES = int(os.environ.get("CONSOLE_REFRESH_MINUTES", "30"))
CONSOLE_HEADLESS        = os.environ.get("CONSOLE_HEADLESS", "true").lower() != "false"
CONSOLE_PROFILE_DIR     = os.path.join(os.path.expanduser("~"), ".claude_widget", "chrome_profile")

# ── Local JSONL parser ────────────────────────────────────────────────────────

def _build_daily_totals(since: date) -> tuple[dict, dict | None, dict]:
    """
    Scan ~/.claude/projects/**/*.jsonl once and return:
      - {date: (input, output)} for every day on or after `since`
      - the most recent completed assistant turn (or None)
      - {project_name: {input, output, total}} for today only
    """
    log.debug("Starting _build_daily_totals since=%s", since)
    import json as _json
    from collections import defaultdict

    totals: dict = defaultdict(lambda: [0, 0])
    since_dt = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
    today_date = date.today()
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
                        obj = _json.loads(line)
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
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        in_tok = (usage.get("input_tokens", 0)
                                  + usage.get("cache_creation_input_tokens", 0)
                                  + usage.get("cache_read_input_tokens", 0))
                        out_tok = usage.get("output_tokens", 0)
                        day = ts.date()

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
            name = parts[-1].replace("-", " ").strip() or folder
        proj_breakdown[name] = {"input": inp, "output": out, "total": inp + out}

    log.debug("Finished _build_daily_totals: %d days, %d projects", len(totals), len(proj_breakdown))
    return {d: (v[0], v[1]) for d, v in totals.items()}, last_exec, proj_breakdown


def _day_total(totals: dict, d: date) -> int:
    log.debug("Starting _day_total for date=%s", d)
    v = totals.get(d, (0, 0))
    result = v[0] + v[1]
    log.debug("Finished _day_total: %d", result)
    return result


def _week_total(totals: dict, week_end: date) -> int:
    """Sum tokens for the 7-day window ending on week_end (inclusive)."""
    log.debug("Starting _week_total week_end=%s", week_end)
    result = sum(_day_total(totals, week_end - timedelta(days=i)) for i in range(7))
    log.debug("Finished _week_total: %d", result)
    return result


# ── Summary ───────────────────────────────────────────────────────────────────

def get_usage_summary() -> dict:
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
    prev_7_days = [
        (yesterday - timedelta(days=i), _day_total(totals, yesterday - timedelta(days=i)))
        for i in range(7)
        if (yesterday - timedelta(days=i)).weekday() not in EXCLUDE_WEEKDAYS
    ]
    days_with_data = [t for _, t in prev_7_days if t > 0]
    daily_limit = int(sum(days_with_data) / len(days_with_data)) if days_with_data else 0

    # ── Weekly limit: average of the 7 previous complete calendar weeks ──
    # Each week total only sums days not in EXCLUDE_WEEKDAYS.
    last_week_monday = week_start - timedelta(weeks=1)

    def _cal_week_total(mon: date) -> int:
        return sum(
            _day_total(totals, mon + timedelta(days=i))
            for i in range(7)
            if (mon + timedelta(days=i)).weekday() not in EXCLUDE_WEEKDAYS
        )

    prev_7_cal_weeks = [_cal_week_total(last_week_monday - timedelta(weeks=i)) for i in range(7)]
    cal_weeks_with_data = [t for t in prev_7_cal_weeks if t > 0]
    weekly_limit = int(sum(cal_weeks_with_data) / len(cal_weeks_with_data)) if cal_weeks_with_data else 0

    log.debug(
        "Finished get_usage_summary: daily=%d, weekly=%d, daily_limit=%d, weekly_limit=%d",
        d_in + d_out, w_in + w_out, daily_limit, weekly_limit,
    )
    return {
        "today":         today.strftime("%A, %b %d"),
        "week_start":    week_start.strftime("%b %d"),
        "user":          None,
        "daily":         {"input": d_in,  "output": d_out,  "total": d_in  + d_out},
        "weekly":        {"input": w_in,  "output": w_out,  "total": w_in  + w_out},
        "daily_limit":        daily_limit,
        "weekly_limit":       weekly_limit,
        "last_exec":          last_exec,
        "project_breakdown":  project_breakdown,
    }

# ── Tray Icon Image ───────────────────────────────────────────────────────────

def make_tray_icon(status: str = "ok") -> Image.Image:
    """
    Generate a 64x64 icon.
    status: 'ok' (green dot), 'loading' (yellow), 'error' (red)
    """
    log.debug("Starting make_tray_icon status=%s", status)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background rounded square
    draw.rounded_rectangle([2, 2, 62, 62], radius=12, fill=(30, 30, 35))

    # "C" lettermark
    draw.text((14, 10), "C", fill=(255, 255, 255))

    # Status dot
    dot_color = {"ok": (80, 220, 120), "loading": (255, 200, 60), "error": (220, 80, 80)}.get(status, (150, 150, 150))
    draw.ellipse([44, 44, 58, 58], fill=dot_color)

    log.debug("Finished make_tray_icon")
    return img

# ── Popup Window ──────────────────────────────────────────────────────────────

class UsagePopup:
    """
    Always-on-top popup anchored above the system tray.
    Built once; data is updated in-place via StringVars and canvas redraws.
    """

    BAR_W = 260
    BAR_H = 7
    BG    = "#1e1e23"
    TRACK = "#2e2e38"
    GREEN = "#50d490"

    def __init__(self, console_available: bool = False):
        log.debug("Starting UsagePopup.__init__ console_available=%s", console_available)
        self._win: tk.Tk | None = None
        self._on_refresh = None
        self._next_refresh_at: datetime | None = None
        self._refreshing: bool = False

        # StringVars populated once the window is built
        self._vars: dict[str, tk.StringVar] = {}
        # Canvas widgets for bars (need direct redraw)
        self._d_bar: tk.Canvas | None = None
        self._w_bar: tk.Canvas | None = None
        # Dynamic content frame for per-project breakdown
        self._proj_content: tk.Frame | None = None
        # Console stats section (only built when selenium is available)
        self._console_available = console_available
        self._cs_content: tk.Frame | None = None
        log.debug("Finished UsagePopup.__init__")

    # ── Public API ────────────────────────────────────────────────────────────

    def show(self, usage: dict | None, error: str | None = None,
             next_refresh_at: datetime | None = None,
             on_refresh: callable = None):
        """Open (or un-hide) the popup and fill with current data."""
        log.debug("Starting UsagePopup.show error=%s", error)
        self._on_refresh      = on_refresh
        self._next_refresh_at = next_refresh_at

        if self._win and self._win.winfo_exists():
            # Window already exists (may be hidden) — update and un-hide on the main thread
            self._win.after(0, lambda: self._reshow(usage, error))
            log.debug("Finished UsagePopup.show (scheduled _reshow)")
            return

        # First open: build window and enter mainloop (blocks this thread)
        self._build_window()
        self._apply(usage, error)
        self._fit_window()
        self._tick()
        log.debug("Finished UsagePopup.show (entering mainloop)")
        self._win.mainloop()

    def _reshow(self, usage: dict | None, error: str | None):
        """Called on the Tk main thread to un-hide and refresh the window."""
        log.debug("Starting UsagePopup._reshow")
        self._apply(usage, error)
        self._fit_window()
        self._win.deiconify()
        self._win.lift()
        log.debug("Finished UsagePopup._reshow")

    def update(self, usage: dict | None, error: str | None,
               next_refresh_at: datetime | None):
        """Thread-safe update — can be called from background threads."""
        log.debug("Starting UsagePopup.update error=%s", error)
        self._refreshing = False
        self._next_refresh_at = next_refresh_at
        if self._win and self._win.winfo_exists():
            self._win.after(0, lambda: self._apply(usage, error))
        log.debug("Finished UsagePopup.update")

    def start_refresh_display(self):
        """Thread-safe: show 'Refreshing…' immediately and pause the tick loop."""
        log.debug("Starting UsagePopup.start_refresh_display")
        self._refreshing = True
        if self._win and self._win.winfo_exists():
            self._win.after(0, lambda: self._vars["countdown"].set("Refreshing…"))
        log.debug("Finished UsagePopup.start_refresh_display")

    # ── Window construction (called once) ─────────────────────────────────────

    def _build_window(self):
        log.debug("Starting UsagePopup._build_window")
        win = tk.Tk()
        self._win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=self.BG)
        win.geometry("300x1+0+0")  # placeholder; _fit_window sets final position/size

        def _sv(key, val=""):
            v = tk.StringVar(value=val)
            self._vars[key] = v
            return v

        # ── Title bar ──
        bar = tk.Frame(win, bg="#13131a", cursor="fleur")
        bar.pack(fill="x")
        tk.Label(bar, text="Claude Usage", font=("Segoe UI", 10, "bold"),
                 fg="#ffffff", bg="#13131a", anchor="w").pack(side="left", padx=12, pady=8)
        tk.Button(bar, text="✕", command=win.withdraw,
                  font=("Segoe UI", 9), bg="#13131a", fg="#606070",
                  relief="flat", bd=0, padx=8,
                  activebackground="#e05050", activeforeground="#ffffff").pack(side="right", pady=4, padx=4)
        def _ds(e): win._dx, win._dy = e.x, e.y
        def _dm(e): win.geometry(f"+{win.winfo_x()+e.x-win._dx}+{win.winfo_y()+e.y-win._dy}")
        bar.bind("<ButtonPress-1>", _ds)
        bar.bind("<B1-Motion>", _dm)

        # ── Today ──
        self._divider(win)
        tk.Label(win, textvariable=_sv("d_head"), font=("Segoe UI", 8, "bold"),
                 fg="#a0a0b0", bg=self.BG, wraplength=270).pack(anchor="w", padx=16)
        self._var_row(win, "Input",  "d_in")
        self._var_row(win, "Output", "d_out")
        self._var_row(win, "Total",  "d_total", bold=True)
        self._d_bar = self._make_bar(win)
        tk.Label(win, textvariable=_sv("d_bar_lbl"), font=("Segoe UI", 7),
                 fg="#808090", bg=self.BG).pack(anchor="w", padx=20)

        # ── This week ──
        self._divider(win)
        tk.Label(win, textvariable=_sv("w_head"), font=("Segoe UI", 8, "bold"),
                 fg="#a0a0b0", bg=self.BG, wraplength=270).pack(anchor="w", padx=16)
        self._var_row(win, "Input",  "w_in")
        self._var_row(win, "Output", "w_out")
        self._var_row(win, "Total",  "w_total", bold=True)
        self._w_bar = self._make_bar(win)
        tk.Label(win, textvariable=_sv("w_bar_lbl"), font=("Segoe UI", 7),
                 fg="#808090", bg=self.BG).pack(anchor="w", padx=20)

        # ── Last execution (collapsible, starts collapsed) ──
        ex_content = self._collapsible_section(win, "Last execution", initial_open=False)
        tk.Label(ex_content, textvariable=_sv("ex_head"), font=("Segoe UI", 7),
                 fg="#606070", bg=self.BG).pack(anchor="w", padx=20, pady=(4, 0))
        self._var_row(ex_content, "Fresh in",  "ex_in")
        self._var_row(ex_content, "Cache +",   "ex_cc")
        self._var_row(ex_content, "Cache hit", "ex_cr")
        self._var_row(ex_content, "Output",    "ex_out")
        self._var_row(ex_content, "Total",     "ex_total", bold=True)
        tk.Frame(ex_content, height=4, bg=self.BG).pack()

        # ── Per project breakdown (collapsible, starts open) ──
        self._proj_content = self._collapsible_section(win, "Per project — Today", initial_open=True)

        # ── Account stats via console (collapsible, starts collapsed) ──
        if self._console_available:
            self._cs_content = self._collapsible_section(
                win, "Account stats — Console", initial_open=False
            )
            tk.Label(self._cs_content, textvariable=_sv("cs_status"),
                     font=("Segoe UI", 7), fg="#606070", bg=self.BG).pack(
                         anchor="w", padx=20, pady=(4, 0))
            self._var_row(self._cs_content, "Period",    "cs_period")
            self._var_row(self._cs_content, "Input",     "cs_input")
            self._var_row(self._cs_content, "Cache +",   "cs_cc")
            self._var_row(self._cs_content, "Cache hit", "cs_cr")
            self._var_row(self._cs_content, "Output",    "cs_output")
            self._var_row(self._cs_content, "Total",     "cs_total", bold=True)
            tk.Frame(self._cs_content, height=4, bg=self.BG).pack()

        # ── Countdown ──
        self._divider(win)
        tk.Label(win, textvariable=_sv("countdown"), font=("Segoe UI", 8),
                 fg="#606070", bg=self.BG).pack(pady=(2, 0))
        tk.Frame(win, height=8, bg=self.BG).pack()
        log.debug("Finished UsagePopup._build_window")

    # ── Window sizing / collapsible helpers ──────────────────────────────────

    def _fit_window(self):
        """Resize and reposition the window to fit its current content."""
        log.debug("Starting UsagePopup._fit_window")
        if not (self._win and self._win.winfo_exists()):
            log.debug("Finished UsagePopup._fit_window (window does not exist)")
            return
        if self._win.state() == "withdrawn":
            log.debug("Finished UsagePopup._fit_window (window withdrawn)")
            return
        try:
            self._win.update_idletasks()
            h = self._win.winfo_reqheight()
            try:
                import ctypes
                class _RECT(ctypes.Structure):
                    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                                 ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
                rc = _RECT()
                ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rc), 0)
                x, y = rc.right - 300 - 4, rc.bottom - h - 4
            except Exception as exc:
                log.error("Error getting taskbar bounds in _fit_window: %s", exc)
                sw, sh = self._win.winfo_screenwidth(), self._win.winfo_screenheight()
                x, y = sw - 300 - 4, sh - h - 4
            self._win.geometry(f"300x{h}+{x}+{y}")
        except Exception as exc:
            log.error("Error in _fit_window: %s", exc)
            pass  # WinError 1402 (invalid cursor handle) can fire during widget cleanup
        log.debug("Finished UsagePopup._fit_window")

    def _collapsible_section(self, parent, title: str, initial_open: bool = True) -> tk.Frame:
        """Add a toggle-button section header; return the content frame."""
        log.debug("Starting UsagePopup._collapsible_section title=%r initial_open=%s", title, initial_open)
        tk.Frame(parent, height=1, bg="#3a3a45").pack(fill="x", padx=16, pady=(5, 0))

        content = tk.Frame(parent, bg=self.BG)

        btn = tk.Button(
            parent,
            text=f"{'▼' if initial_open else '▶'}  {title}",
            font=("Segoe UI", 8, "bold"),
            fg="#a0a0b0", bg="#13131a",
            relief="flat", bd=0, anchor="w",
            padx=16, pady=5,
            activebackground="#1c1c24",
            activeforeground="#c0c0d0",
            cursor="hand2",
        )
        btn.pack(fill="x")

        def toggle():
            log.debug("Starting toggle for section %r", title)
            if content.winfo_ismapped():
                content.pack_forget()
                btn.config(text=f"▶  {title}")
            else:
                content.pack(fill="x", after=btn)
                btn.config(text=f"▼  {title}")
            self._fit_window()
            log.debug("Finished toggle for section %r", title)

        btn.config(command=toggle)

        if initial_open:
            content.pack(fill="x", after=btn)

        log.debug("Finished UsagePopup._collapsible_section title=%r", title)
        return content

    # ── Data application (called on every refresh) ────────────────────────────

    def _apply(self, usage: dict | None, error: str | None):
        log.debug("Starting UsagePopup._apply error=%s", error)
        v = self._vars
        if error or not usage:
            v["d_head"].set(f"Error: {error}" if error else "No data")
            for k in ("d_in","d_out","d_total","w_in","w_out","w_total",
                      "ex_in","ex_cc","ex_cr","ex_out","ex_total"):
                v[k].set("—")
            v["w_head"].set("")
            v["ex_head"].set("")
            v["d_bar_lbl"].set("")
            v["w_bar_lbl"].set("")
            self._draw_bar(self._d_bar, 0, 0)
            self._draw_bar(self._w_bar, 0, 0)
            if self._proj_content:
                for child in self._proj_content.winfo_children():
                    child.destroy()
            log.debug("Finished UsagePopup._apply (error/no-data path)")
            return

        d_total = usage["daily"]["total"]
        d_in    = usage["daily"]["input"]
        d_out   = usage["daily"]["output"]
        w_total = usage["weekly"]["total"]
        w_in    = usage["weekly"]["input"]
        w_out   = usage["weekly"]["output"]
        dl      = usage["daily_limit"]
        wl      = usage["weekly_limit"]

        d_pct = f"  —  {d_total/dl*100:.1f}% of avg" if dl else ""
        v["d_head"].set(f"Today  ({usage['today']}){d_pct}")
        v["d_in"].set(f"{d_in:,}")
        v["d_out"].set(f"{d_out:,}")
        v["d_total"].set(f"{d_total:,}")
        self._draw_bar(self._d_bar, d_total, dl)
        if dl:
            v["d_bar_lbl"].set(f"  {d_total/dl*100:.1f}%  ({max(dl-d_total,0):,} remaining  (avg {dl:,}/day))")
        else:
            v["d_bar_lbl"].set("")

        w_pct = f"  —  {w_total/wl*100:.1f}% of avg" if wl else ""
        v["w_head"].set(f"This week  (since {usage['week_start']}){w_pct}")
        v["w_in"].set(f"{w_in:,}")
        v["w_out"].set(f"{w_out:,}")
        v["w_total"].set(f"{w_total:,}")
        self._draw_bar(self._w_bar, w_total, wl)
        if wl:
            v["w_bar_lbl"].set(f"  {w_total/wl*100:.1f}%  ({max(wl-w_total,0):,} remaining  (avg {wl:,}/week))")
        else:
            v["w_bar_lbl"].set("")

        ex = usage.get("last_exec")
        if ex:
            v["ex_head"].set(ex["ts"].astimezone().strftime("%a %b %d  %H:%M:%S"))
            v["ex_in"].set(f"{ex['input']:,}")
            v["ex_cc"].set(f"{ex['cache_create']:,}")
            v["ex_cr"].set(f"{ex['cache_read']:,}")
            v["ex_out"].set(f"{ex['output']:,}")
            v["ex_total"].set(f"{ex['input']+ex['cache_create']+ex['cache_read']+ex['output']:,}")
        else:
            v["ex_head"].set("(none)")
            for k in ("ex_in","ex_cc","ex_cr","ex_out","ex_total"):
                v[k].set("—")

        # ── Per-project breakdown (rebuild dynamic rows) ──
        if self._proj_content:
            for child in self._proj_content.winfo_children():
                child.destroy()
            breakdown = usage.get("project_breakdown", {})
            if breakdown:
                for name, data in sorted(breakdown.items(), key=lambda x: -x[1]["total"]):
                    pct = data["total"] / d_total if d_total > 0 else 0
                    color = "#e05050" if pct >= 0.90 else "#f0a030" if pct >= 0.70 else self.GREEN
                    display = name if len(name) <= 24 else name[:21] + "…"
                    row = tk.Frame(self._proj_content, bg=self.BG)
                    row.pack(fill="x", padx=20, pady=(5, 0))
                    tk.Label(row, text=display, font=("Segoe UI", 9),
                             fg="#c8c8d8", bg=self.BG, anchor="w").pack(side="left")
                    tk.Label(row, text=f"{pct*100:.1f}%  ({data['total']:,})",
                             font=("Segoe UI", 9), fg="#808090", bg=self.BG,
                             anchor="e").pack(side="right")
                    c = tk.Canvas(self._proj_content, width=self.BAR_W, height=self.BAR_H,
                                  bg=self.BG, highlightthickness=0)
                    c.pack(padx=20, pady=(2, 0))
                    c.create_rectangle(0, 0, self.BAR_W, self.BAR_H, fill=self.TRACK, outline="")
                    if pct > 0:
                        c.create_rectangle(0, 0, int(self.BAR_W * pct), self.BAR_H,
                                           fill=color, outline="")
                tk.Frame(self._proj_content, height=4, bg=self.BG).pack()
            else:
                tk.Label(self._proj_content, text="No data today",
                         font=("Segoe UI", 8), fg="#505060", bg=self.BG).pack(
                             anchor="w", padx=20, pady=6)
            self._win.after(50, self._fit_window)
        log.debug("Finished UsagePopup._apply")

    # ── Console stats update (called from ConsoleFetcher thread) ─────────────

    def apply_console(self, state: dict):
        """Thread-safe: push console fetch state into the UI."""
        log.debug("Starting UsagePopup.apply_console status=%s", state.get("status"))
        if self._win and self._win.winfo_exists():
            self._win.after(0, lambda: self._apply_console(state))
        log.debug("Finished UsagePopup.apply_console")

    def _apply_console(self, state: dict):
        """Must be called on the Tk main thread."""
        log.debug("Starting UsagePopup._apply_console status=%s", state.get("status"))
        if not self._console_available or self._cs_content is None:
            log.debug("Finished UsagePopup._apply_console (console not available)")
            return
        v          = self._vars
        status     = state.get("status", "loading")
        data       = state.get("data")
        fetched_at = state.get("fetched_at")
        error      = state.get("error", "")

        dash = "—"

        if status == "loading":
            v["cs_status"].set("Loading…")
            for k in ("cs_period","cs_input","cs_cc","cs_cr","cs_output","cs_total"):
                v[k].set(dash)

        elif status == "waiting_login":
            v["cs_status"].set("Waiting for login — check Chrome window…")
            for k in ("cs_period","cs_input","cs_cc","cs_cr","cs_output","cs_total"):
                v[k].set(dash)

        elif status == "error":
            short = (error[:60] + "…") if len(error) > 60 else error
            v["cs_status"].set(f"Error: {short}")
            # Keep existing token values visible if we have them

        elif status == "ok" and data:
            if fetched_at:
                age = int((datetime.now() - fetched_at).total_seconds() / 60)
                v["cs_status"].set("Just fetched" if age < 1 else f"Fetched {age} min ago")
            else:
                v["cs_status"].set("OK")

            # Period dates
            ps, pe = data.get("period_start"), data.get("period_end")
            if ps and pe:
                try:
                    fmt = lambda s: datetime.fromisoformat(
                        s.replace("Z", "+00:00")).strftime("%b %d")
                    v["cs_period"].set(f"{fmt(ps)} – {fmt(pe)}")
                except Exception as exc:
                    log.error("Error formatting console period dates: %s", exc)
                    v["cs_period"].set("")
            else:
                v["cs_period"].set("")

            v["cs_input"].set(f"{data.get('input', 0):,}")
            v["cs_cc"].set(f"{data.get('cache_create', 0):,}")
            v["cs_cr"].set(f"{data.get('cache_read', 0):,}")
            v["cs_output"].set(f"{data.get('output', 0):,}")
            v["cs_total"].set(f"{data.get('total', 0):,}")
        log.debug("Finished UsagePopup._apply_console")

    # ── Countdown tick ────────────────────────────────────────────────────────

    def _tick(self):
        log.debug("Starting UsagePopup._tick")
        if not (self._win and self._win.winfo_exists()):
            log.debug("Finished UsagePopup._tick (window gone)")
            return
        if self._refreshing:
            # Keep ticking while the label shows "Refreshing…" so we pick up
            # when update() clears the flag (Refresh Now path).
            self._win.after(1000, self._tick)
            log.debug("Finished UsagePopup._tick (refreshing, rescheduled)")
            return
        nra = self._next_refresh_at
        if nra:
            secs = max(0, int((nra - datetime.now()).total_seconds()))
            if secs == 0 and self._on_refresh:
                self._refreshing = True
                self._vars["countdown"].set("Refreshing…")
                threading.Thread(target=self._do_bg_refresh, daemon=True).start()
                log.debug("Finished UsagePopup._tick (triggered bg refresh)")
                return  # _do_bg_refresh will restart _tick after completion
            m, s = divmod(secs, 60)
            self._vars["countdown"].set(f"Refreshes in {m}:{s:02d}")
        else:
            self._vars["countdown"].set("Refresh time unknown")
        self._win.after(1000, self._tick)
        log.debug("Finished UsagePopup._tick")

    def _do_bg_refresh(self):
        log.debug("Starting UsagePopup._do_bg_refresh")
        if self._on_refresh:
            self._on_refresh()   # updates widget._usage and _next_refresh_at, calls popup.update()
        # Restart the countdown loop on the main thread
        if self._win and self._win.winfo_exists():
            self._win.after(0, self._tick)
        log.debug("Finished UsagePopup._do_bg_refresh")

    # ── Bar helpers ───────────────────────────────────────────────────────────

    def _make_bar(self, parent) -> tk.Canvas:
        log.debug("Starting UsagePopup._make_bar")
        c = tk.Canvas(parent, width=self.BAR_W, height=self.BAR_H,
                      bg=self.BG, highlightthickness=0)
        c.pack(padx=20, pady=(3, 0))
        log.debug("Finished UsagePopup._make_bar")
        return c

    def _draw_bar(self, canvas: tk.Canvas, used: int, limit: int):
        log.debug("Starting UsagePopup._draw_bar used=%d limit=%d", used, limit)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, self.BAR_W, self.BAR_H, fill=self.TRACK, outline="")
        if limit > 0 and used > 0:
            pct   = min(used / limit, 1.0)
            color = "#e05050" if pct >= 0.90 else "#f0a030" if pct >= 0.70 else self.GREEN
            canvas.create_rectangle(0, 0, int(self.BAR_W * pct), self.BAR_H, fill=color, outline="")
        log.debug("Finished UsagePopup._draw_bar")

    # ── Layout helpers ────────────────────────────────────────────────────────

    def _divider(self, parent):
        log.debug("Starting UsagePopup._divider")
        tk.Frame(parent, height=1, bg="#3a3a45").pack(fill="x", padx=16, pady=5)
        log.debug("Finished UsagePopup._divider")

    def _var_row(self, parent, label: str, key: str, bold: bool = False):
        log.debug("Starting UsagePopup._var_row label=%r key=%r", label, key)
        fw    = "bold" if bold else "normal"
        color = "#ffffff" if bold else "#c8c8d8"
        frame = tk.Frame(parent, bg=self.BG)
        frame.pack(fill="x", padx=20, pady=1)
        tk.Label(frame, text=label, font=("Segoe UI", 9, fw),
                 fg=color, bg=self.BG, width=8, anchor="w").pack(side="left")
        tk.Label(frame, textvariable=self._vars.setdefault(key, tk.StringVar()),
                 font=("Segoe UI", 9, fw), fg=color, bg=self.BG,
                 anchor="e").pack(side="right")
        log.debug("Finished UsagePopup._var_row label=%r key=%r", label, key)

# ── Console stats fetcher (Selenium) ─────────────────────────────────────────

class ConsoleFetcher:
    """
    Drives a persistent headless Chrome session to scrape token-usage data
    from console.anthropic.com.  Falls back to a visible window on first run
    so the user can complete Google OAuth — cookies are then saved in
    CONSOLE_PROFILE_DIR and reused on every subsequent start.

    Call is_available() first; if selenium is not installed this class still
    imports safely but start() becomes a no-op.
    """

    CONSOLE_URL = "https://console.anthropic.com"
    USAGE_URL   = "https://console.anthropic.com/settings/usage"
    LOGIN_TIMEOUT = 300  # seconds user has to log in

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
        log.debug("Starting ConsoleFetcher.__init__")
        self._driver         = None
        self._data: dict | None = None
        self._error: str | None = None
        self._status         = "loading"   # loading | waiting_login | ok | error
        self._fetched_at: datetime | None = None
        self._lock           = threading.Lock()
        self._on_update      = None        # callable(state_dict)
        log.debug("Finished ConsoleFetcher.__init__")

    # ── Public ────────────────────────────────────────────────────────────────

    @staticmethod
    def is_available() -> bool:
        """True if selenium is installed (chromedriver need not be pre-installed;
        Selenium 4.6+ will auto-download it via selenium-manager)."""
        log.debug("Starting ConsoleFetcher.is_available")
        try:
            import selenium  # noqa: F401
            log.debug("Finished ConsoleFetcher.is_available: True")
            return True
        except ImportError as exc:
            log.debug("Selenium not available: %s", exc)
            log.debug("Finished ConsoleFetcher.is_available: False")
            return False

    def start(self, on_update):
        """Start the background fetch loop.  on_update(state_dict) is called
        from the worker thread whenever data or status changes."""
        log.debug("Starting ConsoleFetcher.start")
        self._on_update = on_update
        threading.Thread(target=self._loop, daemon=True).start()
        log.debug("Finished ConsoleFetcher.start")

    def fetch_now(self):
        """Trigger an immediate re-fetch without waiting for the interval."""
        log.debug("Starting ConsoleFetcher.fetch_now")
        threading.Thread(target=self._fetch, daemon=True).start()
        log.debug("Finished ConsoleFetcher.fetch_now")

    def get_state(self) -> dict:
        log.debug("Starting ConsoleFetcher.get_state")
        with self._lock:
            state = {
                "status":     self._status,
                "data":       self._data,
                "error":      self._error,
                "fetched_at": self._fetched_at,
            }
        log.debug("Finished ConsoleFetcher.get_state status=%s", state["status"])
        return state

    # ── Background loop ───────────────────────────────────────────────────────

    def _loop(self):
        log.debug("Starting ConsoleFetcher._loop")
        while True:
            self._fetch()
            time.sleep(CONSOLE_REFRESH_MINUTES * 60)

    def _fetch(self):
        log.debug("Starting ConsoleFetcher._fetch")
        try:
            self._set_status("loading")
            self._notify()

            driver = self._get_driver(headless=CONSOLE_HEADLESS)

            # ── Check login state ──
            driver.get(self.CONSOLE_URL)
            time.sleep(2)

            if self._needs_login(driver):
                if CONSOLE_HEADLESS:
                    # Must go visible for Google OAuth (headless is blocked by Google)
                    self._quit_driver()
                    driver = self._get_driver(headless=False)
                    driver.get(self.CONSOLE_URL)
                    time.sleep(4)   # wait for login redirect to settle before polling

                self._set_status("waiting_login")
                self._notify()

                if not self._wait_for_login(driver):
                    raise TimeoutError("Login timed out — reopen the widget to retry")

                if CONSOLE_HEADLESS:
                    # Profile now has valid cookies; restart headless
                    self._quit_driver()
                    driver = self._get_driver(headless=True)

            # ── Navigate to usage page and capture XHR ──
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
            log.error("Error in ConsoleFetcher._fetch: %s", exc)
            with self._lock:
                self._error  = str(exc)
                self._status = "error"
        finally:
            log.debug("Entering finally block in ConsoleFetcher._fetch")
            self._notify()
        log.debug("Finished ConsoleFetcher._fetch")

    # ── Driver management ─────────────────────────────────────────────────────

    def _get_driver(self, headless: bool = True):
        """Return the live driver or create a fresh one."""
        log.debug("Starting ConsoleFetcher._get_driver headless=%s", headless)
        if self._driver is not None:
            try:
                _ = self._driver.title   # raises if dead
                log.debug("Finished ConsoleFetcher._get_driver (reusing existing driver)")
                return self._driver
            except Exception as exc:
                log.warning("Existing driver is dead (%s), creating new one", exc)
                self._driver = None

        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        os.makedirs(CONSOLE_PROFILE_DIR, exist_ok=True)

        # Remove stale Chrome lock files left over from a previous session.
        # If these exist when Chrome starts it will refuse to open the profile.
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
        log.debug("Finished ConsoleFetcher._get_driver (new driver created)")
        return self._driver

    def _quit_driver(self):
        log.debug("Starting ConsoleFetcher._quit_driver")
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception as exc:
                log.warning("Error quitting driver: %s", exc)
            self._driver = None
        log.debug("Finished ConsoleFetcher._quit_driver")

    # ── Login helpers ─────────────────────────────────────────────────────────

    def _needs_login(self, driver) -> bool:
        log.debug("Starting ConsoleFetcher._needs_login")
        url = driver.current_url.lower()
        result = any(kw in url for kw in ("login", "signin", "accounts.google", "/auth"))
        log.debug("Finished ConsoleFetcher._needs_login: %s", result)
        return result

    def _wait_for_login(self, driver) -> bool:
        """Block until the user finishes OAuth or LOGIN_TIMEOUT elapses."""
        log.debug("Starting ConsoleFetcher._wait_for_login timeout=%ds", self.LOGIN_TIMEOUT)
        deadline = time.time() + self.LOGIN_TIMEOUT
        while time.time() < deadline:
            if not self._needs_login(driver):
                time.sleep(2)   # let the post-login redirect settle
                log.debug("Finished ConsoleFetcher._wait_for_login: True")
                return True
            time.sleep(2)
        log.warning("ConsoleFetcher._wait_for_login timed out after %ds", self.LOGIN_TIMEOUT)
        log.debug("Finished ConsoleFetcher._wait_for_login: False")
        return False

    # ── Usage capture ─────────────────────────────────────────────────────────

    def _capture_usage(self, driver) -> dict | None:
        """Navigate to the usage page and retrieve intercepted XHR responses."""
        log.debug("Starting ConsoleFetcher._capture_usage")
        from selenium.webdriver.support.ui import WebDriverWait

        driver.get(self.USAGE_URL)
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(4)   # let XHR calls finish after DOM ready

        captured = driver.execute_script("return window._capturedResponses || []") or []

        # Try URL-hinted responses first (more likely to be usage data)
        url_keywords = ("usage", "billing", "cost", "token", "organization", "metric")
        for item in sorted(captured,
                           key=lambda i: any(kw in i.get("url","").lower() for kw in url_keywords),
                           reverse=True):
            body = item.get("body")
            if not isinstance(body, dict):
                continue
            parsed = self._parse_response(body)
            if parsed:
                log.debug("Finished ConsoleFetcher._capture_usage (found data)")
                return parsed

        log.warning("ConsoleFetcher._capture_usage found no parseable usage data")
        log.debug("Finished ConsoleFetcher._capture_usage: None")
        return None

    # ── Response parser ───────────────────────────────────────────────────────

    def _parse_response(self, body: dict) -> dict | None:
        """Normalise a captured API response into our display format.
        Handles the documented Admin API shape as well as reasonable variants."""
        log.debug("Starting ConsoleFetcher._parse_response")
        if not isinstance(body, dict):
            log.debug("Finished ConsoleFetcher._parse_response: None (not a dict)")
            return None

        # ── Format A: {results|data|buckets: [{token fields, ...}]} ──
        for key in ("results", "data", "buckets", "items"):
            results = body.get(key)
            if results and isinstance(results, list):
                totals = {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0}
                period_start = period_end = None
                found = False

                for bucket in results:
                    if not isinstance(bucket, dict):
                        continue
                    inp = (bucket.get("uncached_input_tokens")
                           or bucket.get("input_tokens") or 0)
                    cc  = bucket.get("cache_creation_input_tokens") or 0
                    cr  = bucket.get("cache_read_input_tokens") or 0
                    out = bucket.get("output_tokens") or 0
                    if inp + cc + cr + out > 0:
                        found = True
                    totals["input"]        += inp
                    totals["cache_create"] += cc
                    totals["cache_read"]   += cr
                    totals["output"]       += out
                    for sk in ("start_time", "period_start", "from", "start"):
                        s = bucket.get(sk)
                        if s and (period_start is None or s < period_start):
                            period_start = s
                    for ek in ("end_time", "period_end", "to", "end"):
                        e = bucket.get(ek)
                        if e and (period_end is None or e > period_end):
                            period_end = e

                if found:
                    log.debug("Finished ConsoleFetcher._parse_response (Format A, key=%r)", key)
                    return {**totals,
                            "total":        sum(totals.values()),
                            "period_start": period_start,
                            "period_end":   period_end}

        # ── Format B: token fields directly on the object ──
        inp = body.get("input_tokens") or body.get("uncached_input_tokens") or 0
        out = body.get("output_tokens") or 0
        if inp + out > 0:
            cc = body.get("cache_creation_input_tokens") or 0
            cr = body.get("cache_read_input_tokens") or 0
            log.debug("Finished ConsoleFetcher._parse_response (Format B)")
            return {"input": inp, "cache_create": cc, "cache_read": cr, "output": out,
                    "total": inp + cc + cr + out,
                    "period_start": None, "period_end": None}

        log.debug("Finished ConsoleFetcher._parse_response: None (no matching format)")
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, status: str):
        log.debug("Starting ConsoleFetcher._set_status status=%s", status)
        with self._lock:
            self._status = status
        log.debug("Finished ConsoleFetcher._set_status")

    def _notify(self):
        log.debug("Starting ConsoleFetcher._notify")
        if self._on_update:
            try:
                self._on_update(self.get_state())
            except Exception as exc:
                log.error("Error in ConsoleFetcher._notify callback: %s", exc)
        log.debug("Finished ConsoleFetcher._notify")

# ── Windows startup helpers ───────────────────────────────────────────────────

_STARTUP_REG_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_REG_NAME = "ClaudeUsageWidget"


def _startup_command() -> str:
    """Build the command stored in the registry — uses pythonw to suppress the console."""
    log.debug("Starting _startup_command")
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    script  = os.path.abspath(__file__)
    result = f'"{pythonw}" "{script}"'
    log.debug("Finished _startup_command: %s", result)
    return result


def _startup_registered() -> bool:
    log.debug("Starting _startup_registered")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY) as k:
            val, _ = winreg.QueryValueEx(k, _STARTUP_REG_NAME)
            result = val == _startup_command()
            log.debug("Finished _startup_registered: %s", result)
            return result
    except Exception as exc:
        log.debug("_startup_registered: not registered (%s)", exc)
        log.debug("Finished _startup_registered: False")
        return False


def _add_to_startup():
    log.debug("Starting _add_to_startup")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY,
                        access=winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, _STARTUP_REG_NAME, 0, winreg.REG_SZ, _startup_command())
    log.debug("Finished _add_to_startup")


def _remove_from_startup():
    log.debug("Starting _remove_from_startup")
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY,
                            access=winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, _STARTUP_REG_NAME)
    except FileNotFoundError as exc:
        log.debug("_remove_from_startup: key not found (%s)", exc)
    log.debug("Finished _remove_from_startup")


# ── Widget Core ───────────────────────────────────────────────────────────────

class ClaudeUsageWidget:
    def __init__(self):
        log.debug("Starting ClaudeUsageWidget.__init__")
        self._usage: dict | None = None
        self._error: str | None = None
        self._next_refresh_at: datetime | None = None
        self._stop_event = threading.Event()

        self._console = (ConsoleFetcher() if ConsoleFetcher.is_available() else None) \
            if CONSOLE_FETCHER_ENABLED else None
        self._popup   = UsagePopup(console_available=self._console is not None)

        # Import here so the script still imports even if pystray isn't installed yet
        try:
            import pystray
        except ImportError as exc:
            log.error("pystray not found: %s", exc)
            print("pystray not found. Install with: pip install pystray Pillow requests")
            sys.exit(1)

        self._icon = pystray.Icon(
            name="claude-usage",
            icon=make_tray_icon("loading"),
            title="Claude Usage — loading...",
            menu=pystray.Menu(
                pystray.MenuItem("Show Usage", self._on_click, default=True),
                pystray.MenuItem("Refresh Now", self._refresh_now),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    lambda item: "Remove from Startup" if _startup_registered() else "Add to Startup",
                    self._toggle_startup,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            ),
        )
        log.debug("Finished ClaudeUsageWidget.__init__")

    # ── Event handlers ──

    def _open_popup(self):
        log.debug("Starting ClaudeUsageWidget._open_popup")
        self._popup.show(
            self._usage, self._error, self._next_refresh_at,
            on_refresh=self._refresh_and_reopen,
        )
        log.debug("Finished ClaudeUsageWidget._open_popup")

    def _refresh_and_reopen(self):
        log.debug("Starting ClaudeUsageWidget._refresh_and_reopen")
        self._do_refresh()
        self._popup.update(self._usage, self._error, self._next_refresh_at)
        log.debug("Finished ClaudeUsageWidget._refresh_and_reopen")

    def _on_click(self, icon, item=None):
        log.debug("Starting ClaudeUsageWidget._on_click")
        threading.Thread(target=self._open_popup, daemon=True).start()
        log.debug("Finished ClaudeUsageWidget._on_click")

    def _refresh_now(self, icon=None, item=None):
        log.debug("Starting ClaudeUsageWidget._refresh_now")
        def _do():
            log.debug("Starting ClaudeUsageWidget._refresh_now._do")
            self._popup.start_refresh_display()
            self._do_refresh()
            self._popup.update(self._usage, self._error, self._next_refresh_at)
            log.debug("Finished ClaudeUsageWidget._refresh_now._do")
        threading.Thread(target=_do, daemon=True).start()
        log.debug("Finished ClaudeUsageWidget._refresh_now")

    def _toggle_startup(self, icon, item):
        log.debug("Starting ClaudeUsageWidget._toggle_startup")
        if _startup_registered():
            _remove_from_startup()
        else:
            _add_to_startup()
        log.debug("Finished ClaudeUsageWidget._toggle_startup")

    def _quit(self, icon, item):
        log.debug("Starting ClaudeUsageWidget._quit")
        self._stop_event.set()
        if self._console is not None:
            self._console._quit_driver()
        icon.stop()
        log.debug("Finished ClaudeUsageWidget._quit")

    # ── Data fetching ──

    def _do_refresh(self):
        log.debug("Starting ClaudeUsageWidget._do_refresh")
        self._icon.icon = make_tray_icon("loading")
        self._icon.title = "Claude Usage — refreshing..."
        try:
            self._usage = get_usage_summary()
            self._error = None
            d = self._usage["daily"]["total"]
            w = self._usage["weekly"]["total"]
            self._icon.icon = make_tray_icon("ok")
            self._icon.title = f"Claude Usage\nToday: {d:,} tokens\nThis week: {w:,} tokens"
        except Exception as exc:
            log.error("Error in ClaudeUsageWidget._do_refresh: %s", exc)
            self._error = str(exc)
            self._icon.icon = make_tray_icon("error")
            self._icon.title = f"Claude Usage — error"
        finally:
            log.debug("Entering finally block in ClaudeUsageWidget._do_refresh")
            self._next_refresh_at = datetime.now() + timedelta(seconds=REFRESH_INTERVAL_SECONDS)
        log.debug("Finished ClaudeUsageWidget._do_refresh")

    def _refresh_loop(self, icon):
        log.debug("Starting ClaudeUsageWidget._refresh_loop")
        icon.visible = True
        while not self._stop_event.is_set():
            self._do_refresh()
            self._stop_event.wait(REFRESH_INTERVAL_SECONDS)

    def run(self):
        log.debug("Starting ClaudeUsageWidget.run")
        if self._console is not None:
            self._console.start(on_update=self._popup.apply_console)
        self._icon.run(self._refresh_loop)
        log.debug("Finished ClaudeUsageWidget.run")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Claude Usage Widget starting up")
    ClaudeUsageWidget().run()
