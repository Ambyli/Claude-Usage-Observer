"""
usage_popup.py
--------------
Always-on-top popup window anchored above the system tray that displays
token-usage data.  Built once; data is updated in-place via StringVars and
canvas redraws.

Public API
----------
UsagePopup(console_available)
    .show(usage, error, next_refresh_at, on_refresh)  — open / un-hide
    .update(usage, error, next_refresh_at)             — thread-safe data push
    .start_refresh_display()                           — show "Refreshing…"
    .apply_console(state)                              — push account-stats data
"""

import threading
import tkinter as tk
from datetime import date, datetime, timedelta

from logging_setup import log


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
        self._on_refresh        = None
        self._next_refresh_at: datetime | None = None
        self._refreshing: bool  = False

        # StringVars populated once the window is built
        self._vars: dict[str, tk.StringVar] = {}
        # Canvas widgets for bars (need direct redraw)
        self._d_bar:    tk.Canvas | None = None
        self._w_bar:    tk.Canvas | None = None
        self._cs_d_bar: tk.Canvas | None = None
        self._cs_w_bar: tk.Canvas | None = None
        # Dynamic content frame for per-project breakdown
        self._proj_content: tk.Frame | None = None
        # Account-stats section (only built when selenium is available)
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
            self._win.after(0, lambda: self._reshow(usage, error))
            log.debug("Finished UsagePopup.show (scheduled _reshow)")
            return

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
        self._refreshing      = False
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

        # ── Account stats via claude.ai (collapsible, starts collapsed) ──
        if self._console_available:
            self._cs_content = self._collapsible_section(
                win, "Account stats — claude.ai", initial_open=False
            )
            tk.Label(self._cs_content, textvariable=_sv("cs_status"),
                     font=("Segoe UI", 7), fg="#606070", bg=self.BG).pack(
                         anchor="w", padx=20, pady=(4, 0))
            # Daily
            tk.Label(self._cs_content, textvariable=_sv("cs_d_head"),
                     font=("Segoe UI", 8, "bold"), fg="#a0a0b0", bg=self.BG).pack(
                         anchor="w", padx=16, pady=(4, 0))
            tk.Label(self._cs_content, textvariable=_sv("cs_d_total"),
                     font=("Segoe UI", 13, "bold"), fg="#ffffff", bg=self.BG).pack(
                         anchor="w", padx=16, pady=(1, 0))
            self._cs_d_bar = self._make_bar(self._cs_content)
            tk.Label(self._cs_content, textvariable=_sv("cs_d_pct"),
                     font=("Segoe UI", 8), fg="#808090", bg=self.BG).pack(anchor="w", padx=20)
            # Weekly
            tk.Label(self._cs_content, textvariable=_sv("cs_w_head"),
                     font=("Segoe UI", 8, "bold"), fg="#a0a0b0", bg=self.BG).pack(
                         anchor="w", padx=16, pady=(6, 0))
            tk.Label(self._cs_content, textvariable=_sv("cs_w_total"),
                     font=("Segoe UI", 13, "bold"), fg="#ffffff", bg=self.BG).pack(
                         anchor="w", padx=16, pady=(1, 0))
            self._cs_w_bar = self._make_bar(self._cs_content)
            tk.Label(self._cs_content, textvariable=_sv("cs_w_pct"),
                     font=("Segoe UI", 8), fg="#808090", bg=self.BG).pack(anchor="w", padx=20)
            # Reset countdown
            tk.Label(self._cs_content, textvariable=_sv("cs_reset"),
                     font=("Segoe UI", 8), fg="#606070", bg=self.BG).pack(
                         anchor="w", padx=20, pady=(4, 0))
            tk.Frame(self._cs_content, height=6, bg=self.BG).pack()

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
                    pct     = data["total"] / d_total if d_total > 0 else 0
                    color   = "#e05050" if pct >= 0.90 else "#f0a030" if pct >= 0.70 else self.GREEN
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

    # ── Account-stats update (called from UsageFetcher thread) ───────────────

    def apply_console(self, state: dict):
        """Thread-safe: push account-stats fetch state into the UI."""
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

        def _clear():
            for k in ("cs_d_head","cs_d_total","cs_d_pct","cs_w_head","cs_w_total","cs_w_pct","cs_reset"):
                v[k].set("—")
            if self._cs_d_bar:
                self._draw_bar(self._cs_d_bar, 0, 0)
            if self._cs_w_bar:
                self._draw_bar(self._cs_w_bar, 0, 0)

        if status == "loading":
            v["cs_status"].set("Loading…")
            _clear()
        elif status == "waiting_login":
            v["cs_status"].set("Waiting for login — check Chrome window…")
            _clear()
        elif status == "error":
            short = (error[:60] + "…") if len(error) > 60 else error
            v["cs_status"].set(f"Error: {short}")
        elif status == "ok" and data:
            if fetched_at:
                age = int((datetime.now() - fetched_at).total_seconds() / 60)
                v["cs_status"].set("Just fetched" if age < 1 else f"Fetched {age} min ago")
            else:
                v["cs_status"].set("OK")

            today      = date.today()
            week_start = today - timedelta(days=today.weekday())

            d_total      = data.get("daily_total", 0)
            w_total      = data.get("weekly_total", 0)
            period_total = data.get("total", 0)
            limit        = period_total if period_total > 0 else 0

            v["cs_d_head"].set(f"Today  ({today.strftime('%A, %b %d')})")
            v["cs_d_total"].set(f"{d_total:,} tokens")
            if limit and d_total:
                pct = d_total / limit * 100
                v["cs_d_pct"].set(f"  {pct:.1f}% of period total")
                self._draw_bar(self._cs_d_bar, d_total, limit)
            else:
                v["cs_d_pct"].set("")
                self._draw_bar(self._cs_d_bar, 0, 0)

            v["cs_w_head"].set(f"This week  (since {week_start.strftime('%b %d')})")
            v["cs_w_total"].set(f"{w_total:,} tokens")
            if limit and w_total:
                pct = w_total / limit * 100
                v["cs_w_pct"].set(f"  {pct:.1f}% of period total")
                self._draw_bar(self._cs_w_bar, w_total, limit)
            else:
                v["cs_w_pct"].set("")
                self._draw_bar(self._cs_w_bar, 0, 0)

            pe = data.get("period_end")
            if pe:
                try:
                    pe_dt = datetime.fromisoformat(pe.replace("Z", "+00:00"))
                    secs  = int((pe_dt - datetime.now(pe_dt.tzinfo)).total_seconds())
                    h, rem = divmod(max(secs, 0), 3600)
                    m = rem // 60
                    if h >= 24:
                        d = h // 24
                        reset_str = f"Resets in {d}d {h % 24}h  ({pe_dt.strftime('%b %d')})"
                    elif h > 0:
                        reset_str = f"Resets in {h}h {m}m"
                    else:
                        reset_str = f"Resets in {m}m"
                    v["cs_reset"].set(f"  {reset_str}")
                except Exception as exc:
                    log.error("Error computing console reset countdown: %s", exc)
                    v["cs_reset"].set("")
            else:
                v["cs_reset"].set("")
        log.debug("Finished UsagePopup._apply_console")

    # ── Countdown tick ────────────────────────────────────────────────────────

    def _tick(self):
        log.debug("Starting UsagePopup._tick")
        if not (self._win and self._win.winfo_exists()):
            log.debug("Finished UsagePopup._tick (window gone)")
            return
        if self._refreshing:
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
                return
            m, s = divmod(secs, 60)
            self._vars["countdown"].set(f"Refreshes in {m}:{s:02d}")
        else:
            self._vars["countdown"].set("Refresh time unknown")
        self._win.after(1000, self._tick)
        log.debug("Finished UsagePopup._tick")

    def _do_bg_refresh(self):
        log.debug("Starting UsagePopup._do_bg_refresh")
        if self._on_refresh:
            self._on_refresh()
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
