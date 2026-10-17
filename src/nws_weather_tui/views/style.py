#!/usr/bin/env python3
"""
NWS Weather TUI — Colour conventions shared by the views.
"""

from __future__ import annotations

from typing import Optional

import curses

from nws_weather_tui.conversions import c_to_f

# Colour pairs (see curses_init.py).
C_CYAN = 1
C_YELLOW = 2
C_GREEN = 3
C_RED = 4
C_MAGENTA = 5
C_BLUE = 6


def temp_attr(v: Optional[float], is_f: bool) -> int:
    """Colour a temperature by how warm it is; the same scale on every view."""
    if v is None:
        return curses.A_DIM
    f = v if is_f else (c_to_f(v) or 0)
    if f < 32:
        return curses.color_pair(C_MAGENTA)
    if f < 50:
        return curses.color_pair(C_CYAN)
    if f < 70:
        return curses.color_pair(C_GREEN)
    if f < 85:
        return curses.color_pair(C_YELLOW)
    return curses.color_pair(C_RED) | curses.A_BOLD
