#!/usr/bin/env python3
"""
NWS Weather TUI — Main application.
"""

from __future__ import annotations

import signal
import threading
import time
from typing import Any, Dict, List, Optional

import curses

from nws_weather_tui.api.client import NWSClient
from nws_weather_tui.constants import (
    DEFAULT_CONFIG,
    MIN_COLS,
    MIN_ROWS,
    STATE_PATH,
    load_config,
    save_json,
)
from nws_weather_tui.curses_init import init_curses, init_radar_colors
from nws_weather_tui.geo import clamp
from nws_weather_tui.helpers import safe_addstr
from nws_weather_tui.input_handler import handle_key
from nws_weather_tui.models import (
    AirQuality,
    AlertItem,
    CurrentConditions,
    ForecastPeriod,
    HourlyPeriod,
    UVIndex,
)
from nws_weather_tui.notify import AlertNotifier
from nws_weather_tui.persistence import build_state_dict, load_app_state, save_app_config
from nws_weather_tui.radar.state import RadarState, reset_radar_state
from nws_weather_tui.views import (
    draw_header,
    draw_footer,
    draw_current,
    draw_forecast,
    draw_hourly,
    draw_alerts,
    draw_help,
    draw_moon,
    draw_favorites,
    draw_afd,
    draw_hwo,
    draw_dashboard,
)
from nws_weather_tui.weather_refresh import refresh_all

_DRAWERS = {
    "current": draw_current,
    "forecast": draw_forecast,
    "hourly": draw_hourly,
    "alerts": draw_alerts,
    "help": draw_help,
    "moon": draw_moon,
    "favorites": draw_favorites,
    "afd": draw_afd,
    "hwo": draw_hwo,
    "dashboard": draw_dashboard,
}


class App:
    def __init__(
        self, stdscr, cfg: Dict[str, Any], startup_warning: Optional[str] = None
    ) -> None:
        self.stdscr = stdscr
        self.cfg = cfg
        # Shown once the first refresh finishes, so the spinner doesn't hide it.
        self.startup_warning: Optional[str] = startup_warning

        # --- Location & preferences ---
        self.location_name: str = str(cfg.get("location_name", "—"))
        self.lat: float = float(cfg.get("lat", 0.0))
        self.lon: float = float(cfg.get("lon", 0.0))
        self.units: str = str(cfg.get("units", "us"))
        self.use_24h: bool = bool(cfg.get("use_24h", False))
        self.auto_refresh_seconds: int = int(cfg.get("auto_refresh_seconds", 300))
        self.timeout: int = int(cfg.get("http_timeout", 10))
        self.hourly_hours: int = int(cfg.get("hourly_hours", 0))
        self.show_radar_map: bool = bool(cfg.get("show_radar_map", True))
        self.alert_notifier = AlertNotifier(cfg.get("alert_notify") or [])
        self.favorites: List[Dict[str, Any]] = list(cfg.get("favorites", []) or [])
        self.fav_idx: int = 0
        self.fav_edit_idx: int = 0

        # --- API client ---
        self.client = NWSClient(
            user_agent=str(cfg.get("user_agent", DEFAULT_CONFIG["user_agent"])),
            timeout=self.timeout,
            ttls=dict(cfg.get("cache_ttls", DEFAULT_CONFIG["cache_ttls"])),
        )

        # --- UI state ---
        self.view: str = "current"
        self._prev_view: str = "current"
        self.paused: bool = False
        self.status_msg: str = ""
        self.status_until: float = 0.0
        self._spinner_idx: int = 0
        self._spinner_frames = ["|", "/", "-", "\\"]
        self._is_loading: bool = False
        self._in_refresh: bool = False
        self._bg_lock = threading.Lock()
        self._bg_generation: int = 0
        self._bg_weather_pending: Optional[Dict[str, Any]] = None
        self._bg_weather_running: bool = False
        self._bg_radar_pending: Optional[Dict[str, Any]] = None
        self._bg_radar_running: bool = False
        self._bg_dash_pending: Optional[Dict[str, Any]] = None
        self._bg_dash_running: bool = False
        self._dash_data: Dict[int, Dict[str, Any]] = {}
        self._dash_last: float = 0.0
        self._state_thread: Optional[threading.Thread] = None

        # --- NWS data ---
        self.points_data: Optional[Dict[str, Any]] = None
        self.forecast_url: Optional[str] = None
        self.hourly_url: Optional[str] = None
        self.stations_url: Optional[str] = None
        self.station_id: Optional[str] = None
        self.radar_station: Optional[str] = None
        self.state_code: Optional[str] = None
        self.office_id: Optional[str] = None

        self.current: Optional[CurrentConditions] = None
        self.forecast_periods: List[ForecastPeriod] = []
        self.hourly_periods: List[HourlyPeriod] = []
        self.alerts: List[AlertItem] = []
        self.area_alerts: List[AlertItem] = []  # elsewhere in the state
        self.air_quality: Optional[AirQuality] = None
        self.uv_index: Optional[UVIndex] = None
        self.afd: Optional[Dict[str, Any]] = None
        self.afd_earlier: List[Dict[str, Any]] = []
        self.hwo: Optional[Dict[str, Any]] = None
        self.grid_data_url: Optional[str] = None

        self.last_refresh: float = 0.0
        self.next_refresh: float = 0.0
        self.offline_mode: bool = False
        self.offline_reason: str = ""

        # --- Scroll positions ---
        self.fc_scroll: int = 0
        self.hr_scroll: int = 0
        self.alert_line_scroll: int = 0
        self.help_scroll: int = 0
        self.afd_scroll: int = 0
        self.hwo_scroll: int = 0
        self.dash_idx: int = 0

        # --- Radar panel: frames, overlays and the fetch that feeds them ---
        self.radar = RadarState(
            anim_interval=float(
                dict(cfg.get("radar", {}) or {}).get("animation_interval_s", 0.5)
            ),
        )

        # --- Init curses ---
        init_curses(self.stdscr)
        self.radar.has_256color = init_radar_colors()

        refresh_all(self, force=True, allow_offline=True)

    # ------------------------------------------------------------------
    # Status / loading messages
    # ------------------------------------------------------------------

    def _flash(self, msg: str, seconds: float = 2.0) -> None:
        self._is_loading = False
        self.status_msg = msg
        self.status_until = time.time() + seconds

    def _show_loading(self, msg: str) -> None:
        frame = self._spinner_frames[self._spinner_idx % len(self._spinner_frames)]
        self._spinner_idx += 1
        self._is_loading = True
        self.status_msg = f"{frame} {msg}"
        self.status_until = time.time() + 3600
        self._draw()

    # ------------------------------------------------------------------
    # Location
    # ------------------------------------------------------------------

    def _apply_location(self, name: str, lat: float, lon: float) -> None:
        self.location_name = name
        self.lat = float(lat)
        self.lon = float(lon)
        self.points_data = None
        self.forecast_url = None
        self.hourly_url = None
        self.stations_url = None
        self.station_id = None
        self.radar_station = None
        self.state_code = None
        self.office_id = None
        self.afd = None
        self.afd_earlier = []
        self.area_alerts = []
        self.afd_scroll = 0
        self.hwo = None
        self.hwo_scroll = 0
        self.grid_data_url = None
        self.client.cache.clear()
        reset_radar_state(self)
        self._bg_generation += 1
        self._bg_weather_running = False
        self._bg_radar_running = False
        refresh_all(self, force=True, allow_offline=True)
        self._save_cfg()

    # ------------------------------------------------------------------
    # Config / state persistence
    # ------------------------------------------------------------------

    def _save_cfg(self) -> None:
        save_app_config(self)

    def _save_state(self) -> None:
        snapshot = build_state_dict(self)
        t = threading.Thread(
            target=self._write_state_bg, args=(snapshot,), daemon=False
        )
        t.start()
        self._state_thread = t

    @staticmethod
    def _write_state_bg(snapshot: dict) -> None:
        try:
            save_json(STATE_PATH, snapshot)
        except Exception:
            pass

    def _load_state(self) -> bool:
        return load_app_state(self)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        self.stdscr.nodelay(False)
        self.stdscr.timeout(500)
        while True:
            self._tick()
            ch = self.stdscr.getch()
            if ch != -1 and not handle_key(self, ch):
                break
        self._cleanup()

    def _cleanup(self) -> None:
        if self._state_thread is not None:
            self._state_thread.join(timeout=2.0)

    def _tick(self) -> None:
        from nws_weather_tui.radar.state import apply_bg_radar, set_current_radar_frame
        from nws_weather_tui.weather_refresh import apply_bg_weather

        # Apply any completed background work
        if self._bg_weather_pending is not None:
            apply_bg_weather(self)
        if self._bg_radar_pending is not None:
            apply_bg_radar(self)
        if self._bg_dash_pending is not None:
            from nws_weather_tui.dashboard import apply_bg_dashboard
            apply_bg_dashboard(self)

        if not self.paused:
            refresh_all(self, force=False, allow_offline=True)

        # Update loading spinner, but let an active _flash() message finish first
        flash_active = not self._is_loading and time.time() < self.status_until
        if (self._bg_weather_running or self._bg_radar_running) and not flash_active:
            frame = self._spinner_frames[self._spinner_idx % len(self._spinner_frames)]
            self._spinner_idx += 1
            self._is_loading = True
            if self._bg_weather_running:
                self.status_msg = f"{frame} Loading weather data..."
            else:
                done, total = self.radar.progress
                progress = f" ({done}/{total} frames)" if total > 1 else ""
                self.status_msg = f"{frame} Loading radar...{progress}"
            self.status_until = time.time() + 3600

        # Advance radar animation
        if self.radar.anim_playing and len(self.radar.frames) > 1:
            now = time.time()
            if now - self.radar.anim_last >= self.radar.anim_interval:
                self.radar.frame_idx = (
                    (self.radar.frame_idx + 1) % len(self.radar.frames)
                )
                set_current_radar_frame(self)
                self.radar.anim_last = now

        now = time.time()
        wait_ms = 500
        if self.next_refresh and not self.paused:
            wait_ms = min(wait_ms, max(50, int((self.next_refresh - now) * 1000)))
        if self.status_msg and now < self.status_until:
            wait_ms = min(wait_ms, max(50, int((self.status_until - now) * 1000)))
        if self.radar.anim_playing and self.radar.frames:
            wait_ms = min(wait_ms, int(self.radar.anim_interval * 1000))
        if self._bg_weather_running or self._bg_radar_running:
            wait_ms = min(wait_ms, 200)
        self.stdscr.timeout(clamp(wait_ms, 50, 1000))
        self._draw()

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _draw(self) -> None:
        self.stdscr.erase()
        rows, cols = self.stdscr.getmaxyx()
        self.radar.map_x = 0
        self.radar.map_y = 0
        self.radar.map_cols = 0
        self.radar.map_rows = 0

        if rows < MIN_ROWS or cols < MIN_COLS:
            safe_addstr(
                self.stdscr, 0, 0,
                f"Terminal too small ({cols}\u00d7{rows}). Need \u2265{MIN_COLS}\u00d7{MIN_ROWS}.",
                curses.color_pair(4) | curses.A_BOLD,
            )
            safe_addstr(self.stdscr, 2, 0, "Resize and try again.  Press q to quit.")
            self.stdscr.refresh()
            return

        draw_header(self, rows, cols)

        body_top = 3
        body_h = rows - body_top - 2
        body_w = cols - 2
        try:
            win = self.stdscr.derwin(body_h, body_w, body_top, 1)
        except curses.error:
            self.stdscr.refresh()
            return

        drawer = _DRAWERS.get(self.view)
        if drawer:
            drawer(self, win)

        draw_footer(self, rows, cols)

        self.stdscr.refresh()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(stdscr) -> None:
    cfg, config_warning = load_config()
    signal.signal(signal.SIGINT, lambda *_: None)
    app = App(stdscr, cfg, startup_warning=config_warning)
    try:
        app.run()
    finally:
        app.alert_notifier.restore()
