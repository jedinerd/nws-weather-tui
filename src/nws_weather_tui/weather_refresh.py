#!/usr/bin/env python3
"""
NWS Weather TUI — Weather data fetching (background thread pipeline).
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

from nws_weather_tui.helpers import dbg
from nws_weather_tui.models import (
    AlertItem,
    HourlyPeriod,
    extract_air_quality,
    extract_alerts,
    extract_current,
    extract_forecast,
    extract_grid_series,
    extract_hourly,
    extract_uv_index,
    merge_grid_into_hourly,
)
from nws_weather_tui.text_product import segment_for_zones

if TYPE_CHECKING:
    from nws_weather_tui.app import App

# Independent requests in one refresh run at most this many at a time.
MAX_PARALLEL_FETCHES = 8


def refresh_all(app: "App", force: bool = False, allow_offline: bool = False) -> None:
    now = time.time()
    if not force and now < app.next_refresh:
        return
    if app._bg_weather_running:
        return
    app.next_refresh = now + app.auto_refresh_seconds
    app._bg_weather_running = True
    app._is_loading = True
    app.status_msg = "/ Loading weather data..."
    app.status_until = time.time() + 3600
    ctx = {
        "lat": app.lat, "lon": app.lon, "units": app.units,
        "points_data": app.points_data, "forecast_url": app.forecast_url,
        "hourly_url": app.hourly_url, "stations_url": app.stations_url,
        "station_id": app.station_id, "hourly_hours": app.hourly_hours,
        "office_id": app.office_id, "grid_data_url": app.grid_data_url,
        "state_code": app.state_code,
        "allow_offline": allow_offline,
    }
    gen = app._bg_generation
    threading.Thread(
        target=_bg_weather_fetch, args=(app, ctx, gen), daemon=True
    ).start()


def _point_zones(points_data: Any) -> List[str]:
    """UGC codes for the point, e.g. ["OHZ055", "OHC049"], from the
    forecastZone/county URLs in its /points response."""
    props = (points_data or {}).get("properties", {}) or {}
    out = []
    for key in ("forecastZone", "county", "fireWeatherZone"):
        url = props.get(key)
        if isinstance(url, str) and url.rstrip("/"):
            out.append(url.rstrip("/").rsplit("/", 1)[-1].upper())
    return out


def _fetch_hourly(
    app: "App", hourly_url: str, units: str, hours: int, grid_data_url: Optional[str]
) -> List[HourlyPeriod]:
    hf = extract_hourly(app.client.forecast_hourly(hourly_url, units))
    hourly_periods = hf[:hours] if hours > 0 else hf
    # Gridpoint series (precip, feels-like, gusts, sky, thunder) are
    # supplementary — a failure here must not take the whole refresh
    # offline, and simply leaves those hourly fields empty.
    if grid_data_url:
        try:
            grid = extract_grid_series(app.client.forecast_grid_data(grid_data_url))
            merge_grid_into_hourly(hourly_periods, grid)
        except Exception as ge:
            dbg(f"Gridpoint data fetch failed: {ge}")
    return hourly_periods


def _fetch_alerts(
    app: "App", lat: float, lon: float, state: Optional[str]
) -> Tuple[List[AlertItem], List[AlertItem]]:
    """(alerts for the point, alerts elsewhere in the state). The latter give
    the Alerts view something to say when the point has none; supplementary,
    so its failures are quiet."""
    alerts = extract_alerts(app.client.alerts(lat, lon))
    area: List[AlertItem] = []
    if state and not alerts:
        try:
            area = extract_alerts(app.client.alerts_area(state))
        except Exception as ae:
            dbg(f"Area alerts fetch failed: {ae}")
    return alerts, area


def _fetch_discussions(
    app: "App", office_id: str
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    # One task for both so the product listing is fetched once, not twice.
    return (
        app.client.forecast_discussion(office_id),
        app.client.earlier_forecast_discussions(office_id),
    )


def _fetch_hwo(app: "App", office_id: str, points_data: Any) -> Optional[Dict[str, Any]]:
    hwo = app.client.hazardous_weather_outlook(office_id)
    if hwo:
        # An HWO has a segment per zone group; show only ours.
        hwo["text"] = segment_for_zones(hwo["text"], _point_zones(points_data))
    return hwo


def _bg_weather_fetch(app: "App", ctx: Dict[str, Any], gen: int) -> None:
    """Run in background thread — fetches all weather data."""
    result: Dict[str, Any] = {"ok": False}
    try:
        points_data = ctx["points_data"]
        station_id = ctx["station_id"]
        forecast_url = ctx["forecast_url"]
        hourly_url = ctx["hourly_url"]
        stations_url = ctx["stations_url"]
        office_id = ctx["office_id"]
        grid_data_url = ctx["grid_data_url"]

        if not points_data:
            points_data = app.client.points(ctx["lat"], ctx["lon"])
            props = (points_data or {}).get("properties", {}) or {}
            forecast_url = props.get("forecast")
            hourly_url = props.get("forecastHourly")
            stations_url = props.get("observationStations")
            rs = props.get("radarStation")
            result["radar_station"] = rs.strip() if isinstance(rs, str) and rs else None
            gdu = props.get("forecastGridData")
            grid_data_url = gdu if isinstance(gdu, str) and gdu else None
            result["grid_data_url"] = grid_data_url
            rel = (props.get("relativeLocation") or {}).get("properties", {})
            st = rel.get("state") if isinstance(rel, dict) else None
            result["state_code"] = (
                st.strip().upper()
                if isinstance(st, str) and re.fullmatch(r"[A-Za-z]{2}", st.strip())
                else None
            )
            cwa = props.get("cwa")
            office_id = cwa.strip().upper() if isinstance(cwa, str) and cwa else None
            result["office_id"] = office_id
            result["points_data"] = points_data
            result["forecast_url"] = forecast_url
            result["hourly_url"] = hourly_url
            result["stations_url"] = stations_url

        if not station_id and stations_url:
            stations = app.client.stations(stations_url)
            feats = (stations or {}).get("features", []) or []
            if feats:
                sid = ((feats[0] or {}).get("properties", {}) or {}).get("stationIdentifier")
                if isinstance(sid, str) and sid:
                    station_id = sid
                    result["station_id"] = sid

        # Everything else needs only the URLs above, so it runs concurrently:
        # a cold start or location change then takes about as long as the
        # slowest request instead of the sum of a dozen. The observation,
        # forecasts and alerts are the core — any of those failing takes the
        # refresh offline, as before. AFD/HWO, air quality and UV index are
        # supplementary sources whose failures are swallowed in their clients.
        state = result.get("state_code", ctx["state_code"])
        tasks: Dict[str, Callable[[], Any]] = {}
        if station_id:
            sid = station_id
            tasks["current"] = lambda: extract_current(app.client.latest_observation(sid))
        if forecast_url:
            fc_url = forecast_url
            tasks["forecast_periods"] = lambda: extract_forecast(
                app.client.forecast(fc_url, ctx["units"])
            )
        if hourly_url:
            h_url, gd_url = hourly_url, grid_data_url
            tasks["hourly_periods"] = lambda: _fetch_hourly(
                app, h_url, ctx["units"], ctx["hourly_hours"], gd_url
            )
        tasks["alerts"] = lambda: _fetch_alerts(app, ctx["lat"], ctx["lon"], state)
        if office_id:
            office = office_id
            tasks["afd"] = lambda: _fetch_discussions(app, office)
            tasks["hwo"] = lambda: _fetch_hwo(app, office, points_data)
        tasks["air_quality"] = lambda: extract_air_quality(
            app.client.air_quality(ctx["lat"], ctx["lon"])
        )
        tasks["uv_index"] = lambda: extract_uv_index(
            app.client.uv_index(ctx["lat"], ctx["lon"])
        )

        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_FETCHES, len(tasks))) as pool:
            futures = {key: pool.submit(fn) for key, fn in tasks.items()}
            fetched = {key: fut.result() for key, fut in futures.items()}

        for key in ("current", "forecast_periods", "hourly_periods",
                    "air_quality", "uv_index"):
            if key in fetched:
                result[key] = fetched[key]
        result["alerts"], result["area_alerts"] = fetched["alerts"]
        if "afd" in fetched:
            result["afd"], result["afd_earlier"] = fetched["afd"]
        if "hwo" in fetched:
            result["hwo"] = fetched["hwo"]
        result["ok"] = True

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["allow_offline"] = ctx["allow_offline"]

    result["_gen"] = gen
    with app._bg_lock:
        app._bg_weather_pending = result

def apply_bg_weather(app: "App") -> None:
    """Main-thread: apply completed background weather results."""
    with app._bg_lock:
        result = app._bg_weather_pending
        app._bg_weather_pending = None
    if result is None:
        return
    if result.get("_gen") != app._bg_generation:
        app._bg_weather_running = False
        return
    app._bg_weather_running = False
    app._is_loading = app._bg_radar_running

    if result.get("ok"):
        if "points_data" in result:
            app.points_data = result["points_data"]
        if "forecast_url" in result:
            app.forecast_url = result["forecast_url"]
        if "hourly_url" in result:
            app.hourly_url = result["hourly_url"]
        if "stations_url" in result:
            app.stations_url = result["stations_url"]
        if "station_id" in result:
            app.station_id = result["station_id"]
        if "radar_station" in result:
            app.radar_station = result["radar_station"]
        if "state_code" in result:
            app.state_code = result["state_code"]
            if not app.state_code:
                m = re.search(r",\s*([A-Za-z]{2})(?:\b|$)", app.location_name or "")
                app.state_code = m.group(1).upper() if m else None
        if "office_id" in result:
            app.office_id = result["office_id"]
        if "grid_data_url" in result:
            app.grid_data_url = result["grid_data_url"]
        if "afd" in result and result["afd"]:
            app.afd = result["afd"]
        if "afd_earlier" in result and result["afd_earlier"]:
            app.afd_earlier = result["afd_earlier"]
        if "hwo" in result and result["hwo"]:
            app.hwo = result["hwo"]

        if "current" in result:
            app.current = result["current"]
        if "forecast_periods" in result:
            app.forecast_periods = result["forecast_periods"]
        if "hourly_periods" in result:
            app.hourly_periods = result["hourly_periods"]
        if "alerts" in result:
            app.alerts = result["alerts"]
            app.area_alerts = result.get("area_alerts", [])
            app.alert_notifier.update(app.alerts, app.lat, app.lon)
        if "air_quality" in result:
            app.air_quality = result["air_quality"]
        if "uv_index" in result:
            app.uv_index = result["uv_index"]

        app.offline_mode = False
        app.offline_reason = ""
        app.last_refresh = time.time()
        app.next_refresh = time.time() + app.auto_refresh_seconds
        app._save_state()
        app._flash("Updated.", 1.1)
    else:
        app.offline_mode = True
        app.offline_reason = result.get("error", "unknown")
        app.next_refresh = time.time() + 60
        if result.get("allow_offline") and app._load_state():
            app._flash(f"Offline: showing last saved data ({app.offline_reason})", 4.0)
        else:
            app._flash(f"Refresh failed: {app.offline_reason}", 6.0)

    if app.startup_warning:
        app._flash(app.startup_warning, 10.0)
        app.startup_warning = None
