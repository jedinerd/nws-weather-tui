#!/usr/bin/env python3
"""
NWS Weather TUI — Current conditions view.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import List, Optional, Tuple, TYPE_CHECKING

import curses

from nws_weather_tui.conversions import c_to_f, dewpoint_c, m_to_mi, mps_to_mph, pa_to_inhg
from nws_weather_tui.curses_init import WINDSOCK_ORANGE_PAIR
from nws_weather_tui.formatting import fmt_num, fmt_time
from nws_weather_tui.geo import clamp
from nws_weather_tui.helpers import safe_addstr, wrap_lines
from nws_weather_tui.icons import ICON_BIG
from nws_weather_tui.models import uv_category
from nws_weather_tui.views.radar import draw_radar_panel
from nws_weather_tui.views.style import temp_attr
from nws_weather_tui.windsock import WINDSOCK_MAX_W, windsock_lines

if TYPE_CHECKING:
    from nws_weather_tui.app import App

SIDE_GAP = 5            # space either side of the divider before the radar
SIDE_MIN_RADAR_W = 100  # radar width needed before it moves beside the text

_CARDINAL = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _deg_to_cardinal(deg: float) -> str:
    idx = int((deg + 11.25) / 22.5) % 16
    return _CARDINAL[idx]


def _aqi_attr(aqi: int) -> int:
    if aqi <= 50:
        return curses.color_pair(3)               # Good — green
    if aqi <= 100:
        return curses.color_pair(2)                # Moderate — yellow
    if aqi <= 150:
        return curses.color_pair(2) | curses.A_BOLD  # Unhealthy for Sensitive Groups
    if aqi <= 200:
        return curses.color_pair(4)                # Unhealthy — red
    if aqi <= 300:
        return curses.color_pair(5)                # Very Unhealthy — magenta
    return curses.color_pair(4) | curses.A_BOLD    # Hazardous


def _uv_attr(uv: float) -> int:
    if uv < 3:
        return curses.color_pair(3)                # Low — green
    if uv < 6:
        return curses.color_pair(2)                # Moderate — yellow
    if uv < 8:
        return curses.color_pair(2) | curses.A_BOLD  # High
    if uv < 11:
        return curses.color_pair(4)                # Very High — red
    return curses.color_pair(4) | curses.A_BOLD    # Extreme


# 3-row block digits for the headline temperature.
_BIG_GLYPHS = {
    "0": ("█▀█", "█ █", "▀▀▀"),
    "1": ("▀█ ", " █ ", "▀▀▀"),
    "2": ("▀▀█", "█▀▀", "▀▀▀"),
    "3": ("▀▀█", " ▀█", "▀▀▀"),
    "4": ("█ █", "▀▀█", "  ▀"),
    "5": ("█▀▀", "▀▀█", "▀▀▀"),
    "6": ("█▀▀", "█▀█", "▀▀▀"),
    "7": ("▀▀█", "  █", "  ▀"),
    "8": ("█▀█", "█▀█", "▀▀▀"),
    "9": ("█▀█", "▀▀█", "▀▀▀"),
    "-": ("   ", "▀▀▀", "   "),
    "°": ("█▀█", "▀▀▀", "   "),
    "F": ("█▀▀", "█▀ ", "▀  "),
    "C": ("█▀▀", "█  ", "▀▀▀"),
    "?": ("▀▀█", " █▀", " ▀ "),
}


def _big_text(text: str) -> List[str]:
    rows = ["", "", ""]
    for ch in text:
        glyph = _BIG_GLYPHS.get(ch, _BIG_GLYPHS["?"])
        for i in range(3):
            rows[i] += glyph[i] + " "
    return [r.rstrip() for r in rows]


def _draw_windsock(win, lines: List[str], x0: int, has_256color: bool) -> None:
    """Draw windsock lines with a gray pole and orange/white sock stripes.

    Stripe color alternates every 2 glyphs along the sock body, counted in
    drawing order (so it stripes correctly whether the sock is drooping
    near-vertical or extended near-horizontal), not by screen column.
    """
    gray_attr = curses.color_pair(14) | curses.A_DIM
    white_attr = curses.color_pair(14)
    orange_attr = (
        curses.color_pair(WINDSOCK_ORANGE_PAIR) if has_256color else curses.color_pair(2)
    )
    stripe_w = 2

    counter = 0
    for row, line in enumerate(lines):
        run_start: Optional[int] = None
        run_attr = None

        def flush(end_col: int) -> None:
            nonlocal run_start
            if run_start is not None:
                safe_addstr(win, row, x0 + run_start, line[run_start:end_col], run_attr)
                run_start = None

        for col, ch in enumerate(line):
            if ch == " ":
                flush(col)
                continue
            if col <= 1:
                attr = gray_attr
            else:
                attr = white_attr if (counter // stripe_w) % 2 == 0 else orange_attr
                counter += 1
            if attr != run_attr:
                flush(col)
                run_start = col
                run_attr = attr
        flush(len(line))


def _conditions(app: "App") -> Tuple[str, str]:
    """(description, icon key). Some stations (e.g. RAWS/mesonet sites) send
    no sky condition at all; then say what the forecast has for this hour
    rather than show "N/A" under a question mark."""
    c = app.current
    text = c.text_description.replace("Current Conditions: ", "").strip()
    if text and text != "N/A":
        return text, c.icon_key
    now = dt.datetime.now(dt.timezone.utc)
    for h in app.hourly_periods:
        if h.start is not None and h.start <= now < h.start + dt.timedelta(hours=1):
            return f"{h.short_forecast} (forecast)", h.icon_key
    return "Not reported", c.icon_key


def draw_current(app: "App", win) -> None:
    win.erase()
    rows, cols = win.getmaxyx()
    win_y0, win_x0 = win.getbegyx()

    if not app.current:
        safe_addstr(
            win, 0, 0,
            "No current conditions yet. Press r to refresh.",
            curses.color_pair(4),
        )
        win.noutrefresh()
        return

    c = app.current
    description, icon_key = _conditions(app)
    icon_lines = ICON_BIG.get(icon_key, ICON_BIG["unknown"]).strip("\n").splitlines()
    icon_w = max((len(x) for x in icon_lines), default=0)
    left_w = clamp(icon_w + 2, 12, cols - 1)

    dp_val = dewpoint_c(c.temperature_c, c.humidity_pct)
    feels_like_c = c.heat_index_c if c.heat_index_c is not None else c.wind_chill_c
    show_feels_like = (
        feels_like_c is not None
        and c.temperature_c is not None
        and abs(feels_like_c - c.temperature_c) >= 1.0
    )
    temp_val = c_to_f(c.temperature_c) if app.units == "us" else c.temperature_c
    big_temp = _big_text(
        "?" if temp_val is None
        else f"{fmt_num(temp_val, 0)}°{'F' if app.units == 'us' else 'C'}"
    )
    if app.units == "us":
        dp_str = f"{fmt_num(c_to_f(dp_val), 1)} \u00b0F" if dp_val is not None else "\u2014"
        feels_str = f"{fmt_num(c_to_f(feels_like_c), 1)} \u00b0F"
        wind_str = "—" if c.wind_mps is None else f"{fmt_num(mps_to_mph(c.wind_mps), 1)} mph"
        gust_str = (
            f"{fmt_num(mps_to_mph(c.gust_mps), 1)} mph"
            if c.gust_mps is not None else "\u2014"
        )
        press_str = (
            f"{fmt_num(pa_to_inhg(c.pressure_pa), 2)} inHg"
            if c.pressure_pa is not None else "\u2014"
        )
        vis_str = (
            f"{fmt_num(m_to_mi(c.visibility_m), 1)} mi"
            if c.visibility_m is not None else "\u2014"
        )
    else:
        dp_str = f"{fmt_num(dp_val, 1)} \u00b0C" if dp_val is not None else "\u2014"
        feels_str = f"{fmt_num(feels_like_c, 1)} \u00b0C"
        wind_str = "—" if c.wind_mps is None else f"{fmt_num(c.wind_mps, 1)} m/s"
        gust_str = f"{fmt_num(c.gust_mps, 1)} m/s" if c.gust_mps is not None else "\u2014"
        press_str = (
            f"{fmt_num((c.pressure_pa or 0) / 100.0, 1)} hPa"
            if c.pressure_pa is not None else "\u2014"
        )
        vis_str = (
            f"{fmt_num((c.visibility_m or 0) / 1000.0, 1)} km"
            if c.visibility_m is not None else "\u2014"
        )

    wind_dir = (
        f"{_deg_to_cardinal(c.wind_dir_deg)} ({fmt_num(c.wind_dir_deg, 0)}\u00b0)"
        if c.wind_dir_deg is not None else "\u2014"
    )
    # Stations report calm as 0 speed with a meaningless 0° (north) direction.
    if c.wind_mps is not None and c.wind_mps < 0.5:
        wind_str, wind_dir = "calm", "\u2014"
    hum_str = f"{fmt_num(c.humidity_pct, 0)}%" if c.humidity_pct is not None else "\u2014"

    # --- Build the text block first, so the icon can be vertically centered against it ---
    big_attr = temp_attr(temp_val, app.units == "us") | curses.A_BOLD
    text_lines: List[Tuple[str, int]] = [(line, big_attr) for line in big_temp]
    text_lines += [
        ("", 0),
        (description, curses.A_BOLD),
    ]
    if show_feels_like:
        label = "Heat Index" if c.heat_index_c is not None else "Wind Chill"
        text_lines.append((f"{label}: {feels_str}", curses.color_pair(2) | curses.A_BOLD))
    text_lines.append((f"Dew Point: {dp_str}", curses.color_pair(2)))
    text_lines += [
        (f"Wind: {wind_str}  Gust: {gust_str}  Dir: {wind_dir}", 0),
        (f"Humidity: {hum_str}   Pressure: {press_str}   Visibility: {vis_str}", 0),
    ]
    if app.air_quality and app.air_quality.aqi is not None:
        aq = app.air_quality
        pollutant = f"  ({aq.primary_pollutant})" if aq.primary_pollutant else ""
        text_lines.append(
            (f"Air Quality: {aq.aqi} \u2014 {aq.category}{pollutant}", _aqi_attr(aq.aqi))
        )
    if app.uv_index and app.uv_index.current is not None:
        uv = app.uv_index.current
        text_lines.append(
            (f"UV Index: {fmt_num(uv, 1)} \u2014 {uv_category(uv)}", _uv_attr(uv))
        )
    text_lines.append((
        f"Station: {c.station}   Observed: {fmt_time(c.timestamp, app.use_24h, with_date=True)}",
        curses.A_DIM,
    ))
    if app.alerts:
        top = app.alerts[0]
        text_lines.append((
            f"Active Alerts: {len(app.alerts)}  (press 'a')",
            curses.color_pair(4) | curses.A_BOLD,
        ))
        # Event and end time only: the full headline is long enough to widen
        # the whole layout block and push the radar off to the side.
        line = f"  {top.event}"
        if top.until is not None:
            line += f" until {fmt_time(top.until, app.use_24h, True)}"
        if len(app.alerts) > 1:
            line += f" \u00b7 +{len(app.alerts) - 1} more"
        text_lines.append((line, curses.color_pair(4)))
    else:
        text_lines.append(("Active Alerts: 0", curses.A_DIM))

    # --- Horizontally center the icon + text + windsock block as a whole ---
    text_max_w = max((len(t) for t, _ in text_lines), default=0)
    sock_lines = windsock_lines(mps_to_mph(c.wind_mps), time.time())
    # Lay out with the sock's fixed maximum width, not this frame's actual
    # width, so the icon/text to its left don't shift as it droops/flutters.
    sock_w = WINDSOCK_MAX_W
    gap = 3

    block_w_with_sock = left_w + text_max_w + gap + sock_w
    show_sock = sock_w > 0 and block_w_with_sock <= cols - 1
    block_w = block_w_with_sock if show_sock else (left_w + text_max_w)
    # Wide terminals: conditions on the left, radar beside them at full
    # height, instead of a short radar strip under the text.
    side_by_side = app.show_radar_map and cols - block_w - SIDE_GAP >= SIDE_MIN_RADAR_W
    margin = 2 if side_by_side else max(0, (cols - block_w) // 2)

    icon_x0 = margin
    x0 = margin + left_w
    sock_x = x0 + text_max_w + gap

    # --- Icon, vertically centered against the text block ---
    icon_h = min(len(icon_lines), rows)
    text_h = min(len(text_lines), rows)
    icon_y0 = max(0, (text_h - icon_h) // 2)
    for i, line in enumerate(icon_lines[: rows - icon_y0]):
        safe_addstr(win, icon_y0 + i, icon_x0, line[:left_w], curses.color_pair(2))

    # --- Text block ---
    y = 0
    for text, attr in text_lines:
        safe_addstr(win, y, x0, text[: cols - x0 - 1], attr)
        y += 1

    # --- Windsock, top-aligned to the right of the text block ---
    if show_sock:
        _draw_windsock(win, sock_lines[:rows], sock_x, app._radar_has_256color)

    # --- Side-by-side: the NWS narrative for the next two periods fills
    # the space under the conditions ---
    if side_by_side:
        y = max(y, len(icon_lines)) + 1
        text_w = max(20, block_w - 2)
        for period in app.forecast_periods[:2]:
            if y >= rows - 2 or not period.detailed_forecast:
                break
            safe_addstr(win, y, margin, period.name, curses.color_pair(1) | curses.A_BOLD)
            y += 1
            for line in wrap_lines(period.detailed_forecast, text_w):
                if y >= rows:
                    break
                safe_addstr(win, y, margin, line)
                y += 1
            y += 1

    # --- Radar panel ---
    if side_by_side:
        rx = margin + block_w + SIDE_GAP
        for ry in range(rows):
            safe_addstr(win, ry, rx - SIDE_GAP // 2 - 1, "│", curses.A_DIM)
        try:
            panel = win.derwin(rows, cols - rx, 0, rx)
        except curses.error:
            panel = None
        if panel is not None:
            draw_radar_panel(app, panel, 0, cols - rx, rows, full_screen=True)
    elif app.show_radar_map and y + 4 < rows:
        draw_radar_panel(app, win, y, cols, rows, full_screen=False)

    win.noutrefresh()
