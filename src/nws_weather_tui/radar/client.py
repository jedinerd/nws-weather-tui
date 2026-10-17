#!/usr/bin/env python3
"""
NWS Weather TUI — Radar data fetching (MRMS, IEM NEXRAD, station WMS).
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Tuple

from nws_weather_tui.api.cache import TTLCache
from nws_weather_tui.helpers import dbg

MRMS_BASE = (
    "https://mapservices.weather.noaa.gov/eventdriven/rest/services/"
    "radar/radar_base_reflectivity_time/ImageServer"
)


class RadarFetcher:
    def __init__(self, session, timeout: int, cache: TTLCache, ttls: Dict[str, int]) -> None:
        self.s = session
        self.timeout = timeout
        self.cache = cache
        self.ttls = ttls

    # ------------------------------------------------------------------
    # MRMS helpers
    # ------------------------------------------------------------------

    # The ImageServer sits behind several nodes that don't agree: one can
    # report a time extent hours behind the others. Re-ask when the answer
    # looks stale and keep the freshest.
    STALE_EXTENT_S = 30 * 60
    EXTENT_TRIES = 3

    def _mrms_time_extent(self) -> Tuple[Optional[int], Optional[int]]:
        """Return (start_ms, end_ms) of available MRMS time series."""
        ck = MRMS_BASE + ":time_extent"
        cached = self.cache.get(ck)
        if isinstance(cached, tuple):
            return cached  # type: ignore[return-value]
        best: Optional[Tuple[int, int]] = None
        for _ in range(self.EXTENT_TRIES):
            try:
                meta = self.s.get(MRMS_BASE, params={"f": "pjson"}, timeout=self.timeout)
                meta.raise_for_status()
                mj = meta.json()
                te = ((mj or {}).get("timeInfo") or {}).get("timeExtent") or []
                if isinstance(te, list) and len(te) >= 2:
                    out = (int(te[0]), int(te[1]))
                    if best is None or out[1] > best[1]:
                        best = out
            except Exception as e:
                dbg(f"RADAR MRMS time-extent lookup failed: {e}")
            if best is not None and time.time() * 1000 - best[1] < self.STALE_EXTENT_S * 1000:
                break
            if best is not None:
                dbg(f"RADAR MRMS time extent looks stale (ends {best[1]}), asking again")
        if best is None:
            return None, None
        self.cache.set(ck, best, 180)
        return best

    def _mrms_latest_time(self) -> Optional[int]:
        """Fetch the latest available timestamp (ms) from MRMS ImageServer."""
        return self._mrms_time_extent()[1]

    def _fetch_mrms_png(
        self,
        bbox4326: Tuple[float, float, float, float],
        width: int,
        height: int,
        timestamp_ms: Optional[int] = None,
    ) -> Optional[bytes]:
        """Fetch one MRMS base-reflectivity PNG for the given bbox/time."""
        minlon, minlat, maxlon, maxlat = bbox4326
        params: Dict[str, str] = {
            "bbox": f"{minlon:.6f},{minlat:.6f},{maxlon:.6f},{maxlat:.6f}",
            "bboxSR": "4326",
            "imageSR": "4326",
            "size": f"{width},{height}",
            "format": "png32",
            "transparent": "true",
            "adjustAspectRatio": "false",
            "f": "image",
        }
        if timestamp_ms is not None:
            params["time"] = str(timestamp_ms)

        for ep in (MRMS_BASE + "/exportImage", MRMS_BASE + "/export"):
            try:
                r = self.s.get(ep, params=params, timeout=self.timeout * 2)
                r.raise_for_status()
                if isinstance(r.content, (bytes, bytearray)) and r.content.startswith(
                    b"\x89PNG\r\n\x1a\n"
                ):
                    return bytes(r.content)
                dbg(
                    f"RADAR MRMS non-PNG ep={ep} "
                    f"ct={(r.headers.get('Content-Type') or '').lower()} "
                    f"bytes={len(r.content)}"
                )
            except Exception as e:
                dbg(f"RADAR MRMS fetch failed ep={ep} err={e}")
        return None

    # ------------------------------------------------------------------
    # IEM NEXRAD composite (secondary source)
    # ------------------------------------------------------------------

    def _fetch_iem_png(
        self,
        bbox4326: Tuple[float, float, float, float],
        width: int,
        height: int,
    ) -> Optional[bytes]:
        """Fetch NEXRAD composite reflectivity from Iowa State IEM WMS."""
        minlon, minlat, maxlon, maxlat = bbox4326
        try:
            r = self.s.get(
                "https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0r.cgi",
                params={
                    "service": "WMS",
                    "version": "1.1.1",
                    "request": "GetMap",
                    "layers": "nexrad-n0r",
                    "styles": "",
                    "srs": "EPSG:4326",
                    "bbox": f"{minlon},{minlat},{maxlon},{maxlat}",
                    "width": str(width),
                    "height": str(height),
                    "format": "image/png",
                    "transparent": "true",
                },
                timeout=self.timeout * 2,
            )
            r.raise_for_status()
            if r.content.startswith(b"\x89PNG\r\n\x1a\n"):
                return bytes(r.content)
        except Exception as e:
            dbg(f"RADAR IEM fetch failed: {e}")
        return None

    # ------------------------------------------------------------------
    # Station WMS fallback (tertiary source)
    # ------------------------------------------------------------------

    def _fetch_station_wms_png(
        self,
        radar_id: str,
        bbox4326: Tuple[float, float, float, float],
        width: int,
        height: int,
    ) -> Optional[bytes]:
        """Fetch base reflectivity from NWS station OpenGeoServer WMS."""
        minlon, minlat, maxlon, maxlat = bbox4326
        rid = (radar_id or "").strip()
        if not rid:
            return None
        rid_l = rid.lower()
        rid_u = rid.upper()
        bases = [
            f"https://opengeo.ncep.noaa.gov/geoserver/{rid_l}/ows",
            f"https://opengeo.ncep.noaa.gov/geoserver/{rid_u}/ows",
        ]

        discovered_layers: List[str] = []
        for base in bases:
            cap_url = base + "?service=WMS&version=1.1.1&request=GetCapabilities"
            try:
                r = self.s.get(cap_url, timeout=self.timeout)
                r.raise_for_status()
                names = re.findall(r"<Name>([^<]+)</Name>", r.text or "")
                if not names:
                    continue

                def _rank(name: str) -> int:
                    n = name.lower()
                    score = 0
                    if "bref" in n:
                        score += 40
                    if "reflect" in n or "_ref" in n:
                        score += 25
                    if "n0q" in n:
                        score += 20
                    if rid_l in n:
                        score += 12
                    if "qcd" in n:
                        score += 5
                    if "raw" in n:
                        score += 3
                    return score

                ranked = sorted(names, key=_rank, reverse=True)
                discovered_layers.extend(n for n in ranked if _rank(n) > 0)
            except Exception as e:
                dbg(f"RADAR capabilities failed base={base}: {e}")

        layers_try = discovered_layers + [
            f"{rid_l}:{rid_l}_bref_raw",
            f"{rid_l}:{rid_l}_bref_qcd",
            f"{rid_l}:{rid_l}_bref",
            f"{rid_u}:{rid_u}_bref_raw",
            f"{rid_u}:{rid_u}_bref_qcd",
            f"{rid_u}:{rid_u}_bref",
        ]
        seen: set = set()
        unique_layers: list = []
        for layer in layers_try:
            if layer and layer not in seen:
                seen.add(layer)
                unique_layers.append(layer)

        for base in bases:
            for layer in unique_layers:
                params = (
                    f"service=WMS&version=1.1.1&request=GetMap"
                    f"&layers={layer}&styles=&format=image/png&transparent=true"
                    f"&srs=EPSG:4326"
                    f"&bbox={minlon:.6f},{minlat:.6f},{maxlon:.6f},{maxlat:.6f}"
                    f"&width={width}&height={height}"
                )
                url = base + "?" + params
                try:
                    ck = url
                    cached = self.cache.get(ck)
                    if isinstance(cached, (bytes, bytearray)) and cached.startswith(
                        b"\x89PNG\r\n\x1a\n"
                    ):
                        return bytes(cached)
                    r = self.s.get(url, timeout=self.timeout, allow_redirects=True)
                    ct = (r.headers.get("Content-Type") or "").lower()
                    if "vnd.ogc.se_xml" in ct or ("xml" in ct and "image" not in ct):
                        dbg(f"RADAR BAD CT={ct} url={url} status={r.status_code}")
                        continue
                    r.raise_for_status()
                    data = r.content
                    if isinstance(data, (bytes, bytearray)) and data.startswith(
                        b"\x89PNG\r\n\x1a\n"
                    ):
                        self.cache.set(ck, data, self.ttls.get("radar", 300))
                        return bytes(data)
                except Exception as e:
                    dbg(f"RADAR station WMS failed base={base} layer={layer}: {e}")
        return None

    # ------------------------------------------------------------------
    # Public: single-frame
    # ------------------------------------------------------------------

    def radar_wms_png(
        self,
        radar_id: str,
        bbox4326: Tuple[float, float, float, float],
        width: int,
        height: int,
    ) -> Optional[bytes]:
        """Fetch the latest radar PNG. MRMS -> IEM -> station WMS."""
        latest_ms = self._mrms_latest_time()
        if latest_ms is not None:
            latest_ms -= 3 * 60 * 1000  # avoid bleeding-edge unprocessed data
        png = self._fetch_mrms_png(bbox4326, width, height, latest_ms)
        if png:
            return png
        dbg("RADAR MRMS failed, trying IEM")

        png = self._fetch_iem_png(bbox4326, width, height)
        if png:
            return png
        dbg("RADAR IEM failed, trying station WMS")

        png = self._fetch_station_wms_png(radar_id, bbox4326, width, height)
        if png:
            return png

        raise RuntimeError("all radar sources failed (MRMS, IEM, station WMS)")

    # ------------------------------------------------------------------
    # Public: animation frames
    # ------------------------------------------------------------------

    def radar_frames_png(
        self,
        radar_id: str,
        bbox4326: Tuple[float, float, float, float],
        width: int,
        height: int,
        n_frames: int = 8,
        step_minutes: int = 5,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> List[Tuple[bytes, int]]:
        """Fetch multiple MRMS radar frames for animation.

        Frames are independent requests, so they're fetched concurrently
        (bounded pool) rather than one at a time — this is the dominant
        cost of a radar refresh. progress_cb(done, total), if given, is
        called from worker threads as each frame completes.
        """
        t_start, t_end = self._mrms_time_extent()

        if t_end is None:
            t_end = int(time.time() * 1000)
        if t_start is None:
            t_start = t_end - 120 * 60 * 1000  # assume 2 hours available

        # Back off from the bleeding edge — MRMS data needs a few minutes
        # to process, so the very latest timestamp often returns empty.
        t_end -= 3 * 60 * 1000  # 3 minute buffer

        # Shrink step if the available time window can't fit all frames
        span_ms = max(1, t_end - t_start)
        step_ms = step_minutes * 60 * 1000
        if n_frames <= 1:
            step_ms = span_ms
        else:
            needed_ms = (n_frames - 1) * step_ms
            if needed_ms > span_ms:
                step_ms = max(60_000, span_ms // max(1, n_frames - 1))

        timestamps = [t_end - (n_frames - 1 - i) * step_ms for i in range(n_frames)]
        timestamps = [max(t_start, min(t_end, t)) for t in timestamps]
        timestamps = sorted(set(timestamps))

        fetched: Dict[int, bytes] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=min(6, len(timestamps))) as pool:
            futures = {
                pool.submit(self._fetch_mrms_png, bbox4326, width, height, ts_ms): ts_ms
                for ts_ms in timestamps
            }
            for fut in as_completed(futures):
                ts_ms = futures[fut]
                png = fut.result()
                if png:
                    fetched[ts_ms] = png
                else:
                    dbg(f"RADAR animation frame missing ts={ts_ms}")
                done += 1
                if progress_cb is not None:
                    progress_cb(done, len(timestamps))

        frames: List[Tuple[bytes, int]] = [
            (fetched[ts_ms], ts_ms) for ts_ms in timestamps if ts_ms in fetched
        ]

        if not frames:
            try:
                png = self.radar_wms_png(radar_id, bbox4326, width, height)
                if png:
                    frames.append((png, t_end))
            except Exception as e:
                dbg(f"RADAR fallback single frame failed: {e}")

        return frames
