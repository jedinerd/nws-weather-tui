#!/usr/bin/env python3
"""
NWS Weather TUI — Help screen view.

Sections are packed into as many columns as the terminal fits (each
section kept whole, placed in the shortest column), and scroll together
when they don't all fit.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple, TYPE_CHECKING

import curses

from nws_weather_tui import __version__
from nws_weather_tui.constants import CONFIG_PATH, STATE_PATH
from nws_weather_tui.geo import clamp
from nws_weather_tui.helpers import safe_addstr, wrap_lines
from nws_weather_tui.radar.renderer import draw_radar_legend

if TYPE_CHECKING:
    from nws_weather_tui.app import App

COL_MIN_W = 58
COL_GAP = 4
KEY_W = 9


def _tilde(path: str) -> str:
    """Show paths under $HOME as ~/..., which is shorter and wraps less."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


# Row kinds: ("title", text) | ("key", key, text) | ("text", text) | ("legend",) | ("blank",)
Row = Tuple[str, ...]

SECTIONS: List[Tuple[str, List[Tuple[Optional[str], str]]]] = [
    ("Views", [
        ("c", "Current conditions, radar and today's forecast"),
        ("f", "Forecast — a column per day; ←/→ scroll when they don't all fit"),
        ("h", "Hourly — highlights, condition ribbon, a column per day on wide screens"),
        ("a", "Active alerts for this location"),
        ("m", "Moon phase, rise/set and upcoming phases"),
        ("d", "Area Forecast Discussion — forecaster narrative in columns"),
        ("H", "Hazardous Weather Outlook — the office's 7-day heads-up"),
        ("D", "Favorites dashboard — every favorite at a glance"),
        ("?", "This help"),
    ]),
    ("Navigation", [
        ("j / k", "Scroll down / up (turns the page on Discussion)"),
        ("↓ / ↑", "Same as j / k"),
        ("PgDn/PgUp", "Scroll by 10 lines"),
        ("G", "Jump to the end"),
        ("Esc", "Back to current conditions"),
    ]),
    ("Actions", [
        ("l", "Search location (city/state, ZIP, or lat,lon)"),
        ("r", "Refresh now"),
        ("u", "Toggle US / SI units"),
        ("t", "Toggle 12h / 24h clock"),
        ("p", "Pause / resume auto-refresh"),
        ("F", "Save / remove the current location as a favorite"),
        ("n / b", "Next / previous favorite"),
        ("e", "Open the favorites editor"),
        ("q", "Quit"),
    ]),
    ("Favorites editor (e)", [
        ("j / k", "Move the cursor"),
        ("J / K", "Move the selected favorite up / down"),
        ("Enter", "Jump to the selected favorite"),
        ("a", "Add a favorite (search by city/ZIP)"),
        ("r", "Rename the selected favorite"),
        ("d", "Delete the selected favorite"),
        ("e / Esc", "Leave the editor (q also works)"),
    ]),
    ("Favorites dashboard (D)", [
        ("j / k", "Move the cursor"),
        ("Enter", "Jump to the selected favorite"),
        ("r", "Refresh every favorite"),
        ("D / Esc", "Back to current conditions"),
    ]),
    ("Radar (current view)", [
        ("A", "Play / pause the animation"),
        ("< / >", "Step back / forward one frame"),
        ("o", "Open weather.gov radar in a browser"),
        ("click", "Click a city marker to jump there"),
        (None, "◉ you marks the current location."),
    ]),
    ("Radar colors", [
        (None, "256-color terminals: half-block cells on the NWS dBZ scale."),
        (None, 'Otherwise an ASCII ramp " .:-=+*#%@", colored by precip type.'),
        (None, "\x00legend"),
        (None, "Sources, tried in order: NOAA MRMS composite, Iowa State IEM "
               "NEXRAD, then the NWS station WMS."),
    ]),
    ("About", [
        ("version", __version__),
        ("config", _tilde(CONFIG_PATH)),
        ("state", _tilde(STATE_PATH)),
        (None, "Requires astral, numpy, pillow and requests."),
    ]),
]


def _section_rows(title: str, entries: List[Tuple[Optional[str], str]], w: int) -> List[Row]:
    rows: List[Row] = [("title", title)]
    for key, text in entries:
        if text == "\x00legend":
            rows.append(("legend",))
        elif key is None:
            rows += [("text", line) for line in wrap_lines(text, w)]
        else:
            for i, line in enumerate(wrap_lines(text, max(10, w - KEY_W - 1))):
                rows.append(("key", key if i == 0 else "", line))
    return rows


def draw_help(app: "App", win) -> None:
    win.erase()
    rows, cols = win.getmaxyx()

    n = clamp((cols - 1 + COL_GAP) // (COL_MIN_W + COL_GAP), 1, 4)
    w = (cols - 1 - COL_GAP * (n - 1)) // n

    # Masonry: each section, whole, into the currently shortest column.
    columns: List[List[Row]] = [[] for _ in range(n)]
    for title, entries in SECTIONS:
        block = _section_rows(title, entries, w)
        col = min(columns, key=len)
        if col:
            col.append(("blank",))
        col.extend(block)

    height = max(len(c) for c in columns)
    view_rows = rows - 1 if height > rows else rows
    app.help_scroll = clamp(app.help_scroll, 0, max(0, height - view_rows))

    for k, col in enumerate(columns):
        x = k * (w + COL_GAP)
        if k:
            for y in range(min(view_rows, height)):
                safe_addstr(win, y, x - COL_GAP // 2 - 1, "│", curses.A_DIM)
        for y, row in enumerate(col[app.help_scroll:app.help_scroll + view_rows]):
            kind = row[0]
            if kind == "title":
                safe_addstr(win, y, x, row[1], curses.color_pair(2) | curses.A_BOLD)
                if len(row[1]) + 2 <= w:
                    safe_addstr(win, y, x + len(row[1]) + 1, "─" * (w - len(row[1]) - 1),
                                curses.A_DIM)
            elif kind == "key":
                safe_addstr(win, y, x, row[1][:KEY_W], curses.color_pair(1) | curses.A_BOLD)
                safe_addstr(win, y, x + KEY_W + 1, row[2][: w - KEY_W - 1])
            elif kind == "text":
                safe_addstr(win, y, x, row[1][:w], curses.A_DIM)
            elif kind == "legend":
                draw_radar_legend(win, y, x, x + w, app.radar.has_256color)

    if height > rows:
        shown_to = min(height, app.help_scroll + view_rows)
        hint = f"lines {app.help_scroll + 1}–{shown_to} of {height} · j/k ↑↓ scroll"
        safe_addstr(win, rows - 1, 0, hint[: cols - 1], curses.A_DIM)

    win.noutrefresh()
