#!/usr/bin/env python3
"""
NWS Weather TUI — Config and state persistence.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, Optional, TYPE_CHECKING

from nws_weather_tui.constants import CONFIG_PATH, STATE_PATH, load_json, save_json
from nws_weather_tui.formatting import parse_iso

if TYPE_CHECKING:
    from nws_weather_tui.app import App


def save_app_config(app: "App") -> None:
    app.cfg.update({
        "location_name": app.location_name,
        "lat": app.lat,
        "lon": app.lon,
        "units": app.units,
        "use_24h": app.use_24h,
        "auto_refresh_seconds": app.auto_refresh_seconds,
        "http_timeout": app.timeout,
        "hourly_hours": app.hourly_hours,
        "show_radar_map": app.show_radar_map,
        "favorites": app.favorites,
    })
    save_json(CONFIG_PATH, app.cfg)


def _serialize_current(app: "App") -> Optional[Dict[str, Any]]:
    if not app.current:
        return None
    d = app.current.__dict__.copy()
    if isinstance(d.get("timestamp"), dt.datetime):
        d["timestamp"] = d["timestamp"].isoformat()
    return d


def _serialize_air_quality(app: "App") -> Optional[Dict[str, Any]]:
    if not app.air_quality:
        return None
    d = app.air_quality.__dict__.copy()
    if isinstance(d.get("timestamp"), dt.datetime):
        d["timestamp"] = d["timestamp"].isoformat()
    return d


def _serialize_uv_index(app: "App") -> Optional[Dict[str, Any]]:
    if not app.uv_index:
        return None
    return app.uv_index.__dict__.copy()


def _fix_dt(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: v.isoformat() if isinstance(v, dt.datetime) else _fix_dt(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_fix_dt(i) for i in obj]
    return obj


def build_state_dict(app: "App") -> Dict[str, Any]:
    """Snapshot app state into a plain dict (safe to call on the main thread)."""
    return _fix_dt({
        "saved_at": dt.datetime.now().astimezone().isoformat(),
        "location_name": app.location_name,
        "lat": app.lat,
        "lon": app.lon,
        "units": app.units,
        "use_24h": app.use_24h,
        "current": _serialize_current(app),
        "air_quality": _serialize_air_quality(app),
        "uv_index": _serialize_uv_index(app),
        "forecast_periods": [p.__dict__ for p in app.forecast_periods],
        "hourly_periods": [h.__dict__ for h in app.hourly_periods],
        "alerts": [a.__dict__ for a in app.alerts],
        "radar_station": app.radar_station,
        "state_code": app.state_code,
        "office_id": app.office_id,
        "afd": app.afd,
        "afd_earlier": app.afd_earlier,
        "hwo": app.hwo,
    })


def save_app_state(app: "App") -> None:
    save_json(STATE_PATH, build_state_dict(app))


def _parse_field(value: Any, field_name: str) -> Any:
    if value is None:
        return None
    if field_name in (
        "start", "end", "sent", "effective", "expires", "onset", "ends", "timestamp",
    ):
        return parse_iso(value) if isinstance(value, str) else None
    if field_name in (
        "temperature", "wind_speed_num", "pop", "humidity_pct",
        "pressure_pa", "visibility_m", "wind_mps", "wind_dir_deg", "gust_mps",
        "heat_index_c", "wind_chill_c", "precip_mm", "snow_mm",
        "dewpoint_c", "apparent_c", "sky_pct", "gust_kmh", "thunder_pct",
    ):
        return value if isinstance(value, (int, float)) else None
    if field_name == "is_daytime":
        return value if isinstance(value, bool) else None
    return value


def load_app_state(app: "App") -> bool:
    from nws_weather_tui.models import (
        AirQuality,
        AlertItem,
        CurrentConditions,
        ForecastPeriod,
        HourlyPeriod,
        UVIndex,
    )

    st = load_json(STATE_PATH)
    if not st:
        return False
    app.location_name = str(st.get("location_name") or app.location_name)
    try:
        app.lat = float(st.get("lat", app.lat))
        app.lon = float(st.get("lon", app.lon))
    except (TypeError, ValueError):
        pass
    app.units = str(st.get("units") or app.units)
    app.use_24h = bool(st.get("use_24h", app.use_24h))
    app.radar_station = st.get("radar_station", "") or None
    sc = st.get("state_code")
    if isinstance(sc, str) and re.fullmatch(r"[A-Za-z]{2}", sc.strip()):
        app.state_code = sc.strip().upper()
    app.office_id = st.get("office_id") or None
    afd = st.get("afd")
    if isinstance(afd, dict) and afd.get("text"):
        app.afd = afd
    earlier = st.get("afd_earlier")
    if isinstance(earlier, list):
        app.afd_earlier = [p for p in earlier if isinstance(p, dict) and p.get("text")]
    hwo = st.get("hwo")
    if isinstance(hwo, dict) and hwo.get("text"):
        app.hwo = hwo

    c = st.get("current")
    if isinstance(c, dict):
        ts = parse_iso(c.get("timestamp")) if isinstance(c.get("timestamp"), str) else None
        try:
            app.current = CurrentConditions(
                station=str(c.get("station") or "\u2014"),
                timestamp=ts,
                temperature_c=c.get("temperature_c"),
                wind_mps=c.get("wind_mps"),
                wind_dir_deg=c.get("wind_dir_deg"),
                gust_mps=c.get("gust_mps"),
                humidity_pct=c.get("humidity_pct"),
                pressure_pa=c.get("pressure_pa"),
                visibility_m=c.get("visibility_m"),
                text_description=str(c.get("text_description") or "\u2014"),
                icon_key=str(c.get("icon_key") or "unknown"),
                heat_index_c=c.get("heat_index_c")
                if isinstance(c.get("heat_index_c"), (int, float)) else None,
                wind_chill_c=c.get("wind_chill_c")
                if isinstance(c.get("wind_chill_c"), (int, float)) else None,
            )
        except Exception:
            app.current = None

    aq = st.get("air_quality")
    if isinstance(aq, dict):
        ts = parse_iso(aq.get("timestamp")) if isinstance(aq.get("timestamp"), str) else None
        try:
            app.air_quality = AirQuality(
                timestamp=ts,
                aqi=aq.get("aqi") if isinstance(aq.get("aqi"), (int, float)) else None,
                category=str(aq.get("category") or "—"),
                primary_pollutant=aq.get("primary_pollutant"),
                pm2_5=aq.get("pm2_5") if isinstance(aq.get("pm2_5"), (int, float)) else None,
                pm10=aq.get("pm10") if isinstance(aq.get("pm10"), (int, float)) else None,
            )
        except Exception:
            app.air_quality = None

    uv = st.get("uv_index")
    if isinstance(uv, dict):
        try:
            app.uv_index = UVIndex(
                current=uv.get("current") if isinstance(uv.get("current"), (int, float)) else None,
                daily_max=uv.get("daily_max") if isinstance(uv.get("daily_max"), (int, float)) else None,
            )
        except Exception:
            app.uv_index = None

    for key, cls, fields in [
        ("forecast_periods", ForecastPeriod, [
            "name", "start", "end", "is_daytime", "temperature",
            "temperature_unit", "wind_speed", "wind_dir",
            "short_forecast", "detailed_forecast", "icon_key", "pop",
        ]),
        ("hourly_periods", HourlyPeriod, [
            "start", "temperature", "temperature_unit", "wind_speed",
            "wind_speed_num", "wind_dir", "short_forecast", "icon_key", "pop",
            "is_daytime", "dewpoint_c", "humidity_pct", "precip_mm", "snow_mm",
            "apparent_c", "sky_pct", "gust_kmh", "thunder_pct",
        ]),
        ("alerts", AlertItem, [
            "event", "severity", "urgency", "certainty", "headline",
            "sent", "effective", "expires", "description", "instruction",
            "area_desc", "id", "onset", "ends",
        ]),
    ]:
        items = st.get(key, [])
        setattr(app, key, [
            cls(**{f: _parse_field(p.get(f), f) for f in fields})
            for p in items if isinstance(p, dict)
        ])
    return True
