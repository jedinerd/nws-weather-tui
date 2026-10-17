#!/usr/bin/env python3
"""
NWS Weather TUI — Helper functions and utilities.

Most functions have been extracted to dedicated modules.  This file
keeps the residual helpers and re-exports everything that other modules
still import via ``from helpers import …``.
"""

from __future__ import annotations

import datetime as dt
import textwrap
import unicodedata
from functools import lru_cache
from typing import List, Optional, Tuple

from nws_weather_tui.constants import DEBUG_LOG_PATH, ensure_dir

# --- Re-exports from extracted modules (backward compat) ---
from nws_weather_tui.conversions import c_to_f, dewpoint_c, m_to_mi, mps_to_mph, pa_to_inhg  # noqa: F401
from nws_weather_tui.formatting import fmt_num, fmt_time, parse_first_number, parse_iso      # noqa: F401
from nws_weather_tui.geo import bbox_around, clamp, expand_bbox_km                           # noqa: F401
from nws_weather_tui.radar.overlays import (                                                        # noqa: F401
    city_overlay_and_hits_for_bbox,
    png_to_line_overlay,
    vector_lines_overlay,
)
from nws_weather_tui.radar.decode import RadarCell, png_to_ascii, png_to_halfblock_radar      # noqa: F401

try:
    from astral import LocationInfo
    from astral.sun import sun
except ImportError:
    LocationInfo = None  # type: ignore[assignment]
    sun = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Astronomy
# ---------------------------------------------------------------------------

def get_sunrise_sunset(
    lat: float, lon: float, date: dt.date
) -> Tuple[Optional[dt.datetime], Optional[dt.datetime]]:
    if sun is None or LocationInfo is None:
        return None, None
    # LocationInfo defaults to Europe/London, which puts the US's sunset
    # on the previous calendar day; use the local zone the app displays
    # in, with the offset in force on `date` (not today's: across a DST
    # change that would land an hour off).
    local_noon = dt.datetime(date.year, date.month, date.day, 12).astimezone()
    return _sun_times(lat, lon, date, local_noon.utcoffset())


@lru_cache(maxsize=512)
def _sun_times(
    lat: float, lon: float, date: dt.date, utc_offset: Optional[dt.timedelta]
) -> Tuple[Optional[dt.datetime], Optional[dt.datetime]]:
    # The views ask for the same week of dates on every draw tick; the
    # answer only changes with the location, the date or the zone's offset
    # (keyed as a timedelta: the local tzinfo object itself may not hash).
    try:
        loc = LocationInfo(latitude=lat, longitude=lon)
        local_tz = dt.timezone(utc_offset) if utc_offset is not None else dt.timezone.utc
        s = sun(loc.observer, date=date, tzinfo=local_tz)
        return s["sunrise"].astimezone(), s["sunset"].astimezone()
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Debug logging
# ---------------------------------------------------------------------------

def dbg(msg: str) -> None:
    try:
        ensure_dir(DEBUG_LOG_PATH.replace("debug.log", ""))
        ts = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Curses / text utilities
# ---------------------------------------------------------------------------

def text_width(s: str) -> int:
    """Terminal cells `s` occupies — East Asian wide glyphs (⛅ ☔ ⚡ …)
    take two, so padding by len() misaligns columns after them."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def fit_width(s: str, width: int, align: str = "<") -> str:
    """Truncate (with …) or pad `s` to exactly `width` cells."""
    w = text_width(s)
    if w > width:
        out, w = "", 0
        for ch in s:
            cw = text_width(ch)
            if w + cw > width - 1:
                break
            out += ch
            w += cw
        s, w = out + "…", w + 1
    pad = " " * max(0, width - w)
    return pad + s if align == ">" else s + pad


def safe_addstr(win, y: int, x: int, s: str, attr: int = 0) -> None:
    try:
        win.addstr(y, x, s, attr)
    except Exception:
        pass


@lru_cache(maxsize=256)
def _wrap_lines_cached(text: str, width: int) -> Tuple[str, ...]:
    """Wrap text respecting embedded newlines.

    Single newlines reflow within a paragraph; double newlines (blank
    lines) are preserved as paragraph breaks.
    """
    lines: List[str] = []
    for paragraph in text.split("\n\n"):
        if lines:
            lines.append("")  # blank line between paragraphs
        # Collapse single newlines within the paragraph into spaces
        flat = " ".join(paragraph.split("\n"))
        flat = flat.strip()
        if not flat:
            continue
        wrapped = textwrap.wrap(flat, width=width)
        lines.extend(wrapped if wrapped else [""])
    return tuple(lines)


def wrap_lines(text: str, width: int) -> Tuple[str, ...]:
    if width <= 1:
        return (text,)
    return _wrap_lines_cached(text, width)
