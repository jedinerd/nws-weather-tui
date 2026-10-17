#!/usr/bin/env python3
"""
NWS Weather TUI — Favorites editor view.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import curses

from nws_weather_tui.favorites import is_here
from nws_weather_tui.helpers import fit_width, safe_addstr, text_width

if TYPE_CHECKING:
    from nws_weather_tui.app import App


def draw_favorites(app: "App", win) -> None:
    win.erase()
    rows, cols = win.getmaxyx()

    if not app.favorites:
        safe_addstr(win, 1, 0, "No favorites yet.", curses.A_BOLD)
        safe_addstr(win, 2, 0, "Press a to add one, or F from any view.", curses.A_DIM)
    else:
        names = [str(f.get("name", "—")) for f in app.favorites]
        # Name column as wide as the longest name, leaving room for lat/lon.
        name_w = max(text_width("Name"), *(text_width(n) for n in names))
        name_w = max(12, min(name_w, cols - 1 - 7 - 22))
        safe_addstr(win, 1, 2, f"{'#':>3}  {fit_width('Name', name_w)}  {'Lat':>9}  {'Lon':>10}",
                    curses.A_BOLD)
        safe_addstr(win, 2, 0, "─" * min(cols - 1, 7 + name_w + 23), curses.A_DIM)

        for i, fav in enumerate(app.favorites):
            y = 3 + i
            if y >= rows - 2:
                break
            selected = i == app.fav_edit_idx
            try:
                coords = f"{float(fav.get('lat', 0)):>9.4f}  {float(fav.get('lon', 0)):>10.4f}"
            except (TypeError, ValueError):
                coords = f"{'—':>9}  {'—':>10}"
            safe_addstr(win, y, 0, "▸" if selected else " ", curses.color_pair(2) | curses.A_BOLD)
            if is_here(app, fav):
                safe_addstr(win, y, 1, "*", curses.color_pair(1) | curses.A_BOLD)
            line = f"{i + 1:>3}  {fit_width(names[i], name_w)}  {coords}"
            attr = curses.color_pair(2) | curses.A_BOLD if selected else 0
            safe_addstr(win, y, 2, line, attr)

    # Wrap the key hints at their separators rather than cut one in half.
    hints = ["j/k move", "J/K reorder", "Enter jump", "a add", "r rename", "d delete",
             "e/Esc exit", "* = current location"]
    lines = [""]
    for hint in hints:
        joined = f"{lines[-1]} · {hint}" if lines[-1] else hint
        if len(joined) <= cols - 1 or not lines[-1]:
            lines[-1] = joined
        else:
            lines.append(hint)
    for i, line in enumerate(lines[-2:]):
        safe_addstr(win, rows - min(2, len(lines)) + i, 0, line[: cols - 1], curses.A_DIM)
    win.noutrefresh()
