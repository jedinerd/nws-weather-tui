#!/usr/bin/env python3
"""
NWS Weather TUI — Curses terminal and color initialization.
"""

from __future__ import annotations

import curses

from nws_weather_tui.radar.palette import (
    N_RADAR_COLORS,
    NWS_RADAR_PALETTE,
    radar_curses_color,
    radar_dual_pair,
    radar_single_pair,
)
from nws_weather_tui.helpers import dbg

# Custom color 30 / pair 16 are unused by the radar palette (which occupies
# colors 16-29 and pairs 20-229) — reserved here for a true 256-color orange.
WINDSOCK_ORANGE_COLOR = 30
WINDSOCK_ORANGE_PAIR = 16


def init_curses(stdscr) -> None:
    """Set up curses mode, UI color pairs 1-15, and mouse input."""
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    # UI color pairs 1-19
    curses.init_pair(1,  curses.COLOR_CYAN,    -1)  # header, info
    curses.init_pair(2,  curses.COLOR_YELLOW,  -1)  # highlights, forecast header
    curses.init_pair(3,  curses.COLOR_GREEN,   -1)  # OK, temperature
    curses.init_pair(4,  curses.COLOR_RED,     -1)  # errors, alerts
    curses.init_pair(5,  curses.COLOR_MAGENTA, -1)  # header right, PoP bar
    curses.init_pair(6,  curses.COLOR_BLUE,    -1)  # wind sparkline
    curses.init_pair(7,  curses.COLOR_YELLOW,  -1)  # temp sparkline
    curses.init_pair(8,  curses.COLOR_CYAN,    -1)  # radar default (ASCII)
    curses.init_pair(9,  curses.COLOR_GREEN,   -1)  # radar rain (ASCII)
    curses.init_pair(10, curses.COLOR_BLUE,    -1)  # radar snow (ASCII)
    curses.init_pair(11, curses.COLOR_MAGENTA, -1)  # radar sleet (ASCII)
    curses.init_pair(12, curses.COLOR_WHITE,   -1)  # city dot @
    curses.init_pair(13, curses.COLOR_YELLOW,  curses.COLOR_BLACK)  # radar anim indicator
    curses.init_pair(14, curses.COLOR_WHITE,   -1)  # general bold
    curses.init_pair(15, curses.COLOR_CYAN,    -1)  # radar view title
    try:
        mask = curses.ALL_MOUSE_EVENTS | getattr(curses, "REPORT_MOUSE_POSITION", 0)
        curses.mousemask(mask)
        curses.mouseinterval(200)
    except curses.error:
        pass


def init_radar_colors() -> bool:
    """Initialise NWS radar colors and all needed pairs for 256-color mode.

    Color IDs 16-29 are assigned to the 14 NWS radar palette entries.
    Pair  20-33: single-color radar cells (fg=NWS color, bg=default)
    Pair  34-229: dual-color radar halfblock cells (fg x bg, 14x14)

    Returns True if 256-color mode was successfully initialised.
    """
    try:
        if curses.COLORS < 256 or not curses.can_change_color():
            return False
        # Init custom colors
        for i, (_, _, _, r1k, g1k, b1k, *_rest) in enumerate(NWS_RADAR_PALETTE):
            curses.init_color(radar_curses_color(i + 1), r1k, g1k, b1k)
        curses.init_color(WINDSOCK_ORANGE_COLOR, 1000, 549, 0)
        curses.init_pair(WINDSOCK_ORANGE_PAIR, WINDSOCK_ORANGE_COLOR, -1)
        # Single-color pairs (fg=nws, bg=transparent)
        for nws_idx in range(1, N_RADAR_COLORS + 1):
            curses.init_pair(
                radar_single_pair(nws_idx),
                radar_curses_color(nws_idx),
                -1,
            )
        # Dual-color pairs (fg=nws_fg, bg=nws_bg) for halfblocks
        for fg in range(1, N_RADAR_COLORS + 1):
            for bg in range(1, N_RADAR_COLORS + 1):
                pair_num = radar_dual_pair(fg, bg)
                if pair_num < 256:
                    curses.init_pair(
                        pair_num,
                        radar_curses_color(fg),
                        radar_curses_color(bg),
                    )
        return True
    except curses.error as e:
        dbg(f"256-color radar init failed: {e}")
        return False
