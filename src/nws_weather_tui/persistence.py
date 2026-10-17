#!/usr/bin/env python3
"""
NWS Weather TUI — Config and state persistence.

The saved state is the dataclasses in models.py, field by field, so a
field added there is saved and restored without touching this module.
Loading coerces each value by the field's declared type and drops what
doesn't fit, so an older or hand-edited state.json loads what it can
instead of failing as a whole.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import typing
from functools import lru_cache
from typing import Any, Dict, List, Optional, Type, TypeVar, TYPE_CHECKING

from nws_weather_tui.constants import CONFIG_PATH, STATE_PATH, load_json, save_json
from nws_weather_tui.formatting import parse_iso
from nws_weather_tui.models import (
    AirQuality,
    AlertItem,
    CurrentConditions,
    ForecastPeriod,
    HourlyPeriod,
    UVIndex,
)

if TYPE_CHECKING:
    from nws_weather_tui.app import App

T = TypeVar("T")

# Fields left out of the saved state. Alert geometry is large and only the
# radar overlay uses it; the next refresh brings it back.
_UNSAVED: Dict[type, frozenset] = {AlertItem: frozenset({"geometry"})}


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


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def _jsonable(obj: Any) -> Any:
    """`obj` with every datetime, however deep, as an ISO string."""
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(i) for i in obj]
    return obj


def _serialize(item: Any) -> Optional[Dict[str, Any]]:
    if item is None:
        return None
    skip = _UNSAVED.get(type(item), frozenset())
    return _jsonable({
        f.name: getattr(item, f.name)
        for f in dataclasses.fields(item) if f.name not in skip
    })


def build_state_dict(app: "App") -> Dict[str, Any]:
    """Snapshot app state into a plain dict (safe to call on the main thread)."""
    return {
        "saved_at": dt.datetime.now().astimezone().isoformat(),
        "location_name": app.location_name,
        "lat": app.lat,
        "lon": app.lon,
        "units": app.units,
        "use_24h": app.use_24h,
        "current": _serialize(app.current),
        "air_quality": _serialize(app.air_quality),
        "uv_index": _serialize(app.uv_index),
        "forecast_periods": [_serialize(p) for p in app.forecast_periods],
        "hourly_periods": [_serialize(h) for h in app.hourly_periods],
        "alerts": [_serialize(a) for a in app.alerts],
        "radar_station": app.radar_station,
        "state_code": app.state_code,
        "office_id": app.office_id,
        "afd": app.afd,
        "afd_earlier": app.afd_earlier,
        "hwo": app.hwo,
    }


def save_app_state(app: "App") -> None:
    save_json(STATE_PATH, build_state_dict(app))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _base_type(t: Any) -> Any:
    """The concrete type a value must have: Optional[X] -> X, Dict[...] -> dict."""
    origin = typing.get_origin(t)
    if origin is typing.Union:
        args = [a for a in typing.get_args(t) if a is not type(None)]
        return _base_type(args[0]) if len(args) == 1 else Any
    return origin or t


@lru_cache(maxsize=None)
def _field_types(cls: type) -> Dict[str, Any]:
    hints = typing.get_type_hints(cls)
    return {f.name: _base_type(hints.get(f.name, Any)) for f in dataclasses.fields(cls)}


def _coerce(value: Any, base: Any) -> Any:
    """`value` if it fits the field's type (parsing datetimes), else None."""
    if value is None or base is Any:
        return value
    if base is dt.datetime:
        return parse_iso(value) if isinstance(value, str) else None
    if base is bool:
        return value if isinstance(value, bool) else None
    if base in (int, float):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if base is str:
        return value if isinstance(value, str) else str(value)
    return value if isinstance(value, base) else None


def from_dict(cls: Type[T], data: Dict[str, Any]) -> T:
    """Build a model dataclass from a saved dict, field by field: values that
    don't fit their type fall back to the field's default (or '' for text)."""
    types = _field_types(cls)
    kwargs: Dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        value = _coerce(data.get(f.name), types[f.name])
        if value is None:
            if f.default is not dataclasses.MISSING:
                value = f.default
            elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                value = f.default_factory()  # type: ignore[misc]
            elif types[f.name] is str:
                value = ""
        kwargs[f.name] = value
    return cls(**kwargs)


def _list_of(cls: Type[T], items: Any) -> List[T]:
    if not isinstance(items, list):
        return []
    return [from_dict(cls, p) for p in items if isinstance(p, dict)]


def load_app_state(app: "App") -> bool:
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

    for name, cls in (
        ("current", CurrentConditions),
        ("air_quality", AirQuality),
        ("uv_index", UVIndex),
    ):
        data = st.get(name)
        if isinstance(data, dict):
            setattr(app, name, from_dict(cls, data))
    app.forecast_periods = _list_of(ForecastPeriod, st.get("forecast_periods"))
    app.hourly_periods = _list_of(HourlyPeriod, st.get("hourly_periods"))
    app.alerts = _list_of(AlertItem, st.get("alerts"))
    return True
