#!/usr/bin/env python3
"""
NWS Weather TUI — NWS API client (weather endpoints + delegation to sub-clients).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from nws_weather_tui.api.air_quality import AirQualityClient
from nws_weather_tui.api.cache import TTLCache
from nws_weather_tui.api.fixtures import (
    FixtureMissError,
    FixtureSession,
    fixture_settings_from_env,
)
from nws_weather_tui.api.geo_boundaries import BoundaryClient
from nws_weather_tui.api.geocode import Geocoder
from nws_weather_tui.api.uv_index import UVIndexClient
from nws_weather_tui.constants import BASE
from nws_weather_tui.radar.client import RadarFetcher


# Concurrent fetches (the weather refresh fans out, and radar frames and the
# favorites dashboard use pools) share one session, so keep-alive connections
# are reused across threads and refreshes instead of every worker thread
# paying for a fresh TLS handshake. The pool is sized for those workers.
SESSION_POOL_SIZE = 16


def _make_session(headers: Dict[str, str]) -> requests.Session:
    s = requests.Session()
    s.headers.update(headers)
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=SESSION_POOL_SIZE, pool_maxsize=SESSION_POOL_SIZE
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def _is_client_error(err: requests.HTTPError) -> bool:
    """A 4xx other than 429: retrying can't help (404, 400, 403...), whereas a
    429 or any 5xx may clear up on the next try."""
    status = getattr(err.response, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status != 429


class NWSClient:
    def __init__(
        self,
        user_agent: str,
        timeout: int,
        ttls: Dict[str, int],
        fixture_dir: Optional[str] = None,
        fixture_mode: str = "replay",
    ) -> None:
        live = _make_session(
            {
                "User-Agent": user_agent,
                "Accept": "application/geo+json, application/json",
            }
        )
        # FIXTURE mode (see api/fixtures.py): an explicit fixture_dir wins,
        # otherwise NWS_TUI_FIXTURE_DIR / NWS_TUI_FIXTURE_MODE switch it on.
        if fixture_dir is None:
            env = fixture_settings_from_env()
            if env:
                fixture_dir, fixture_mode = env["directory"], env["mode"]
        self.fixtures: Optional[FixtureSession] = (
            FixtureSession(fixture_dir, fixture_mode, inner=live) if fixture_dir else None
        )
        self.s = self.fixtures or live
        self.timeout = timeout
        self.ttls = ttls
        self.cache = TTLCache()

        # Sub-clients
        self._radar = RadarFetcher(self.s, timeout, self.cache, ttls)
        self._geocoder = Geocoder(self.s, timeout, self.cache)
        self._boundaries = BoundaryClient(self.s, timeout, self.cache)
        self._air_quality = AirQualityClient(self.s, timeout, self.cache)
        self._uv_index = UVIndexClient(self.s, timeout, self.cache)

    # ------------------------------------------------------------------
    # Core HTTP helpers
    # ------------------------------------------------------------------

    def _get_json(
        self, url: str, ttl: int, tries: int = 3, backoff: float = 0.6
    ) -> Dict[str, Any]:
        cached = self.cache.get(url)
        if cached is not None:
            return cached  # type: ignore[return-value]
        last_err: Optional[Exception] = None
        for i in range(tries):
            try:
                r = self.s.get(url, timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                self.cache.set(url, data, ttl)
                return data
            except FixtureMissError:
                raise  # replaying: a retry can't conjure a recording
            except requests.HTTPError as e:
                last_err = e
                if _is_client_error(e):
                    break  # 4xx: the same request will get the same answer
            except Exception as e:
                last_err = e
            if i < tries - 1:
                time.sleep(backoff * (2 ** i))
        if last_err:
            raise last_err
        return {}

    # ------------------------------------------------------------------
    # NWS API
    # ------------------------------------------------------------------

    def points(self, lat: float, lon: float) -> Dict[str, Any]:
        url = f"{BASE}/points/{lat:.4f},{lon:.4f}"
        return self._get_json(url, ttl=self.ttls["points"])

    def stations(self, stations_url: str) -> Dict[str, Any]:
        return self._get_json(stations_url, ttl=self.ttls["stations"])

    def latest_observation(self, station_id: str) -> Dict[str, Any]:
        url = f"{BASE}/stations/{station_id}/observations/latest"
        return self._get_json(url, ttl=self.ttls["observation"])

    def forecast(self, forecast_url: str, units: str) -> Dict[str, Any]:
        sep = "&" if "?" in forecast_url else "?"
        url = forecast_url + sep + f"units={units}"
        return self._get_json(url, ttl=self.ttls["forecast"])

    def forecast_hourly(self, hourly_url: str, units: str) -> Dict[str, Any]:
        sep = "&" if "?" in hourly_url else "?"
        url = hourly_url + sep + f"units={units}"
        return self._get_json(url, ttl=self.ttls["forecast_hourly"])

    def alerts(self, lat: float, lon: float) -> Dict[str, Any]:
        url = f"{BASE}/alerts/active?point={lat:.4f},{lon:.4f}"
        return self._get_json(url, ttl=self.ttls["alerts"])

    def alerts_area(self, state_code: str) -> Dict[str, Any]:
        """Active alerts anywhere in a state (e.g. "OH")."""
        url = f"{BASE}/alerts/active?area={state_code.strip().upper()}"
        return self._get_json(url, ttl=self.ttls["alerts"])

    def forecast_discussion(self, office_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the latest Area Forecast Discussion (AFD) text product
        issued by a WFO (e.g. "ILN")."""
        return self._latest_text_product(office_id, "AFD", ttl_key="afd")

    def earlier_forecast_discussions(
        self, office_id: str, count: int = 2
    ) -> List[Dict[str, Any]]:
        """The AFDs issued before the latest one, newest first — used to fill
        spare room on the discussion screen."""
        out = []
        for index in range(1, count + 1):
            prod = self._latest_text_product(office_id, "AFD", ttl_key="afd", index=index)
            if prod:
                out.append(prod)
        return out

    def hazardous_weather_outlook(self, office_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the latest Hazardous Weather Outlook (HWO) text product
        issued by a WFO (e.g. "ILN") — a rolling 7-day heads-up on
        potential severe/hazardous weather."""
        return self._latest_text_product(office_id, "HWO", ttl_key="hwo")

    def _latest_text_product(
        self, office_id: str, product_type: str, ttl_key: str, index: int = 0
    ) -> Optional[Dict[str, Any]]:
        """Fetch a text product issued by a WFO; `index` 0 is the latest,
        1 the one before it, and so on."""
        office = (office_id or "").strip().upper()
        if not office:
            return None
        try:
            listing = self._get_json(
                f"{BASE}/products/types/{product_type}/locations/{office}",
                ttl=self.ttls.get(ttl_key, 3600),
            )
            graph = (listing or {}).get("@graph") or []
            if len(graph) <= index:
                return None
            prod_id = (graph[index] or {}).get("id")
            if not isinstance(prod_id, str) or not prod_id:
                return None
            prod = self._get_json(
                f"{BASE}/products/{prod_id}", ttl=self.ttls.get(ttl_key, 3600)
            )
            text = str((prod or {}).get("productText") or "").strip()
            if not text:
                return None
            return {
                "text": text,
                "issuance_time": prod.get("issuanceTime"),
                "office": office,
            }
        except Exception:
            return None

    def forecast_grid_data(self, grid_data_url: str) -> Dict[str, Any]:
        """Fetch raw NWS gridpoint data (quantitative precip, snowfall, etc.)."""
        return self._get_json(grid_data_url, ttl=self.ttls.get("gridpoints", 600))

    # ------------------------------------------------------------------
    # Delegated: air quality
    # ------------------------------------------------------------------

    def air_quality(self, lat: float, lon: float) -> Dict[str, Any]:
        return self._air_quality.current(lat, lon, ttl=self.ttls["air_quality"])

    # ------------------------------------------------------------------
    # Delegated: UV index
    # ------------------------------------------------------------------

    def uv_index(self, lat: float, lon: float) -> Dict[str, Any]:
        return self._uv_index.current(lat, lon, ttl=self.ttls.get("uv_index", 900))

    # ------------------------------------------------------------------
    # Delegated: geocoding
    # ------------------------------------------------------------------

    def geocode_us(self, query: str) -> Optional[Tuple[str, float, float]]:
        return self._geocoder.geocode_us(query)

    # ------------------------------------------------------------------
    # Delegated: radar
    # ------------------------------------------------------------------

    def radar_wms_png(
        self,
        radar_id: str,
        bbox4326: Tuple[float, float, float, float],
        width: int,
        height: int,
    ) -> Optional[bytes]:
        return self._radar.radar_wms_png(radar_id, bbox4326, width, height)

    def radar_frames_png(
        self,
        radar_id: str,
        bbox4326: Tuple[float, float, float, float],
        width: int,
        height: int,
        n_frames: int = 4,
        step_minutes: int = 5,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> List[Tuple[bytes, int]]:
        return self._radar.radar_frames_png(
            radar_id, bbox4326, width, height, n_frames, step_minutes, progress_cb
        )

    # ------------------------------------------------------------------
    # Delegated: boundaries
    # ------------------------------------------------------------------

    def state_lines_png(
        self, bbox4326: Tuple[float, float, float, float], width: int, height: int
    ) -> Tuple[Optional[bytes], Optional[Tuple[float, float, float, float]]]:
        return self._boundaries.state_lines_png(bbox4326, width, height)

    def state_bbox(
        self, state_code: str
    ) -> Optional[Tuple[float, float, float, float]]:
        return self._boundaries.state_bbox(state_code)

    def state_lines_features(
        self,
        bbox4326: Tuple[float, float, float, float],
        state_code: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._boundaries.state_lines_features(bbox4326, state_code)
