#!/usr/bin/env python3
"""
NWS Weather TUI — Alerts view.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, List

import curses

from nws_weather_tui.formatting import fmt_time
from nws_weather_tui.geo import clamp
from nws_weather_tui.helpers import safe_addstr, wrap_lines
from nws_weather_tui.models import AlertItem

if TYPE_CHECKING:
    from nws_weather_tui.app import App


def alert_window(a: AlertItem, use_24h: bool) -> str:
    """'From Thu Oct 15, 6:00am until Fri Oct 16, 10:00pm · Sent ...' —
    the hazard's own window, not the message's `expires`, which usually
    comes well before the hazard ends."""
    parts = []
    now = dt.datetime.now(dt.timezone.utc)
    if a.onset is not None and a.onset > now:
        parts.append(f"From {fmt_time(a.onset, use_24h, True)}")
    if a.until is not None:
        parts.append(f"until {fmt_time(a.until, use_24h, True)}" if parts
                     else f"Until {fmt_time(a.until, use_24h, True)}")
    window = " ".join(parts)
    sent = f"Sent {fmt_time(a.sent, use_24h, True)}"
    return f"{window} · {sent}" if window else sent


def _build_alert_lines(app: "App", cols: int) -> List[tuple]:
    """Pre-render all alert text into (text, attr) tuples for line-based scrolling."""
    out: List[tuple] = []
    for idx, a in enumerate(app.alerts):
        title = f"{a.event}  [{a.severity}/{a.urgency}/{a.certainty}]"
        out.append((title[: cols - 1], curses.color_pair(4) | curses.A_BOLD))

        for wline in wrap_lines(alert_window(a, app.use_24h), cols - 1):
            out.append((wline, curses.A_DIM))

        for wline in wrap_lines(a.headline or "\u2014", cols - 1):
            out.append((wline[: cols - 1], curses.color_pair(2)))

        desc = (a.description or "").strip()
        if desc:
            for wline in wrap_lines(desc, cols - 2):
                out.append((" " + wline[: cols - 2], 0))

        instr = (a.instruction or "").strip()
        if instr:
            out.append(("", 0))
            out.append(("Instruction:", curses.A_BOLD))
            for wline in wrap_lines(instr, cols - 2):
                out.append((" " + wline[: cols - 2], curses.A_DIM))

        out.append(("\u2500" * (cols - 1), curses.A_DIM))

    return out


def _draw_no_alerts(app: "App", win, rows: int, cols: int) -> None:
    """All clear here — say when that was checked, and what's active
    elsewhere in the state."""
    safe_addstr(win, 0, 0, f"✓ No active alerts for {app.location_name}"[: cols - 1],
                curses.color_pair(3) | curses.A_BOLD)
    checked = fmt_time(dt.datetime.fromtimestamp(app.last_refresh), app.use_24h) \
        if app.last_refresh else "—"
    safe_addstr(win, 1, 0,
                f"Checked {checked} · refreshes every {app.auto_refresh_seconds // 60} min"
                [: cols - 1], curses.A_DIM)
    if not app.state_code:
        return
    y = 3
    others = app.area_alerts
    if not others:
        safe_addstr(win, y, 0, f"Nothing active anywhere in {app.state_code} either.",
                    curses.A_DIM)
        return
    head = f"Elsewhere in {app.state_code} · {len(others)} active"
    safe_addstr(win, y, 0, head, curses.color_pair(2) | curses.A_BOLD)
    y += 1
    event_w = min(34, max(len(a.event) for a in others))
    for a in others:
        if y >= rows:
            break
        until = f"until {fmt_time(a.until, app.use_24h, True)}" if a.until else ""
        attr = curses.color_pair(4) | curses.A_BOLD if a.severity in ("Extreme", "Severe") \
            else curses.color_pair(2)
        safe_addstr(win, y, 0, a.event[:event_w].ljust(event_w), attr)
        rest_w = max(0, cols - event_w - 3 - len(until) - 3)
        area = a.area_desc if len(a.area_desc) <= rest_w else a.area_desc[: rest_w - 1] + "…"
        safe_addstr(win, y, event_w + 2, area)
        if until and event_w + 2 + len(area) + 3 + len(until) < cols:
            safe_addstr(win, y, cols - 1 - len(until), until, curses.A_DIM)
        y += 1


def draw_alerts(app: "App", win) -> None:
    win.erase()
    rows, cols = win.getmaxyx()
    if not app.alerts:
        _draw_no_alerts(app, win, rows, cols)
        win.noutrefresh()
        return

    safe_addstr(
        win, 0, 0,
        f"Active Alerts: {len(app.alerts)}  (most severe/newest first)"[: cols - 1],
        curses.color_pair(4) | curses.A_BOLD,
    )

    all_lines = _build_alert_lines(app, cols)
    total = len(all_lines)
    view_rows = rows - 3  # header + footer

    app.alert_line_scroll = clamp(
        app.alert_line_scroll, 0, max(0, total - max(1, view_rows))
    )

    y = 2
    for text, attr in all_lines[app.alert_line_scroll:]:
        if y >= rows - 1:
            break
        safe_addstr(win, y, 0, text, attr)
        y += 1

    scroll_pos = app.alert_line_scroll + 1
    scroll_max = max(1, total - max(1, view_rows) + 1)
    safe_addstr(
        win, rows - 1, 0,
        f"Scroll: {scroll_pos}/{scroll_max} (j/k \u2191\u2193)"[: cols - 1],
        curses.A_DIM,
    )
    win.noutrefresh()
