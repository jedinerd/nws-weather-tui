#!/usr/bin/env python3
"""
NWS Weather TUI — Radar panel and full-screen radar view.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from typing import TYPE_CHECKING, List

import curses

from nws_weather_tui.formatting import fmt_time
from nws_weather_tui.helpers import safe_addstr
from nws_weather_tui.radar.renderer import (
    draw_alert_overlay_line,
    draw_city_overlay_line,
    draw_radar_ascii_line,
    draw_radar_halfblock_line,
    draw_state_overlay_line,
)

if TYPE_CHECKING:
    from nws_weather_tui.app import App


RADAR_STALE_S = 20 * 60  # MRMS updates every ~2 min; older than this is notable


def radar_age_tag(age_s: float) -> str:
    """'  (3h 43m old)' once a radar frame is stale enough to mention."""
    if age_s < RADAR_STALE_S:
        return ""
    mins = int(age_s // 60)
    return f"  ({mins // 60}h {mins % 60:02d}m old)" if mins >= 60 else f"  ({mins}m old)"


def draw_radar_panel(
    app: "App",
    win,
    y: int,
    cols: int,
    rows: int,
    full_screen: bool = False,
) -> None:
    """Draw radar map, legend, and overlays starting at row y."""
    radar_cfg = dict(app.cfg.get("radar", {}) or {})
    ramp = str(radar_cfg.get("ascii_ramp", " .:-=+*#%@"))
    show_state_lines = bool(radar_cfg.get("show_state_lines", True))
    show_city_labels = bool(radar_cfg.get("show_city_labels", True))
    show_alert_polygons = bool(radar_cfg.get("show_alert_polygons", True))

    # --- header line ---
    n_frames = len(app.radar.frames)
    frame_idx = app.radar.frame_idx + 1
    anim_tag = (
        f"  ANIM {frame_idx}/{n_frames}" if app.radar.anim_playing
        else f"  frame {frame_idx}/{max(1, n_frames)}" if n_frames > 1
        else ""
    )
    ts_tag = ""
    age_tag = ""
    if app.radar.ts_ms:
        local = dt.datetime.fromtimestamp(app.radar.ts_ms / 1000).astimezone()
        ts_tag = f"  {fmt_time(local, app.use_24h)}"
        age_tag = radar_age_tag(time.time() - app.radar.ts_ms / 1000)
    state_tag = f" \u2014 {app.state_code}" if app.state_code else ""
    header = f"Radar{state_tag}{ts_tag}{anim_tag}"
    header = header[: cols - 1]
    age_tag = age_tag[: max(0, cols - 1 - len(header))]
    header_x = max(0, (cols - 1 - len(header) - len(age_tag)) // 2)
    safe_addstr(win, y, header_x, header, curses.color_pair(15) | curses.A_BOLD)
    if age_tag:
        # An old frame is easy to mistake for the current picture.
        safe_addstr(win, y, header_x + len(header), age_tag,
                    curses.color_pair(2) | curses.A_BOLD)
    y += 1

    # --- radar map ---
    map_rows = rows - y - 1
    max_cols = max(20, cols - 1) if full_screen else max(20, min(cols - 1, int((cols - 1) * 0.96)))

    # Constrain width to geographic aspect ratio so tall states aren't stretched
    map_cols = max_cols
    if app.radar.bbox:
        minlon, minlat, maxlon, maxlat = app.radar.bbox
        lat_span = maxlat - minlat
        lon_span = maxlon - minlon
        if lat_span > 1e-6 and lon_span > 1e-6:
            cos_lat = abs(math.cos(math.radians((minlat + maxlat) / 2)))
            geo_aspect = (lon_span * cos_lat) / lat_span
            # Terminal chars are ~2x tall as wide; half-block = 2 vpx per row
            ideal_cols = int(round(geo_aspect * map_rows * 2))
            # The bbox was fitted to the last size we asked for; don't chase
            # rounding error into a refresh loop.
            if abs(ideal_cols - app.radar.src_cols) <= 2:
                ideal_cols = app.radar.src_cols
            map_cols = max(20, min(max_cols, ideal_cols))

    map_x = (cols - 1 - map_cols) // 2

    win_y0, win_x0 = win.getbegyx()
    app.radar.map_x = win_x0 + map_x
    app.radar.map_y = win_y0 + y
    app.radar.map_cols = map_cols
    app.radar.map_rows = max(0, map_rows)

    if map_rows < 3:
        return

    from nws_weather_tui.radar.state import maybe_refresh_radar
    maybe_refresh_radar(app, map_cols, map_rows)

    # Don't draw stale data from a different size — show loading stub
    stale = (app.radar.src_cols != map_cols or app.radar.src_rows != map_rows)

    city_lines: List[str] = app.radar.city_overlay if show_city_labels else []
    alert_lines: List[str] = app.radar.alert_overlay if show_alert_polygons else []

    if stale:
        _draw_loading(win, y, map_x, map_cols, map_rows)
    elif app.radar.has_256color and app.radar.cells:
        for i, row_cells in enumerate(app.radar.cells[:map_rows]):
            ry = y + i
            if ry >= rows:
                break
            draw_radar_halfblock_line(win, ry, row_cells, cols, x_off=map_x)
            if show_state_lines and i < len(app.radar.state_overlay):
                draw_state_overlay_line(
                    win, ry, app.radar.state_overlay[i], cols, x_off=map_x
                )
            if i < len(alert_lines):
                draw_alert_overlay_line(win, ry, alert_lines[i], cols, x_off=map_x)
            if i < len(city_lines):
                draw_city_overlay_line(win, ry, city_lines[i], cols, x_off=map_x)

    elif app.radar.ascii_lines:
        for i, line in enumerate(app.radar.ascii_lines[:map_rows]):
            ry = y + i
            if ry >= rows:
                break
            kind = app.radar.kind_lines[i] if i < len(app.radar.kind_lines) else ""
            draw_radar_ascii_line(win, ry, line, kind, cols, ramp, x_off=map_x)
            if show_state_lines and i < len(app.radar.state_overlay):
                draw_state_overlay_line(
                    win, ry, app.radar.state_overlay[i], cols, x_off=map_x
                )
            if i < len(alert_lines):
                draw_alert_overlay_line(win, ry, alert_lines[i], cols, x_off=map_x)
            if i < len(city_lines):
                draw_city_overlay_line(win, ry, city_lines[i], cols, x_off=map_x)

    elif app.radar.err:
        safe_addstr(
            win, y, map_x,
            f"(radar unavailable: {app.radar.err})"[:map_cols],
            curses.color_pair(4),
        )
    else:
        _draw_loading(win, y, map_x, map_cols, map_rows)


def _draw_loading(win, y: int, map_x: int, map_cols: int, map_rows: int) -> None:
    text = "(loading radar\u2026)"[:map_cols]
    safe_addstr(
        win, y + map_rows // 2, map_x + (map_cols - len(text)) // 2,
        text,
        curses.A_DIM,
    )


def draw_radar_view(app: "App", win) -> None:
    win.erase()
    rows, cols = win.getmaxyx()
    draw_radar_panel(app, win, 0, cols, rows, full_screen=True)
    win.noutrefresh()
