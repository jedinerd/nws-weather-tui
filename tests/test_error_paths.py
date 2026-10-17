"""What happens when a server fails: retries, fallbacks, and what reaches the user.

Every recording in fixtures/http is a success, so none of this runs in the
snapshot pass.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Tuple, Union

import pytest
import requests

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32

Answer = Union[Exception, Tuple[int, Union[bytes, str, dict, list]], Callable[[], Any]]


def _response(url: str, status: int, body: Any, content_type: str = "") -> Any:
    from nws_weather_tui.api.fixtures import FixtureResponse

    if isinstance(body, (dict, list)):
        body, content_type = json.dumps(body).encode(), content_type or "application/json"
    elif isinstance(body, str):
        body, content_type = body.encode(), content_type or "text/html"
    else:
        content_type = content_type or "image/png"
    return FixtureResponse(url, status, {"Content-Type": content_type}, body)


class ScriptedSession:
    """Answers by URL substring; a list of answers is used up one per request
    (the last one repeats). Unmatched URLs fail like a dead network."""

    headers = {"User-Agent": "test"}

    def __init__(self, script: Dict[str, Union[Answer, List[Answer]]]) -> None:
        self.script = {k: list(v) if isinstance(v, list) else [v] for k, v in script.items()}
        self.calls: List[str] = []

    def get(self, url: str, params: Any = None, **_: Any) -> Any:
        self.calls.append(url)
        for pattern, answers in self.script.items():
            if pattern in url:
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, Exception):
                    raise answer
                status, body = answer  # type: ignore[misc]
                return _response(url, status, body)
        raise requests.ConnectionError(f"no route to {url}")

    def count(self, pattern: str) -> int:
        return sum(pattern in u for u in self.calls)


@pytest.fixture
def no_sleep(monkeypatch: Any) -> List[float]:
    from nws_weather_tui.api import client

    slept: List[float] = []
    monkeypatch.setattr(client.time, "sleep", slept.append)
    return slept


def _nws(session: ScriptedSession) -> Any:
    from nws_weather_tui.api.client import NWSClient
    from nws_weather_tui.constants import DEFAULT_CONFIG

    c = NWSClient("ua", 5, dict(DEFAULT_CONFIG["cache_ttls"]))
    # The sub-clients hold their own reference to the session, so swap
    # all of them: nothing here may reach the real network.
    for sub in (c, c._radar, c._geocoder, c._boundaries, c._air_quality, c._uv_index):
        sub.s = session
    return c


# ---------------------------------------------------------------------------
# NWS API: retries
# ---------------------------------------------------------------------------

def test_transient_failures_are_retried(no_sleep: List[float]) -> None:
    s = ScriptedSession({"/points/": [requests.Timeout("slow"), (503, "busy"),
                                      (200, {"properties": {"cwa": "BOU"}})]})
    client = _nws(s)
    assert client.points(39.7392, -104.9903)["properties"]["cwa"] == "BOU"
    assert s.count("/points/") == 3 and no_sleep == [0.6, 1.2]  # backing off
    client.points(39.7392, -104.9903)
    assert s.count("/points/") == 3  # the good answer is cached


@pytest.mark.parametrize("answer", [(503, "busy"), (200, "<html>not json</html>"),
                                    requests.ConnectionError("down")])
def test_persistent_failure_raises_after_three_tries(no_sleep: List[float], answer: Any) -> None:
    s = ScriptedSession({"/alerts/": answer})
    with pytest.raises(Exception):
        _nws(s).alerts(39.7392, -104.9903)
    assert s.count("/alerts/") == 3
    assert no_sleep == [0.6, 1.2]  # no pointless wait after the last try
    s.calls.clear()
    with pytest.raises(Exception):
        _nws(s).alerts(39.7392, -104.9903)
    assert s.count("/alerts/") == 3  # a failure is never cached as an answer


@pytest.mark.parametrize("status", [400, 403, 404])
def test_client_errors_are_not_retried(no_sleep: List[float], status: int) -> None:
    """Before: a 404 (a point outside NWS coverage, say) was asked for three
    times with 1.8 s of sleeps in between, and once more after the last."""
    s = ScriptedSession({"/points/": (status, {"detail": "no"})})
    with pytest.raises(requests.HTTPError) as err:
        _nws(s).points(39.7392, -104.9903)
    assert err.value.response.status_code == status
    assert s.count("/points/") == 1 and no_sleep == []


def test_rate_limiting_is_retried(no_sleep: List[float]) -> None:
    s = ScriptedSession({"/points/": [(429, "slow down"), (200, {"properties": {}})]})
    assert _nws(s).points(39.7392, -104.9903) == {"properties": {}}
    assert s.count("/points/") == 2 and no_sleep == [0.6]


@pytest.mark.parametrize("script", [
    {"/products/types/": requests.ConnectionError("down")},
    {"/products/types/": (200, {"@graph": []})},
    {"/products/types/": (200, {"@graph": [{"id": "abc"}]}), "/products/abc": (500, "oops")},
    {"/products/types/": (200, {"@graph": [{"id": "abc"}]}),
     "/products/abc": (200, {"productText": "   "})},
])
def test_text_product_failures_mean_no_product(no_sleep: List[float], script: Any) -> None:
    """AFD and HWO are extras: any failure is 'nothing to show', never an error."""
    client = _nws(ScriptedSession(script))
    assert client.forecast_discussion("BOU") is None
    assert client.hazardous_weather_outlook("BOU") is None
    assert client.earlier_forecast_discussions("BOU") == []


# ---------------------------------------------------------------------------
# Radar: MRMS -> IEM -> station WMS
# ---------------------------------------------------------------------------

BBOX = (-109.1, 37.0, -102.0, 41.0)


def _radar(script: Dict[str, Any]) -> Tuple[Any, ScriptedSession]:
    from nws_weather_tui.api.cache import TTLCache
    from nws_weather_tui.radar.client import RadarFetcher

    s = ScriptedSession(script)
    return RadarFetcher(s, 5, TTLCache(), {"radar": 300}), s


def test_radar_falls_back_to_iem() -> None:
    fetcher, s = _radar({
        "ImageServer/export": (200, "<Error>Invalid time</Error>"),  # MRMS: not a PNG
        "ImageServer": (200, {"timeInfo": {"timeExtent": [0, 1]}}),
        "mesonet.agron.iastate.edu": (200, PNG),
    })
    assert fetcher.radar_wms_png("KFTG", BBOX, 80, 40) == PNG
    assert s.count("ImageServer/export") == 2  # exportImage, then export
    assert s.count("opengeo") == 0  # never got as far as the station WMS


def test_radar_falls_back_to_station_wms() -> None:
    fetcher, s = _radar({
        "ImageServer": requests.ConnectionError("MRMS down"),
        "mesonet.agron.iastate.edu": (500, "IEM down"),
        "request=GetCapabilities": (200, "<Name>kftg:kftg_bref_raw</Name><Name>other</Name>"),
        "request=GetMap": (200, PNG),
    })
    assert fetcher.radar_wms_png("KFTG", BBOX, 80, 40) == PNG
    getmaps = [u for u in s.calls if "GetMap" in u]
    assert "layers=kftg:kftg_bref_raw" in getmaps[0]  # the advertised reflectivity layer first


def test_radar_reports_when_every_source_fails() -> None:
    fetcher, s = _radar({
        "ImageServer": (500, "down"),
        "mesonet.agron.iastate.edu": (500, "down"),
        "opengeo": (200, "<ServiceExceptionReport/>"),
    })
    with pytest.raises(RuntimeError, match="all radar sources failed"):
        fetcher.radar_wms_png("KFTG", BBOX, 80, 40)
    with pytest.raises(RuntimeError):
        fetcher.radar_wms_png("", BBOX, 80, 40)  # no station: the WMS step is skipped


def test_animation_falls_back_to_a_single_frame() -> None:
    import time

    end = int(time.time() * 1000)
    fetcher, s = _radar({
        "/exportImage": (404, "frame gone"),
        "/export": (404, "frame gone"),
        "ImageServer": (200, {"timeInfo": {"timeExtent": [end - 7_200_000, end]}}),
        "mesonet.agron.iastate.edu": (200, PNG),
    })
    frames = fetcher.radar_frames_png("KFTG", BBOX, 80, 40, n_frames=8)
    assert frames == [(PNG, end - 3 * 60_000)]


# ---------------------------------------------------------------------------
# Location search
# ---------------------------------------------------------------------------

def _geocoder(script: Dict[str, Any]) -> Tuple[Any, ScriptedSession]:
    from nws_weather_tui.api.cache import TTLCache
    from nws_weather_tui.api.geocode import Geocoder

    s = ScriptedSession(script)
    return Geocoder(s, 5, TTLCache()), s


ZIP_80202 = {"places": [{"place name": "Denver", "state abbreviation": "CO",
                         "latitude": "39.7491", "longitude": "-104.9946"}]}
NOMINATIM_DENVER = [{"lat": "39.7392", "lon": "-104.9903",
                     "address": {"city": "Denver", "state": "Colorado"}}]


def test_zip_lookup() -> None:
    geo, s = _geocoder({"zippopotam.us/us/80202": (200, ZIP_80202)})
    assert geo.geocode_us("80202-1234") == ("80202 Denver, CO", 39.7491, -104.9946)
    assert geo.geocode_us("80202-1234") is not None and len(s.calls) == 1  # cached


def test_unknown_zip_falls_back_to_place_search() -> None:
    geo, s = _geocoder({"zippopotam.us": (404, {}),
                        "nominatim": (200, NOMINATIM_DENVER)})
    assert geo.geocode_us("80202") == ("Denver, Colorado", 39.7392, -104.9903)
    assert s.count("zippopotam") == 1 and s.count("nominatim") == 1


@pytest.mark.parametrize("address,want", [
    ({"town": "Estes Park", "state": "Colorado"}, "Estes Park, Colorado"),
    ({"county": "Larimer County"}, "Larimer County"),
    ({"state": "Colorado"}, "Colorado"),
    ({}, "Somewhere, USA"),
])
def test_place_names(address: Dict[str, str], want: str) -> None:
    hit = [{"lat": "40", "lon": "-105", "address": address, "display_name": "Somewhere, USA"}]
    geo, _ = _geocoder({"nominatim": (200, hit)})
    assert geo.geocode_us("q")[0] == want


def test_no_match_is_none() -> None:
    geo, s = _geocoder({"nominatim": (200, [])})
    assert geo.geocode_us("Atlantis") is None
    assert geo.geocode_us("   ") is None and len(s.calls) == 1  # blank: no request at all


def test_network_failure_is_not_reported_as_no_match() -> None:
    """A dropped connection must surface as an error (the search prompt then
    says 'Location search failed: ...'), not as 'No match found', which
    tells the user the place doesn't exist. The next try goes back out."""
    geo, s = _geocoder({"nominatim": [requests.ConnectionError("offline"),
                                      (200, NOMINATIM_DENVER)]})
    with pytest.raises(requests.ConnectionError):
        geo.geocode_us("Denver")
    assert geo.geocode_us("Denver") == ("Denver, Colorado", 39.7392, -104.9903)
    assert s.count("nominatim") == 2


def test_no_match_is_remembered() -> None:
    """A miss is cached for a while; before, only hits were (a cached None
    read as 'not cached'), so every retype of a bad name went back out."""
    geo, s = _geocoder({"nominatim": (200, [])})
    assert geo.geocode_us("Atlantis") is None
    assert geo.geocode_us("Atlantis") is None
    assert s.count("nominatim") == 1


# ---------------------------------------------------------------------------
# State boundaries
# ---------------------------------------------------------------------------

def test_unknown_state_bbox_is_remembered() -> None:
    """The bbox lookup is several requests; a state the map service doesn't
    know must not be asked again on every radar refresh."""
    from nws_weather_tui.api.cache import TTLCache
    from nws_weather_tui.api.geo_boundaries import BoundaryClient

    s = ScriptedSession({"MapServer/3/query": (200, {"extent": {}}),  # matched first
                         "MapServer/3": (200, {"fields": [{"name": "STUSPS"}]})})
    client = BoundaryClient(s, 5, TTLCache())
    assert client.state_bbox("XX") is None
    first = len(s.calls)
    assert first >= 2
    assert client.state_bbox("XX") is None
    assert len(s.calls) == first


# ---------------------------------------------------------------------------
# The weather refresh: one thread fans out the independent requests
# ---------------------------------------------------------------------------

_CORE = {
    "/observations/latest": (200, {"properties": {"temperature": {"value": 12.0},
                                                  "textDescription": "Clear"}}),
    "https://x/fc": (200, {"properties": {"periods": [
        {"name": "Today", "isDaytime": True, "temperature": 60, "shortForecast": "Sunny"}]}}),
    "https://x/hr": (200, {"properties": {"periods": [
        {"startTime": "2026-10-16T12:00:00-04:00", "temperature": 55,
         "shortForecast": "Sunny"}]}}),
    "/alerts/active?point": (200, {"features": []}),
}


def _refresh(session: ScriptedSession) -> Dict[str, Any]:
    """Run _bg_weather_fetch against `session` with the point already known
    and return what it leaves for the main thread."""
    import threading
    from types import SimpleNamespace

    from nws_weather_tui.weather_refresh import _bg_weather_fetch

    app = SimpleNamespace(client=_nws(session), _bg_lock=threading.Lock(),
                          _bg_weather_pending=None)
    ctx = {
        "lat": 39.9612, "lon": -82.9988, "units": "us",
        "points_data": {"properties": {}}, "forecast_url": "https://x/fc",
        "hourly_url": "https://x/hr", "stations_url": "https://x/stations",
        "station_id": "KCMH", "hourly_hours": 0, "office_id": "ILN",
        "grid_data_url": "https://x/grid", "state_code": "OH", "allow_offline": True,
    }
    _bg_weather_fetch(app, ctx, 0)
    return app._bg_weather_pending


def test_refresh_collects_every_source(no_sleep: List[float]) -> None:
    """The core answers arrive; the extras (gridpoint data, area alerts,
    AFD/HWO, air quality, UV) are all unreachable and just come back empty."""
    result = _refresh(ScriptedSession(dict(_CORE)))
    assert result["ok"] is True
    assert result["current"].temperature_c == 12.0
    assert [p.name for p in result["forecast_periods"]] == ["Today"]
    assert len(result["hourly_periods"]) == 1
    assert result["hourly_periods"][0].precip_mm is None  # gridpoint fetch failed quietly
    assert result["alerts"] == [] and result["area_alerts"] == []
    assert result["afd"] is None and result["afd_earlier"] == [] and result["hwo"] is None
    assert result["air_quality"] is None and result["uv_index"] is None


def test_refresh_goes_offline_when_a_core_fetch_fails(no_sleep: List[float]) -> None:
    script = dict(_CORE)
    script["https://x/fc"] = (503, "busy")
    result = _refresh(ScriptedSession(script))
    assert result["ok"] is False
    assert result["error"].startswith("HTTPError") and result["allow_offline"] is True
    assert "current" not in result  # nothing half-applied


def test_refresh_requests_overlap(no_sleep: List[float]) -> None:
    """A dozen requests at 0.15 s each took ~2 s one after another; fanned
    out, the refresh takes about as long as its slowest chain."""
    import time

    class SlowSession(ScriptedSession):
        def get(self, url: str, params: Any = None, **kw: Any) -> Any:
            time.sleep(0.15)
            return super().get(url, params, **kw)

    s = SlowSession(dict(_CORE))
    t0 = time.monotonic()
    result = _refresh(s)
    elapsed = time.monotonic() - t0
    assert result["ok"] is True
    assert len(s.calls) >= 10, s.calls
    assert elapsed < 0.15 * len(s.calls) / 2, f"{elapsed:.2f}s for {len(s.calls)} requests"
