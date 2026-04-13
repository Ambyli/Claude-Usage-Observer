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

import os
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Load .env from the same directory as this script
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

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

# ── Local JSONL parser ────────────────────────────────────────────────────────

def _build_daily_totals(since: date) -> tuple[dict, dict | None, dict]:
    """
    Scan ~/.claude/projects/**/*.jsonl once and return:
      - {date: (input, output)} for every day on or after `since`
      - the most recent completed assistant turn (or None)
      - {project_name: {input, output, total}} for today only
    """
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
            except Exception:
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

    return {d: (v[0], v[1]) for d, v in totals.items()}, last_exec, proj_breakdown


def _day_total(totals: dict, d: date) -> int:
    v = totals.get(d, (0, 0))
    return v[0] + v[1]


def _week_total(totals: dict, week_end: date) -> int:
    """Sum tokens for the 7-day window ending on week_end (inclusive)."""
    return sum(_day_total(totals, week_end - timedelta(days=i)) for i in range(7))


# ── Summary ───────────────────────────────────────────────────────────────────

def get_usage_summary() -> dict:
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
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background rounded square
    draw.rounded_rectangle([2, 2, 62, 62], radius=12, fill=(30, 30, 35))

    # "C" lettermark
    draw.text((14, 10), "C", fill=(255, 255, 255))

    # Status dot
    dot_color = {"ok": (80, 220, 120), "loading": (255, 200, 60), "error": (220, 80, 80)}.get(status, (150, 150, 150))
    draw.ellipse([44, 44, 58, 58], fill=dot_color)

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

    def __init__(self):
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

    # ── Public API ────────────────────────────────────────────────────────────

    def show(self, usage: dict | None, error: str | None = None,
             next_refresh_at: datetime | None = None,
             on_refresh: callable = None):
        """Open the popup (or bring it to front) and fill with current data."""
        self._on_refresh    = on_refresh
        self._next_refresh_at = next_refresh_at

        if self._win and self._win.winfo_exists():
            # Already open — just update data and lift
            self._apply(usage, error)
            self._win.lift()
            return

        self._build_window()
        self._apply(usage, error)
        self._fit_window()
        self._tick()
        self._win.mainloop()

    def update(self, usage: dict | None, error: str | None,
               next_refresh_at: datetime | None):
        """Thread-safe update — can be called from background threads."""
        self._refreshing = False
        self._next_refresh_at = next_refresh_at
        if self._win and self._win.winfo_exists():
            self._win.after(0, lambda: self._apply(usage, error))

    def start_refresh_display(self):
        """Thread-safe: show 'Refreshing…' immediately and pause the tick loop."""
        self._refreshing = True
        if self._win and self._win.winfo_exists():
            self._win.after(0, lambda: self._vars["countdown"].set("Refreshing…"))

    # ── Window construction (called once) ─────────────────────────────────────

    def _build_window(self):
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
        tk.Button(bar, text="✕", command=win.destroy,
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

        # ── Countdown ──
        self._divider(win)
        tk.Label(win, textvariable=_sv("countdown"), font=("Segoe UI", 8),
                 fg="#606070", bg=self.BG).pack(pady=(2, 0))
        tk.Frame(win, height=8, bg=self.BG).pack()

    # ── Window sizing / collapsible helpers ──────────────────────────────────

    def _fit_window(self):
        """Resize and reposition the window to fit its current content."""
        if not (self._win and self._win.winfo_exists()):
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
            except Exception:
                sw, sh = self._win.winfo_screenwidth(), self._win.winfo_screenheight()
                x, y = sw - 300 - 4, sh - h - 4
            self._win.geometry(f"300x{h}+{x}+{y}")
        except Exception:
            pass  # WinError 1402 (invalid cursor handle) can fire during widget cleanup

    def _collapsible_section(self, parent, title: str, initial_open: bool = True) -> tk.Frame:
        """Add a toggle-button section header; return the content frame."""
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
            if content.winfo_ismapped():
                content.pack_forget()
                btn.config(text=f"▶  {title}")
            else:
                content.pack(fill="x", after=btn)
                btn.config(text=f"▼  {title}")
            self._fit_window()

        btn.config(command=toggle)

        if initial_open:
            content.pack(fill="x", after=btn)

        return content

    # ── Data application (called on every refresh) ────────────────────────────

    def _apply(self, usage: dict | None, error: str | None):
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
            self._win.after(5, self._fit_window)

    # ── Countdown tick ────────────────────────────────────────────────────────

    def _tick(self):
        if not (self._win and self._win.winfo_exists()):
            return
        if self._refreshing:
            # Keep ticking while the label shows "Refreshing…" so we pick up
            # when update() clears the flag (Refresh Now path).
            self._win.after(1000, self._tick)
            return
        nra = self._next_refresh_at
        if nra:
            secs = max(0, int((nra - datetime.now()).total_seconds()))
            if secs == 0 and self._on_refresh:
                self._refreshing = True
                self._vars["countdown"].set("Refreshing…")
                threading.Thread(target=self._do_bg_refresh, daemon=True).start()
                return  # _do_bg_refresh will restart _tick after completion
            m, s = divmod(secs, 60)
            self._vars["countdown"].set(f"Refreshes in {m}:{s:02d}")
        else:
            self._vars["countdown"].set("Refresh time unknown")
        self._win.after(1000, self._tick)

    def _do_bg_refresh(self):
        if self._on_refresh:
            self._on_refresh()   # updates widget._usage and _next_refresh_at, calls popup.update()
        # Restart the countdown loop on the main thread
        if self._win and self._win.winfo_exists():
            self._win.after(0, self._tick)

    # ── Bar helpers ───────────────────────────────────────────────────────────

    def _make_bar(self, parent) -> tk.Canvas:
        c = tk.Canvas(parent, width=self.BAR_W, height=self.BAR_H,
                      bg=self.BG, highlightthickness=0)
        c.pack(padx=20, pady=(3, 0))
        return c

    def _draw_bar(self, canvas: tk.Canvas, used: int, limit: int):
        canvas.delete("all")
        canvas.create_rectangle(0, 0, self.BAR_W, self.BAR_H, fill=self.TRACK, outline="")
        if limit > 0 and used > 0:
            pct   = min(used / limit, 1.0)
            color = "#e05050" if pct >= 0.90 else "#f0a030" if pct >= 0.70 else self.GREEN
            canvas.create_rectangle(0, 0, int(self.BAR_W * pct), self.BAR_H, fill=color, outline="")

    # ── Layout helpers ────────────────────────────────────────────────────────

    def _divider(self, parent):
        tk.Frame(parent, height=1, bg="#3a3a45").pack(fill="x", padx=16, pady=5)

    def _var_row(self, parent, label: str, key: str, bold: bool = False):
        fw    = "bold" if bold else "normal"
        color = "#ffffff" if bold else "#c8c8d8"
        frame = tk.Frame(parent, bg=self.BG)
        frame.pack(fill="x", padx=20, pady=1)
        tk.Label(frame, text=label, font=("Segoe UI", 9, fw),
                 fg=color, bg=self.BG, width=8, anchor="w").pack(side="left")
        tk.Label(frame, textvariable=self._vars.setdefault(key, tk.StringVar()),
                 font=("Segoe UI", 9, fw), fg=color, bg=self.BG,
                 anchor="e").pack(side="right")

# ── Windows startup helpers ───────────────────────────────────────────────────

_STARTUP_REG_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_REG_NAME = "ClaudeUsageWidget"


def _startup_command() -> str:
    """Build the command stored in the registry — uses pythonw to suppress the console."""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    script  = os.path.abspath(__file__)
    return f'"{pythonw}" "{script}"'


def _startup_registered() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY) as k:
            val, _ = winreg.QueryValueEx(k, _STARTUP_REG_NAME)
            return val == _startup_command()
    except Exception:
        return False


def _add_to_startup():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY,
                        access=winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, _STARTUP_REG_NAME, 0, winreg.REG_SZ, _startup_command())


def _remove_from_startup():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY,
                            access=winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, _STARTUP_REG_NAME)
    except FileNotFoundError:
        pass


# ── Widget Core ───────────────────────────────────────────────────────────────

class ClaudeUsageWidget:
    def __init__(self):
        self._usage: dict | None = None
        self._error: str | None = None
        self._next_refresh_at: datetime | None = None
        self._popup = UsagePopup()

        # Import here so the script still imports even if pystray isn't installed yet
        try:
            import pystray
        except ImportError:
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

    # ── Event handlers ──

    def _open_popup(self):
        self._popup.show(
            self._usage, self._error, self._next_refresh_at,
            on_refresh=self._refresh_and_reopen,
        )

    def _refresh_and_reopen(self):
        self._do_refresh()
        self._popup.update(self._usage, self._error, self._next_refresh_at)

    def _on_click(self, icon, item=None):
        threading.Thread(target=self._open_popup, daemon=True).start()

    def _refresh_now(self, icon=None, item=None):
        def _do():
            self._popup.start_refresh_display()
            self._do_refresh()
            self._popup.update(self._usage, self._error, self._next_refresh_at)
        threading.Thread(target=_do, daemon=True).start()

    def _toggle_startup(self, icon, item):
        if _startup_registered():
            _remove_from_startup()
        else:
            _add_to_startup()

    def _quit(self, icon, item):
        icon.stop()
        os._exit(0)

    # ── Data fetching ──

    def _do_refresh(self):
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
            self._error = str(exc)
            self._icon.icon = make_tray_icon("error")
            self._icon.title = f"Claude Usage — error"
        finally:
            self._next_refresh_at = datetime.now() + timedelta(seconds=REFRESH_INTERVAL_SECONDS)

    def _refresh_loop(self, icon):
        icon.visible = True
        while True:
            self._do_refresh()
            time.sleep(REFRESH_INTERVAL_SECONDS)

    def run(self):
        self._icon.run(self._refresh_loop)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ClaudeUsageWidget().run()
