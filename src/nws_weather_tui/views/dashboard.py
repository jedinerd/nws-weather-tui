#!/usr/bin/env python3
"""
NWS Weather TUI — Multi-location favorites dashboard view.

One row per favorite: current conditions plus today's high/low, chance of
precipitation and any active alerts. Columns size themselves to the
terminal so names and conditions aren't cut short on wide screens.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import curses

from nws_weather_tui.conversions import c_to_f, mps_to_mph
from nws_weather_tui.favorites import is_here
from nws_weather_tui.formatting import fmt_num
from nws_weather_tui.geo import clamp
from nws_weather_tui.helpers import fit_width, safe_addstr, text_width
from nws_weather_tui.icons import ICON_TINY

if TYPE_CHECKING:
    from nws_weather_tui.app import App

def _today(periods: List[Any]) -> Tuple[str, Optional[float]]:
    """('68°/48°', peak precip %) from the first day + night periods."""
    first = periods[:2]
    hi = next((p.temperature for p in first if p.is_daytime), None)
    lo = next((p.temperature for p in first if p.is_daytime is False), None)
    temps = "/".join("—" if t is None else f"{t:.0f}°" for t in (hi, lo))
    pops = [p.pop for p in first if p.pop is not None]
    return temps, (max(pops) if pops else None)


def _row(app: "App", data: Optional[Dict[str, Any]]) -> Dict[str, Tuple[str, int]]:
    """Cell text + attr for one favorite, keyed by column."""
    if data is None:
        return {"temp": ("loading…", curses.A_DIM)}
    if data.get("error"):
        return {"temp": ("—", curses.A_DIM), "cond": ("(unavailable)", curses.A_DIM)}
    c = data["current"]
    us = app.units == "us"
    temp = c_to_f(c.temperature_c) if us else c.temperature_c
    wind = mps_to_mph(c.wind_mps) if us else c.wind_mps
    periods = data.get("periods") or []
    cond = (c.text_description or "").replace("Current Conditions: ", "").strip()
    icon_key = c.icon_key
    if (not cond or cond == "N/A") and periods:
        # No sky condition from this station: use the forecast, and say so.
        cond, icon_key = f"{periods[0].short_forecast} (fcst)", periods[0].icon_key
    if c.wind_mps is None:
        wind_s = "—"  # missing, which is not the same as calm
    elif c.wind_mps < 0.5:
        wind_s = "calm"
    else:
        wind_s = f"{fmt_num(wind, 0)} {'mph' if us else 'm/s'}"
    cells: Dict[str, Tuple[str, int]] = {
        "temp": ("—" if temp is None else f"{fmt_num(temp, 0)}°{'F' if us else 'C'}", 0),
        "icon": (ICON_TINY.get(icon_key, "?") or "?", 0),
        "cond": (cond or "—", 0),
        "wind": (wind_s, 0),
    }
    if periods:
        hilo, pop = _today(periods)
        cells["today"] = (hilo, 0)
        if pop:
            attr = curses.color_pair(6) | (curses.A_BOLD if pop >= 60 else 0)
            cells["pop"] = (f"{pop:.0f}%", attr if pop >= 30 else curses.A_DIM)
    alerts = data.get("alerts") or []
    if alerts:
        more = f" +{len(alerts) - 1}" if len(alerts) > 1 else ""
        cells["alerts"] = (f"⚠ {alerts[0].event}{more}", curses.color_pair(4) | curses.A_BOLD)
    else:
        cells["alerts"] = ("none", curses.A_DIM)
    age_s = time.time() - data.get("ts", time.time())
    cells["updated"] = (f"{int(age_s // 60)}m ago" if age_s >= 60 else "just now", curses.A_DIM)
    return cells


def draw_dashboard(app: "App", win) -> None:
    win.erase()
    rows, cols = win.getmaxyx()

    from nws_weather_tui.dashboard import refresh_dashboard
    refresh_dashboard(app)

    if not app.favorites:
        safe_addstr(win, 1, 0, "No favorites yet.", curses.A_BOLD)
        safe_addstr(win, 2, 0, "Press F from any view to save the current location.", curses.A_DIM)
        win.noutrefresh()
        return

    app.dash_idx = clamp(app.dash_idx, 0, len(app.favorites) - 1)
    cells = [_row(app, app._dash_data.get(i)) for i in range(len(app.favorites))]
    names = [str(f.get("name", "—")) for f in app.favorites]

    def widest(key: str, head: str) -> int:
        return max([text_width(head)] + [text_width(c.get(key, ("", 0))[0]) for c in cells])

    # (key, header, width, align). Name, condition and alerts flex to fit.
    columns: List[Tuple[str, str, int, str]] = [
        ("name", "Location", max(text_width("Location"), *(text_width(n) for n in names)), "<"),
        ("temp", "Now", widest("temp", "Now"), ">"),
        ("icon", "", 2, "<"),
        ("cond", "Conditions", widest("cond", "Conditions"), "<"),
        ("wind", "Wind", widest("wind", "Wind"), ">"),
        ("today", "Today", widest("today", "Today"), ">"),
        ("pop", "Precip", widest("pop", "Precip"), ">"),
        ("alerts", "Alerts", widest("alerts", "Alerts"), "<"),
        ("updated", "Updated", widest("updated", "Updated"), "<"),
    ]
    gap = 3
    avail = cols - 3 - gap * (len(columns) - 1)  # 3 = marker column
    # Shrink the flexible columns (alerts, then condition, then name) until it fits.
    for key, floor in (("alerts", 8), ("cond", 12), ("name", 14)):
        over = sum(c[2] for c in columns) - avail
        if over <= 0:
            break
        columns = [
            (k, h, max(floor, w - over) if k == key else w, a) for k, h, w, a in columns
        ]
    # Still too wide: drop the least useful columns rather than run off the
    # edge (curses would wrap the overflow onto the next row).
    for key in ("updated", "wind", "pop", "today"):
        if sum(c[2] for c in columns) + gap * (len(columns) - 1) <= cols - 3:
            break
        columns = [c for c in columns if c[0] != key]

    x = 3
    xs: Dict[str, int] = {}
    fitted: List[Tuple[str, str, int, str]] = []
    for key, head, w, align in columns:
        w = min(w, cols - 1 - x)  # never past the right edge
        if w < 1:
            break
        xs[key] = x
        fitted.append((key, head, w, align))
        safe_addstr(win, 1, x, fit_width(head, w, align), curses.A_BOLD)
        x += w + gap
    columns = fitted
    safe_addstr(win, 2, 0, "─" * min(cols - 1, x - gap), curses.A_DIM)

    any_here = False
    for i, fav in enumerate(app.favorites):
        y = 3 + i
        if y >= rows - 2:
            break
        selected = i == app.dash_idx
        here = is_here(app, fav)
        any_here = any_here or here
        safe_addstr(win, y, 0, "▸" if selected else " ", curses.color_pair(2) | curses.A_BOLD)
        safe_addstr(win, y, 1, "*" if here else " ", curses.color_pair(1) | curses.A_BOLD)
        row = dict(cells[i])
        row["name"] = (names[i], curses.color_pair(2) | curses.A_BOLD if selected else 0)
        for key, _, w, align in columns:
            text, attr = row.get(key, ("", 0))
            if text:
                safe_addstr(win, y, xs[key], fit_width(text, w, align), attr)

    hints = "j/k move · Enter jump · r refresh · D/Esc back"
    if any_here:
        hints += "    * = current location"
    safe_addstr(win, rows - 1, 0, hints[: cols - 1], curses.A_DIM)
    win.noutrefresh()
