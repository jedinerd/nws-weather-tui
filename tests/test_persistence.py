"""Config and saved state: what's on disk is what the user gets back.

The saved state is what the app shows when it starts without the network,
so a field that doesn't survive the round trip is missing (or wrong) offline.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from harness.locations import HTTP_FIXTURES_DIR, LOCATIONS


@pytest.fixture
def config_dir(tmp_path: Any, monkeypatch: Any) -> str:
    """Point the config and state paths at a temp dir."""
    from nws_weather_tui import constants, persistence

    d = str(tmp_path / "cfg")
    for mod in (constants, persistence):
        monkeypatch.setattr(mod, "CONFIG_PATH", os.path.join(d, "config.json"), raising=False)
        monkeypatch.setattr(mod, "STATE_PATH", os.path.join(d, "state.json"), raising=False)
    monkeypatch.setattr(constants, "CONFIG_DIR", d)
    return d


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_first_run_writes_defaults(config_dir: str) -> None:
    from nws_weather_tui.constants import DEFAULT_CONFIG, load_config

    cfg, warning = load_config()
    assert warning is None and cfg == DEFAULT_CONFIG
    with open(os.path.join(config_dir, "config.json"), encoding="utf-8") as f:
        assert json.load(f) == DEFAULT_CONFIG


def test_partial_config_keeps_nested_defaults(config_dir: str) -> None:
    from nws_weather_tui.constants import DEFAULT_CONFIG, load_config, save_json

    path = os.path.join(config_dir, "config.json")
    save_json(path, {"units": "si", "radar": {"animation_frames": 3}})
    cfg, warning = load_config()
    assert warning is None
    assert cfg["units"] == "si" and cfg["radar"]["animation_frames"] == 3
    # Keys the file didn't mention, at either level, come from the defaults.
    assert cfg["radar"]["max_city_labels"] == DEFAULT_CONFIG["radar"]["max_city_labels"]
    assert cfg["cache_ttls"] == DEFAULT_CONFIG["cache_ttls"]


@pytest.mark.parametrize("content,reason", [
    ('{"favorites": [{"name": "Home", "lat": 1, "lon": 2},]}', "could not be read"),
    ('["not", "an", "object"]', "is not a JSON object"),
])
def test_broken_config_is_kept_not_overwritten(config_dir: str, content: str, reason: str) -> None:
    """A hand-edit typo must not cost the user their favorites."""
    from nws_weather_tui.constants import DEFAULT_CONFIG, load_config

    path = os.path.join(config_dir, "config.json")
    os.makedirs(config_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    cfg, warning = load_config()
    assert cfg == DEFAULT_CONFIG
    assert warning is not None and reason in warning
    backups = [n for n in os.listdir(config_dir) if n.startswith("config.json.broken-")]
    assert len(backups) == 1 and backups[0] in warning
    with open(os.path.join(config_dir, backups[0]), encoding="utf-8") as f:
        assert f.read() == content  # byte for byte


def test_save_app_config_writes_preferences(config_dir: str) -> None:
    from nws_weather_tui.constants import DEFAULT_CONFIG, load_config
    from nws_weather_tui.persistence import save_app_config

    favorites = [{"name": "Denver, CO", "lat": 39.7392, "lon": -104.9903}]
    app = SimpleNamespace(
        cfg=dict(DEFAULT_CONFIG), location_name="Denver, CO", lat=39.7392, lon=-104.9903,
        units="si", use_24h=True, auto_refresh_seconds=120, timeout=7, hourly_hours=48,
        show_radar_map=False, favorites=favorites,
    )
    save_app_config(app)  # type: ignore[arg-type]
    cfg, warning = load_config()
    assert warning is None
    assert (cfg["location_name"], cfg["units"], cfg["use_24h"], cfg["http_timeout"],
            cfg["hourly_hours"], cfg["show_radar_map"], cfg["favorites"]) == (
        "Denver, CO", "si", True, 7, 48, False, favorites)


# ---------------------------------------------------------------------------
# Saved state
# ---------------------------------------------------------------------------

def _fetched_app(loc: str) -> SimpleNamespace:
    """App-shaped object holding what a refresh of `loc` produces, built
    from the recordings through the real client and parsers."""
    from nws_weather_tui.api.client import NWSClient
    from nws_weather_tui.constants import DEFAULT_CONFIG
    from nws_weather_tui.models import (
        extract_air_quality,
        extract_alerts,
        extract_current,
        extract_forecast,
        extract_grid_series,
        extract_hourly,
        extract_uv_index,
        merge_grid_into_hourly,
    )

    info = LOCATIONS[loc]
    lat, lon = float(info["lat"]), float(info["lon"])  # type: ignore[arg-type]
    client = NWSClient("ua", 5, dict(DEFAULT_CONFIG["cache_ttls"]),
                       fixture_dir=HTTP_FIXTURES_DIR, fixture_mode="replay")
    props = client.points(lat, lon)["properties"]
    station = client.stations(props["observationStations"])["features"][0]
    hourly = extract_hourly(client.forecast_hourly(props["forecastHourly"], "us"))
    merge_grid_into_hourly(hourly, extract_grid_series(
        client.forecast_grid_data(props["forecastGridData"])))
    return SimpleNamespace(
        location_name=info["name"], lat=lat, lon=lon, units="us", use_24h=False,
        current=extract_current(client.latest_observation(
            station["properties"]["stationIdentifier"])),
        air_quality=extract_air_quality(client.air_quality(lat, lon)),
        uv_index=extract_uv_index(client.uv_index(lat, lon)),
        forecast_periods=extract_forecast(client.forecast(props["forecast"], "us")),
        hourly_periods=hourly,
        alerts=extract_alerts(client.alerts(lat, lon)),
        radar_station=props["radarStation"], state_code="CA", office_id=props["cwa"],
        afd=client.forecast_discussion(props["cwa"]),
        afd_earlier=client.earlier_forecast_discussions(props["cwa"]),
        hwo=client.hazardous_weather_outlook(props["cwa"]),
    )


def _blank_app() -> SimpleNamespace:
    return SimpleNamespace(
        location_name="", lat=0.0, lon=0.0, units="us", use_24h=False, current=None,
        air_quality=None, uv_index=None, forecast_periods=[], hourly_periods=[], alerts=[],
        radar_station=None, state_code=None, office_id=None, afd=None, afd_earlier=[], hwo=None,
    )


def _diff(saved: Any, loaded: Any) -> list:
    """Fields that came back different, as 'Class.field: before -> after'."""
    out = []
    for f in dataclasses.fields(saved):
        a, b = getattr(saved, f.name), getattr(loaded, f.name)
        if f.name == "geometry":
            continue  # deliberately not saved (large; only the radar overlay uses it)
        if a != b or type(a) is not type(b):
            out.append(f"{type(saved).__name__}.{f.name}: {a!r} -> {b!r}")
    return out


def test_state_round_trip(config_dir: str) -> None:
    """Everything a refresh produced comes back from state.json with the same
    values and types (a datetime saved must load as a datetime)."""
    from nws_weather_tui.persistence import load_app_state, save_app_state

    saved = _fetched_app("ventura")
    assert saved.alerts and any(a.onset for a in saved.alerts), "need alerts with onset"
    save_app_state(saved)  # type: ignore[arg-type]
    loaded = _blank_app()
    assert load_app_state(loaded) is True  # type: ignore[arg-type]

    problems = []
    for name in ("current", "air_quality", "uv_index"):
        problems += _diff(getattr(saved, name), getattr(loaded, name))
    for name in ("forecast_periods", "hourly_periods", "alerts"):
        before, after = getattr(saved, name), getattr(loaded, name)
        assert len(before) == len(after), name
        for a, b in zip(before, after):
            problems += _diff(a, b)
    for name in ("location_name", "lat", "lon", "radar_station", "office_id",
                 "afd", "afd_earlier", "hwo"):
        if getattr(saved, name) != getattr(loaded, name):
            problems.append(f"{name}: {getattr(saved, name)!r} -> {getattr(loaded, name)!r}")
    unique = sorted({p.split(":")[0] for p in problems})
    assert not problems, f"lost or changed on reload: {unique}\n" + "\n".join(problems[:10])


def test_missing_or_corrupt_state_is_not_loaded(config_dir: str) -> None:
    from nws_weather_tui.persistence import load_app_state

    app = _blank_app()
    assert load_app_state(app) is False  # type: ignore[arg-type]
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "state.json"), "w", encoding="utf-8") as f:
        f.write('{"location_name": "Half a fi')
    assert load_app_state(app) is False  # type: ignore[arg-type]
    assert app.location_name == ""


def test_state_with_wrong_types_loads_what_it_can(config_dir: str) -> None:
    """A state.json from an older version, or hand-edited: bad values are
    dropped field by field rather than failing the whole load."""
    from nws_weather_tui.constants import save_json
    from nws_weather_tui.persistence import load_app_state

    save_json(os.path.join(config_dir, "state.json"), {
        "location_name": "Somewhere", "lat": "not a number", "lon": None,
        "state_code": "California",  # not a two-letter code
        "hourly_periods": [{"start": "2026-10-13T15:00:00-07:00", "temperature": "warm",
                            "pop": 40, "short_forecast": "Sunny"}, "junk"],
        "alerts": [{"event": "Heat Advisory", "sent": 12345, "expires": "garbage"}],
    })
    app = _blank_app()
    assert load_app_state(app) is True  # type: ignore[arg-type]
    assert app.location_name == "Somewhere" and app.lat == 0.0 and app.state_code is None
    (h,) = app.hourly_periods
    assert h.temperature is None and h.pop == 40 and h.start.hour == 15
    (a,) = app.alerts
    assert a.event == "Heat Advisory" and a.sent is None and a.expires is None


def test_from_dict_follows_the_dataclass_fields() -> None:
    """A new model field needs no change in persistence: it is saved and
    restored by name, coerced by its declared type."""
    from nws_weather_tui.persistence import from_dict

    @dataclasses.dataclass
    class Thing:
        name: str
        when: Optional[dt.datetime]
        score: Optional[float]
        flag: Optional[bool] = None
        extra: Optional[Dict[str, Any]] = None
        tags: list = dataclasses.field(default_factory=list)

    t = from_dict(Thing, {"name": 7, "when": "2026-10-13T15:00:00-07:00", "score": 3,
                          "flag": True, "extra": {"a": 1}, "tags": ["x"]})
    assert (t.name, t.when.hour, t.score, t.flag, t.extra, t.tags) == ("7", 15, 3, True, {"a": 1}, ["x"])
    bad = from_dict(Thing, {"when": 12345, "score": "high", "flag": 1, "extra": "no", "tags": "no"})
    assert (bad.name, bad.when, bad.score, bad.flag, bad.extra, bad.tags) == ("", None, None, None, None, [])


def test_alert_geometry_is_not_saved(config_dir: str) -> None:
    """Polygons are large and only the radar overlay draws them; the next
    refresh brings them back."""
    from nws_weather_tui.persistence import load_app_state, save_app_state

    saved = _fetched_app("ventura")
    saved.alerts[0].geometry = {"type": "Polygon", "coordinates": [[[-119.3, 34.3], [-119.2, 34.3],
                                                               [-119.2, 34.2], [-119.3, 34.3]]]}
    save_app_state(saved)  # type: ignore[arg-type]
    with open(os.path.join(config_dir, "state.json"), encoding="utf-8") as f:
        assert "geometry" not in json.load(f)["alerts"][0]
    loaded = _blank_app()
    load_app_state(loaded)  # type: ignore[arg-type]
    assert all(a.geometry is None for a in loaded.alerts)
