#!/usr/bin/env python3
"""
NWS Weather TUI — Radar state management, animation, and background fetch.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from nws_weather_tui.constants import RADAR_LAST_PNG_PATH, ensure_dir
from nws_weather_tui.geo import bbox_around, clamp, expand_bbox_km
from nws_weather_tui.helpers import dbg
from nws_weather_tui.models import RadarFrame
from nws_weather_tui.radar.decode import RadarCell
from nws_weather_tui.radar.overlays import (
    city_overlay_and_hits_for_bbox,
    png_to_line_overlay,
    shape_line_overlay,
    vector_lines_overlay,
)
from nws_weather_tui.radar.decode import png_to_ascii, png_to_halfblock_radar

if TYPE_CHECKING:
    from nws_weather_tui.app import App


@dataclass
class RadarState:
    """Everything the radar panel shows and the bookkeeping of the fetch
    that feeds it. One per App; reset on a location change."""

    # Decoded animation frames and the one on screen.
    frames: List[RadarFrame] = field(default_factory=list)
    frame_idx: int = 0
    anim_playing: bool = False
    anim_last: float = 0.0
    anim_interval: float = 0.5
    cells: List[List[RadarCell]] = field(default_factory=list)
    ascii_lines: List[str] = field(default_factory=list)
    kind_lines: List[str] = field(default_factory=list)
    ts_ms: int = 0
    ts_utc: str = ""
    # Overlays drawn over the frame, and where the city labels are for clicks.
    state_overlay: List[str] = field(default_factory=list)
    city_overlay: List[str] = field(default_factory=list)
    alert_overlay: List[str] = field(default_factory=list)
    city_hitmap: Dict[Tuple[int, int], Tuple[str, float, float]] = field(default_factory=dict)
    # The last fetch: when, for which view, and how it went.
    last: float = 0.0
    err: Optional[str] = None
    bbox: Optional[Tuple[float, float, float, float]] = None
    bbox_state: Optional[str] = None  # state code the bbox was built for (None = radius fallback)
    src_cols: int = 0
    src_rows: int = 0
    progress: Tuple[int, int] = (0, 0)
    # Where the map was last drawn on the screen, for mouse hits.
    map_x: int = 0
    map_y: int = 0
    map_cols: int = 0
    map_rows: int = 0
    has_256color: bool = False

    def reset(self) -> None:
        """Forget the frames, overlays and fetch for the old location; the
        configured interval and the terminal's colour support stay."""
        fresh = RadarState(anim_interval=self.anim_interval, has_256color=self.has_256color)
        self.__dict__.update(fresh.__dict__)


def reset_radar_state(app: "App") -> None:
    app.radar.reset()


def set_current_radar_frame(app: "App") -> None:
    """Copy data from the current animation frame index to display state."""
    if not app.radar.frames:
        return
    idx = clamp(app.radar.frame_idx, 0, len(app.radar.frames) - 1)
    f = app.radar.frames[idx]
    app.radar.cells = f.cells
    app.radar.ascii_lines = f.ascii_lines
    app.radar.kind_lines = f.kind_lines
    app.radar.ts_ms = f.timestamp_ms
    app.radar.ts_utc = ts_ms_to_utc_str(f.timestamp_ms)


def ts_ms_to_utc_str(ts_ms: int) -> str:
    if not ts_ms:
        return "—"
    try:
        ts_dt = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
        return ts_dt.strftime("%H:%Mz")
    except Exception:
        return "—"


def save_radar_png(png: bytes) -> None:
    try:
        ensure_dir(os.path.dirname(RADAR_LAST_PNG_PATH))
        tmp = RADAR_LAST_PNG_PATH + ".tmp"
        with open(tmp, "wb") as f:
            f.write(png)
        os.replace(tmp, RADAR_LAST_PNG_PATH)
    except Exception as e:
        dbg(f"RADAR save failed: {e}")


def toggle_radar_anim(app: "App") -> bool:
    if len(app.radar.frames) < 2:
        app._flash("Animation requires ≥2 frames. Try refreshing (r).", 2.5)
        return True
    app.radar.anim_playing = not app.radar.anim_playing
    if app.radar.anim_playing:
        app.radar.anim_last = time.time()
        app._flash(
            f"Animation ON  ({len(app.radar.frames)} frames)", 1.5
        )
    else:
        app._flash("Animation OFF", 1.2)
    return True


def step_radar_frame(app: "App", direction: int) -> bool:
    if not app.radar.frames:
        return True
    n = len(app.radar.frames)
    app.radar.frame_idx = (app.radar.frame_idx + direction) % n
    set_current_radar_frame(app)
    app.radar.anim_playing = False
    return True


RADAR_RETRY_S = 60  # after a failed fetch, wait this long before trying again


def maybe_refresh_radar(app: "App", target_cols: int, target_rows: int) -> None:
    if not app.show_radar_map:
        return
    if app._bg_radar_running:
        return
    ttl = int(app.cfg.get("cache_ttls", {}).get("radar", 300))
    age = time.time() - app.radar.last
    same_view = (
        app.radar.src_cols == target_cols
        and app.radar.src_rows == target_rows
        # The first fetch can happen before the weather load sets state_code,
        # leaving a radius view around the user; rebuild once the state is known.
        and app.radar.bbox_state == app.state_code
    )
    if same_view and age < ttl and (app.radar.cells or app.radar.ascii_lines):
        return
    # A failed fetch leaves no frames; without this every redraw (several a
    # second) would start another full round of requests to every source.
    if same_view and app.radar.err and age < RADAR_RETRY_S:
        return

    try:
        from PIL import Image as _PIL_Image  # noqa: F401
    except ImportError:
        app.radar.ascii_lines = ["(install pillow to render radar map)"]
        app.radar.kind_lines = []
        app.radar.state_overlay = []
        app.radar.city_hitmap = {}
        app.radar.err = "pillow missing"
        app.radar.last = time.time()
        return

    radar_cfg = dict(app.cfg.get("radar", {}) or {})
    ramp = str(radar_cfg.get("ascii_ramp", " .:-=+*#%@"))
    show_state_lines = bool(radar_cfg.get("show_state_lines", True))
    show_city_labels = bool(radar_cfg.get("show_city_labels", True))
    show_alert_polygons = bool(radar_cfg.get("show_alert_polygons", True))
    max_city_labels = int(radar_cfg.get("max_city_labels", 20))
    n_frames = int(radar_cfg.get("animation_frames", 8))
    step_min = int(radar_cfg.get("animation_step_min", 5))

    # The state bbox lookup can make several HTTP requests (and the cache is
    # cleared on every location change), so it happens on the worker thread;
    # _radar_bbox/_radar_src_* are stored by apply_bg_radar from the result.
    app.radar.last = time.time()
    app._bg_radar_running = True
    app._is_loading = True
    app.status_msg = "/ Loading radar..."
    app.status_until = time.time() + 3600

    ctx = {
        "radar_station": app.radar_station or "",
        "target_cols": target_cols, "target_rows": target_rows,
        "has_256color": app.radar.has_256color,
        "ramp": ramp,
        "show_state_lines": show_state_lines,
        "show_city_labels": show_city_labels,
        "show_alert_polygons": show_alert_polygons,
        "max_city_labels": max_city_labels,
        "alert_geometries": [
            {"geometry": a.geometry, "severity": a.severity}
            for a in app.alerts if a.geometry
        ],
        "n_frames": n_frames, "step_min": step_min,
        "lat": app.lat, "lon": app.lon,
        "state_code": app.state_code,
        "anim_playing": app.radar.anim_playing,
    }
    threading.Thread(
        target=_bg_radar_fetch, args=(app, ctx, app._bg_generation), daemon=True
    ).start()


def fit_bbox_to_panel(
    bbox: Tuple[float, float, float, float], target_cols: int, target_rows: int
) -> Tuple[Tuple[float, float, float, float], int, int]:
    """Widen `bbox` to the panel's shape; return (bbox, req_w, req_h)."""
    minlon, minlat, maxlon, maxlat = bbox
    cos_lat = abs(math.cos(math.radians((minlat + maxlat) / 2)))
    geo_aspect = ((maxlon - minlon) * cos_lat) / max(1e-9, maxlat - minlat)
    # On a panel wider than the state's shape, show more country east and
    # west rather than leave the sides empty (terminal cells ~2:1, and the
    # half-block renderer packs 2 pixels per row). Capped so a very wide
    # screen doesn't zoom out to half the continent.
    want_aspect = target_cols / max(1, target_rows * 2)
    if want_aspect > geo_aspect * 1.05:
        widen = min(want_aspect / geo_aspect, 4.0)
        mid, half = (minlon + maxlon) / 2, (maxlon - minlon) * widen / 2
        minlon, maxlon = mid - half, mid + half
        bbox = (minlon, minlat, maxlon, maxlat)
        geo_aspect *= widen
    req_h = max(64, target_rows * 12)
    req_w = max(64, int(req_h * geo_aspect))
    return bbox, req_w, req_h


def _bg_radar_fetch(app: "App", ctx: Dict[str, Any], gen: int) -> None:
    """Run in background thread — fetches radar frames + overlays."""
    # Recorded even when the fetch fails, so apply_bg_radar can mark the view
    # as fetched and maybe_refresh_radar's retry backoff applies to it.
    result: Dict[str, Any] = {
        "ok": False,
        "target_cols": ctx["target_cols"], "target_rows": ctx["target_rows"],
        "state_code": ctx["state_code"],
    }
    app.radar.progress = (0, ctx["n_frames"])
    try:
        state_code = ctx["state_code"]
        bbox = app.client.state_bbox(state_code) if state_code else None
        bbox = (
            expand_bbox_km(bbox, 20.0)
            if bbox
            else bbox_around(ctx["lat"], ctx["lon"], 300.0)
        )
        bbox, req_w, req_h = fit_bbox_to_panel(
            bbox, ctx["target_cols"], ctx["target_rows"]
        )
        result["bbox"] = bbox
        ctx = dict(ctx, bbox=bbox, req_w=req_w, req_h=req_h)

        raw_frames = app.client.radar_frames_png(
            ctx["radar_station"], ctx["bbox"],
            ctx["req_w"], ctx["req_h"],
            n_frames=ctx["n_frames"], step_minutes=ctx["step_min"],
            progress_cb=lambda done, total: setattr(app.radar, "progress", (done, total)),
        )
        if not raw_frames:
            raise RuntimeError("radar returned no frames")

        new_frames: List[RadarFrame] = []
        for fi, (png, ts_ms) in enumerate(raw_frames):
            if fi == len(raw_frames) - 1:
                save_radar_png(png)
            if ctx["has_256color"]:
                cells = png_to_halfblock_radar(png, ctx["target_cols"], ctx["target_rows"])
                if not any(fg or bg for row in cells for _, fg, bg in row):
                    dbg(f"RADAR skipping blank frame ts={ts_ms}")
                    continue
                ascii_lines, kind_lines = [], []
            else:
                cells = []
                ascii_lines, kind_lines = png_to_ascii(
                    png, ctx["target_cols"], ctx["target_rows"], ctx["ramp"]
                )
                if not any(ch != " " for line in ascii_lines for ch in line):
                    dbg(f"RADAR skipping blank frame ts={ts_ms}")
                    continue
            new_frames.append(RadarFrame(
                cells=cells, ascii_lines=ascii_lines,
                kind_lines=kind_lines, timestamp_ms=ts_ms,
            ))
        if not new_frames:
            png, ts_ms = raw_frames[-1]
            if ctx["has_256color"]:
                cells = png_to_halfblock_radar(png, ctx["target_cols"], ctx["target_rows"])
                new_frames.append(RadarFrame(
                    cells=cells, ascii_lines=[], kind_lines=[], timestamp_ms=ts_ms,
                ))
            else:
                ascii_lines, kind_lines = png_to_ascii(
                    png, ctx["target_cols"], ctx["target_rows"], ctx["ramp"]
                )
                new_frames.append(RadarFrame(
                    cells=[], ascii_lines=ascii_lines,
                    kind_lines=kind_lines, timestamp_ms=ts_ms,
                ))
        result["frames"] = new_frames

        state_overlay: List[str] = []
        city_overlay: List[str] = []
        city_hitmap: Dict[Tuple[int, int], Tuple[str, float, float]] = {}

        if ctx["show_state_lines"]:
            try:
                feats = app.client.state_lines_features(ctx["bbox"], ctx["state_code"])
                state_overlay = vector_lines_overlay(
                    feats, ctx["bbox"], ctx["target_cols"], ctx["target_rows"], mark="|"
                )
                if not any(state_overlay):
                    ol, out_bbox = app.client.state_lines_png(
                        ctx["bbox"], ctx["req_w"], ctx["req_h"]
                    )
                    if ol:
                        state_overlay = png_to_line_overlay(
                            ol, ctx["target_cols"], ctx["target_rows"],
                            mark="|", src_bbox=out_bbox, dst_bbox=ctx["bbox"],
                        )
                state_overlay = shape_line_overlay(state_overlay)
            except Exception as oe:
                dbg(f"RADAR state-lines overlay failed: {oe}")

        if ctx["show_city_labels"]:
            city_overlay, city_hitmap = city_overlay_and_hits_for_bbox(
                ctx["target_cols"], ctx["target_rows"], ctx["bbox"],
                max_labels=ctx["max_city_labels"],
                here=(ctx["lat"], ctx["lon"]),
            )

        alert_overlay: List[str] = []
        if ctx["show_alert_polygons"] and ctx["alert_geometries"]:
            try:
                alert_overlay = vector_lines_overlay(
                    ctx["alert_geometries"], ctx["bbox"],
                    ctx["target_cols"], ctx["target_rows"], mark="!",
                )
            except Exception as ae:
                dbg(f"RADAR alert-polygon overlay failed: {ae}")

        result["state_overlay"] = state_overlay
        result["city_overlay"] = city_overlay
        result["alert_overlay"] = alert_overlay
        result["city_hitmap"] = city_hitmap
        result["anim_playing"] = ctx["anim_playing"]
        result["ok"] = True

    except Exception as e:
        result["error"] = str(e)
        dbg(f"RADAR refresh failed: {e}")

    result["_gen"] = gen
    with app._bg_lock:
        app._bg_radar_pending = result


def apply_bg_radar(app: "App") -> None:
    """Main-thread: apply completed background radar results."""
    with app._bg_lock:
        result = app._bg_radar_pending
        app._bg_radar_pending = None
    if result is None:
        return
    if result.get("_gen") != app._bg_generation:
        app._bg_radar_running = False
        return
    app._bg_radar_running = False
    app._is_loading = app._bg_weather_running
    app.radar.last = time.time()
    # The view this result was fetched for (success or not); the next draw
    # compares against it to decide whether to fetch again.
    if result.get("bbox"):
        app.radar.bbox = result["bbox"]
    app.radar.bbox_state = result.get("state_code")
    app.radar.src_cols = int(result.get("target_cols", 0))
    app.radar.src_rows = int(result.get("target_rows", 0))

    if result.get("ok"):
        frames = result["frames"]
        app.radar.frames = frames
        if not result.get("anim_playing"):
            app.radar.frame_idx = len(frames) - 1
        set_current_radar_frame(app)
        app.radar.state_overlay = result.get("state_overlay", [])
        app.radar.city_overlay = result.get("city_overlay", [])
        app.radar.alert_overlay = result.get("alert_overlay", [])
        app.radar.city_hitmap = result.get("city_hitmap", {})
        app.radar.err = None
        if not app._bg_weather_running:
            app._flash("Radar updated.", 1.1)
    else:
        app.radar.cells = []
        app.radar.ascii_lines = []
        app.radar.kind_lines = []
        app.radar.state_overlay = []
        app.radar.city_overlay = []
        app.radar.alert_overlay = []
        app.radar.city_hitmap = {}
        app.radar.err = result.get("error", "unknown")
