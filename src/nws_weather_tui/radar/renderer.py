#!/usr/bin/env python3
"""
NWS Weather TUI — Radar curses rendering functions.
"""

from __future__ import annotations

import curses
from functools import lru_cache
from typing import Iterable, List, Tuple

from nws_weather_tui.helpers import safe_addstr
from nws_weather_tui.radar.decode import RadarCell
from nws_weather_tui.radar.palette import (
    NWS_RADAR_PALETTE,
    radar_dual_pair,
    radar_single_pair,
)


@lru_cache(maxsize=None)
def _cell_attr(fg_idx: int, bg_idx: int) -> int:
    # Pair numbers are fixed once curses is initialised; the same few hundred
    # (fg, bg) combinations come up for every cell of every frame.
    if fg_idx == 0 and bg_idx == 0:
        return curses.A_DIM
    if bg_idx == 0:
        return curses.color_pair(radar_single_pair(fg_idx))
    pair_num = radar_dual_pair(fg_idx, bg_idx)
    if pair_num < 256:
        return curses.color_pair(pair_num)
    return curses.color_pair(radar_single_pair(fg_idx))


def radar_cell_attr(char: str, fg_idx: int, bg_idx: int) -> int:
    """Return curses attr for a radar halfblock cell."""
    return _cell_attr(fg_idx, bg_idx)


def radar_char_attr_ascii(ch: str, ramp: str, kind: str = " ") -> int:
    """Return curses attr for ASCII-mode radar character."""
    if not ch or ch == " ":
        return curses.A_DIM
    levels = ramp or " .:-=+*#%@"
    idx = levels.find(ch)
    if idx < 0:
        idx = 1
    t = idx / max(1, len(levels) - 1)
    base_pair = {"R": 9, "S": 10, "I": 11}.get(kind, 8)
    base = curses.color_pair(base_pair)
    if t < 0.25:
        return base | curses.A_DIM
    if t < 0.8:
        return base
    return base | curses.A_BOLD


def _draw_runs(
    win, y: int, x0: int, cells: Iterable[Tuple[str, int]], transparent: bool = False
) -> None:
    """Draw (char, attr) cells left to right from x0, one addstr per run of
    equal attr rather than one per cell: a 200x50 radar panel is 10,000
    cells, redrawn several times a second. With `transparent`, spaces are
    skipped so whatever is under them (the radar) shows through."""
    run: List[str] = []
    run_attr = 0
    run_x = x0
    for i, (ch, attr) in enumerate(cells):
        skip = transparent and ch == " "
        if run and (skip or attr != run_attr):
            safe_addstr(win, y, run_x, "".join(run), run_attr)
            run = []
        if skip:
            continue
        if not run:
            run_x, run_attr = x0 + i, attr
        run.append(ch)
    if run:
        safe_addstr(win, y, run_x, "".join(run), run_attr)


def draw_radar_halfblock_line(
    win, y: int, row_cells: List[RadarCell], cols: int, x_off: int = 0
) -> None:
    max_w = max(0, cols - 1 - x_off)
    _draw_runs(
        win, y, max(0, x_off),
        ((ch, _cell_attr(fg_idx, bg_idx)) for ch, fg_idx, bg_idx in row_cells[:max_w]),
    )


def draw_radar_ascii_line(
    win,
    y: int,
    line: str,
    kind_line: str,
    cols: int,
    ramp: str,
    x_off: int = 0,
) -> None:
    max_w = max(0, cols - 1 - x_off)
    line = (line or "")[:max_w]
    kind_line = (kind_line or "").ljust(len(line))[:max_w]
    if not line:
        return
    _draw_runs(
        win, y, max(0, x_off),
        ((ch, radar_char_attr_ascii(ch, ramp, kind_line[i])) for i, ch in enumerate(line)),
    )


def draw_state_overlay_line(
    win, y: int, line: str, cols: int, x_off: int = 0
) -> None:
    line = (line or "")[: max(0, cols - 1 - x_off)]
    attr = curses.color_pair(2) | curses.A_DIM
    _draw_runs(win, y, max(0, x_off), ((ch, attr) for ch in line), transparent=True)


def draw_alert_overlay_line(
    win, y: int, line: str, cols: int, x_off: int = 0
) -> None:
    """Draw NWS alert (warning/watch) polygon outlines in bold red."""
    line = (line or "")[: max(0, cols - 1 - x_off)]
    attr = curses.color_pair(4) | curses.A_BOLD
    _draw_runs(win, y, max(0, x_off), ((ch, attr) for ch in line), transparent=True)


def draw_city_overlay_line(
    win, y: int, line: str, cols: int, x_off: int = 0
) -> None:
    line = (line or "")[: max(0, cols - 1 - x_off)]
    special = {
        "O": curses.color_pair(1) | curses.A_BOLD,
        "◉": curses.color_pair(4) | curses.A_BOLD,
        "@": curses.color_pair(12) | curses.A_BOLD,
    }
    label_attr = curses.color_pair(3) | curses.A_BOLD
    _draw_runs(
        win, y, max(0, x_off),
        ((ch, special.get(ch, label_attr)) for ch in line), transparent=True,
    )

def draw_radar_legend(
    win, y: int, x: int, cols: int, has_256color: bool
) -> None:
    """Draw a one-line dBZ color legend."""
    label = "dBZ:"
    safe_addstr(win, y, x, label, curses.A_DIM)
    cx = x + len(label) + 1
    for i, (_, _, _, _, _, _, dbz, lbl) in enumerate(NWS_RADAR_PALETTE):
        nws_idx = i + 1
        if cx + 3 > cols - 1:
            break
        if has_256color:
            pair_num = radar_dual_pair(nws_idx, nws_idx)
            if pair_num < 256:
                safe_addstr(win, y, cx, "\u2580", curses.color_pair(pair_num))
                cx += 1
        else:
            safe_addstr(win, y, cx, "\u2588", curses.color_pair(
                {1: 8, 2: 8, 3: 8, 4: 9, 5: 9, 6: 9,
                 7: 2, 8: 2, 9: 2, 10: 4, 11: 4, 12: 4,
                 13: 5, 14: 5}.get(nws_idx, 8)
            ))
            cx += 1
        if cx + len(lbl) + 1 < cols - 1:
            safe_addstr(win, y, cx, lbl, curses.A_DIM)
            cx += len(lbl) + 1
