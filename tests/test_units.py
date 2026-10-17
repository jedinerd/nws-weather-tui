"""Unit tests for the fixture client and the specific bugs fixed alongside the harness."""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from harness.locations import HTTP_FIXTURES_DIR
from harness.recordings import records


# ---------------------------------------------------------------------------
# FIXTURE mode
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, content: bytes, content_type: str, status: int = 200) -> None:
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": content_type}


class _FakeSession:
    headers = {"User-Agent": "test"}

    def __init__(self, responses: Dict[str, _FakeResponse]) -> None:
        self.responses = responses
        self.calls: List[str] = []

    def get(self, url: str, params: Optional[Dict[str, Any]] = None, **_: Any) -> _FakeResponse:
        from nws_weather_tui.api.fixtures import canonical_url

        key = canonical_url(url, params)
        self.calls.append(key)
        return self.responses[key]


def test_canonical_url_ignores_param_order() -> None:
    from nws_weather_tui.api.fixtures import canonical_url

    a = canonical_url("https://x/q", {"b": 2, "a": "x y"})
    b = canonical_url("https://x/q", {"a": "x y", "b": 2})
    assert a == b == "https://x/q?a=x+y&b=2"
    assert canonical_url("https://x/q?z=1", {"a": 1}) == "https://x/q?z=1&a=1"


def test_record_then_replay_round_trip(tmp_path: Any) -> None:
    import requests

    from nws_weather_tui.api.fixtures import FixtureSession

    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    inner = _FakeSession({
        "https://x/a.json": _FakeResponse(b'{"k": [1, 2]}', "application/geo+json"),
        "https://x/b.png": _FakeResponse(png, "image/png"),
        "https://x/c?f=1": _FakeResponse(b"<Name>x</Name>", "text/xml", status=404),
    })
    rec = FixtureSession(str(tmp_path), "record", inner=inner)
    rec.get("https://x/a.json")
    rec.get("https://x/b.png")
    rec.get("https://x/c", params={"f": 1})
    rec.get("https://x/a.json")  # already recorded: served from disk
    assert inner.calls.count("https://x/a.json") == 1

    rep = FixtureSession(str(tmp_path), "replay")
    assert rep.get("https://x/a.json").json() == {"k": [1, 2]}
    assert rep.get("https://x/b.png").content == png
    r = rep.get("https://x/c", params={"f": 1})
    assert r.status_code == 404 and not r.ok and r.text == "<Name>x</Name>"
    with pytest.raises(requests.HTTPError):
        r.raise_for_status()


def test_replay_miss_fails_fast_as_offline(tmp_path: Any) -> None:
    """A missing recording behaves like being offline, without retry sleeps."""
    import requests

    from nws_weather_tui.api.client import NWSClient
    from nws_weather_tui.api.fixtures import FixtureMissError
    from nws_weather_tui.constants import DEFAULT_CONFIG

    client = NWSClient("ua", 5, dict(DEFAULT_CONFIG["cache_ttls"]),
                       fixture_dir=str(tmp_path), fixture_mode="replay")
    start = time.monotonic()
    with pytest.raises(requests.ConnectionError) as exc:
        client.points(39.7392, -104.9903)
    assert isinstance(exc.value, FixtureMissError)
    assert time.monotonic() - start < 0.5  # _get_json's 0.6 s+ backoff skipped
    assert client.fixtures is not None and len(client.fixtures.misses) == 1


def test_fixture_mode_from_environment(monkeypatch: Any) -> None:
    from nws_weather_tui.api.client import NWSClient
    from nws_weather_tui.constants import DEFAULT_CONFIG

    monkeypatch.setenv("NWS_TUI_FIXTURE_DIR", HTTP_FIXTURES_DIR)
    monkeypatch.delenv("NWS_TUI_FIXTURE_MODE", raising=False)
    client = NWSClient("ua", 5, dict(DEFAULT_CONFIG["cache_ttls"]))
    assert client.fixtures is not None and client.fixtures.mode == "replay"
    data = client.points(39.7392, -104.9903)
    assert data["properties"]["cwa"] == "BOU"

    monkeypatch.delenv("NWS_TUI_FIXTURE_DIR")
    assert NWSClient("ua", 5, dict(DEFAULT_CONFIG["cache_ttls"])).fixtures is None


# ---------------------------------------------------------------------------
# Text products: HWO zone segments and inline section headers
# ---------------------------------------------------------------------------

def test_ugc_zone_expansion() -> None:
    from nws_weather_tui.text_product import ugc_zones

    header = "INZ050-058-073>075-KYZ089>091-OHZ060-\n088-140800-"
    assert ugc_zones(header) == {
        "INZ050", "INZ058", "INZ073", "INZ074", "INZ075",
        "KYZ089", "KYZ090", "KYZ091", "OHZ060", "OHZ088",
    }


def _iln_hwo() -> str:
    for rec in records():
        body = rec.get("json") or {}
        if body.get("productCode") == "HWO" and "HWOILN" in body.get("productText", ""):
            return str(body["productText"])
    pytest.fail("no ILN HWO in the recording: Columbus must stay a covered location")
    return ""


def test_hwo_shows_only_the_segment_for_the_point() -> None:
    from nws_weather_tui.text_product import parse_sections, segment_for_zones

    raw = _iln_hwo()
    # Fail rather than skip: after a re-record this test must not quietly stop
    # running. If ILN issued a single-segment HWO that day, re-record later.
    assert raw.count("$$") >= 2, "this recording's ILN HWO has a single segment"
    columbus = segment_for_zones(raw, ["OHZ055", "OHC049"])  # Franklin County, OH
    assert "OHZ" not in columbus  # UGC header lines removed
    titles = [t for t, _ in parse_sections(columbus)]
    assert titles[:2] == ["DAY ONE", "DAYS TWO THROUGH SEVEN"]
    assert titles.count("SPOTTER INFORMATION STATEMENT") == 1
    # A zone in no segment: everything is kept, still without UGC lines.
    everywhere = segment_for_zones(raw, ["ZZZ999"])
    assert "OHZ" not in everywhere and everywhere.count(".DAY ONE...") == raw.count(".DAY ONE...")


def test_inline_section_headers_keep_their_text() -> None:
    from nws_weather_tui.text_product import parse_sections

    raw = (
        "000\nFLUS43 KBOU 131000\nHWOBOU\n\n"
        ".DAY ONE...Today and tonight.\n\nNo hazardous weather is expected.\n\n"
        ".DAYS TWO THROUGH SEVEN...Wednesday through Monday.\n\nSnow Thursday.\n\n"
        ".SPOTTER INFORMATION STATEMENT...\n\nSpotter activation is not expected.\n\n$$\n"
    )
    assert parse_sections(raw) == [
        ("DAY ONE", "Today and tonight.\n\nNo hazardous weather is expected."),
        ("DAYS TWO THROUGH SEVEN", "Wednesday through Monday.\n\nSnow Thursday."),
        ("SPOTTER INFORMATION STATEMENT", "Spotter activation is not expected."),
    ]


def test_single_segment_product_unchanged() -> None:
    from nws_weather_tui.text_product import segment_for_zones

    raw = "AFD\n.SYNOPSIS...\nHigh pressure.\n$$\n"
    assert segment_for_zones(raw, ["OHZ055"]) == raw


# ---------------------------------------------------------------------------
# Alerts: the hazard's window, not the message expiry
# ---------------------------------------------------------------------------

def _alert(**kw: Any) -> Any:
    from nws_weather_tui.models import AlertItem

    base = dict(event="Red Flag Warning", severity="Severe", urgency="Expected",
                certainty="Likely", headline="h", sent=None, effective=None,
                expires=None, description="", instruction="")
    base.update(kw)
    return AlertItem(**base)


def test_alert_until_prefers_ends() -> None:
    utc = dt.timezone.utc
    expires = dt.datetime(2026, 10, 14, 22, tzinfo=utc)
    ends = dt.datetime(2026, 10, 17, 5, tzinfo=utc)
    assert _alert(expires=expires, ends=ends).until == ends
    assert _alert(expires=expires).until == expires


def test_extract_alerts_reads_onset_and_ends() -> None:
    from nws_weather_tui.models import extract_alerts

    alerts = extract_alerts({"features": [{"properties": {
        "event": "Freeze Warning", "severity": "Severe",
        "expires": "2026-10-13T20:30:00-06:00", "ends": "2026-10-15T08:00:00-06:00",
        "onset": "2026-10-14T00:00:00-06:00",
    }}]})
    assert alerts[0].until.isoformat() == "2026-10-15T08:00:00-06:00"
    assert alerts[0].onset.isoformat() == "2026-10-14T00:00:00-06:00"


def test_alert_window_text(monkeypatch: Any) -> None:
    import freezegun

    from nws_weather_tui.views.alerts import alert_window

    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        a = _alert(sent=dt.datetime(2026, 10, 13, 21, 38, tzinfo=dt.timezone.utc),
                   onset=dt.datetime(2026, 10, 15, 13, tzinfo=dt.timezone.utc),
                   ends=dt.datetime(2026, 10, 17, 5, tzinfo=dt.timezone.utc))
        with freezegun.freeze_time("2026-10-13T22:00:00Z"):
            assert alert_window(a, False) == (
                "From Thu Oct 15, 1:00pm until Sat Oct 17, 5:00am · Sent Tue Oct 13, 9:38pm")
        with freezegun.freeze_time("2026-10-16T00:00:00Z"):  # already under way
            assert alert_window(a, False).startswith("Until Sat Oct 17, 5:00am")
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


# ---------------------------------------------------------------------------
# Radar
# ---------------------------------------------------------------------------

def test_radar_age_tag() -> None:
    from nws_weather_tui.views.radar import radar_age_tag

    assert radar_age_tag(5 * 60) == ""
    assert radar_age_tag(25 * 60) == "  (25m old)"
    assert radar_age_tag(3 * 3600 + 43 * 60) == "  (3h 43m old)"


def _extent(start_ms: int, end_ms: int) -> _FakeResponse:
    body = {"timeInfo": {"timeExtent": [start_ms, end_ms]}}
    return _FakeResponse(json.dumps(body).encode(), "application/json")


class _SequenceSession(_FakeSession):
    """Answers the MRMS metadata URL with each queued response in turn."""

    def __init__(self, answers: List[_FakeResponse]) -> None:
        super().__init__({})
        self.answers = answers

    def get(self, url: str, params: Optional[Dict[str, Any]] = None, **_: Any) -> Any:
        self.calls.append(url)
        r = self.answers.pop(0)
        r.json = lambda r=r: json.loads(r.content)  # type: ignore[attr-defined]
        r.raise_for_status = lambda: None  # type: ignore[attr-defined]
        return r


def test_mrms_stale_extent_is_asked_again() -> None:
    from nws_weather_tui.api.cache import TTLCache
    from nws_weather_tui.radar.client import RadarFetcher

    now_ms = int(time.time() * 1000)
    stale = _extent(now_ms - 6 * 3600_000, now_ms - 4 * 3600_000)
    fresh = _extent(now_ms - 2 * 3600_000, now_ms - 3 * 60_000)
    session = _SequenceSession([stale, fresh, stale])
    fetcher = RadarFetcher(session, 5, TTLCache(), {})
    assert fetcher._mrms_time_extent()[1] == now_ms - 3 * 60_000
    assert len(session.calls) == 2  # stopped once it had a fresh answer
    assert fetcher._mrms_latest_time() == now_ms - 3 * 60_000  # cached, no new call
    assert len(session.calls) == 2


def test_mrms_gives_up_after_three_stale_answers() -> None:
    from nws_weather_tui.api.cache import TTLCache
    from nws_weather_tui.radar.client import RadarFetcher

    now_ms = int(time.time() * 1000)
    answers = [_extent(0, now_ms - h * 3600_000) for h in (5, 4, 6)]
    session = _SequenceSession(answers)
    assert RadarFetcher(session, 5, TTLCache(), {})._mrms_time_extent()[1] == now_ms - 4 * 3600_000
    assert len(session.calls) == 3


class _RadarApp:
    """The attributes maybe_refresh_radar reads."""

    def __init__(self) -> None:
        from nws_weather_tui.radar.state import RadarState

        self.show_radar_map = True
        self.radar = RadarState()
        self._bg_radar_running = False
        self.cfg = {"cache_ttls": {"radar": 300}}
        self.radar.last = time.time()
        self.radar.cells: List[Any] = []
        self.radar.ascii_lines: List[str] = []
        self.radar.src_cols, self.radar.src_rows = 80, 20
        self.radar.bbox_state = "CO"
        self.state_code = "CO"
        self.radar.err: Optional[str] = "all radar sources failed"
        # What starting a fetch reads.
        self.lat, self.lon = 39.74, -104.99
        self.radar_station = "KFTG"
        self.alerts: List[Any] = []
        self.radar.has_256color = True
        self.radar.anim_playing = False
        self._bg_generation = 0
        self.client = type("C", (), {"state_bbox": lambda _s, _c: (-109.1, 37.0, -102.0, 41.0)})()
        # What a finished fetch (apply_bg_radar) writes.
        self._bg_lock = threading.Lock()
        self._bg_radar_pending: Optional[Dict[str, Any]] = None
        self._bg_weather_running = False
        self._is_loading = False
        self.status_msg, self.status_until = "", 0.0
        self.radar.bbox: Optional[Any] = None
        self.radar.frames: List[Any] = []
        self.radar.frame_idx = 0
        self.radar.kind_lines: List[str] = []
        self.radar.ts_ms, self.radar.ts_utc = 0, ""
        self.radar.state_overlay: List[str] = []
        self.radar.city_overlay: List[str] = []
        self.radar.alert_overlay: List[str] = []
        self.radar.city_hitmap: Dict[Any, Any] = {}
        self.radar.progress = (0, 0)
        self.flashed: List[str] = []

    def _flash(self, msg: str, seconds: float = 2.0) -> None:
        self.flashed.append(msg)


class _RadarClient:
    """Counts bbox lookups; radar frames come from `frames` or raise `error`."""

    STATE_BBOX = (-109.1, 37.0, -102.0, 41.0)

    def __init__(self, frames: Optional[List[Any]] = None, error: Optional[str] = None) -> None:
        self.frames, self.error = frames or [], error
        self.bbox_calls: List[str] = []
        self.requests: List[Any] = []

    def state_bbox(self, state_code: str) -> Any:
        self.bbox_calls.append(state_code)
        return self.STATE_BBOX

    def radar_frames_png(self, radar_id: str, bbox: Any, width: int, height: int, **_: Any) -> Any:
        self.requests.append((radar_id, bbox, width, height))
        if self.error:
            raise RuntimeError(self.error)
        return list(self.frames)


def _capture_threads(monkeypatch: Any) -> List[Any]:
    """Replace Thread so a test can run the worker itself, on this thread."""
    from nws_weather_tui.radar import state

    started: List[Any] = []

    class Thread:
        def __init__(self, target: Any, args: Any, daemon: bool) -> None:
            self.run = lambda: target(*args)

        def start(self) -> None:
            started.append(self.run)

    monkeypatch.setattr(state.threading, "Thread", Thread)
    return started


def _fresh_radar_app(client: Any) -> _RadarApp:
    """An app that has never fetched radar for its current location."""
    app = _RadarApp()
    app.client = client
    app.radar.err = None
    app.radar.last = 0.0
    app.radar.src_cols = app.radar.src_rows = 0
    app.radar.bbox_state = None
    app.cfg["radar"] = {"show_state_lines": False, "show_city_labels": False,
                        "show_alert_polygons": False, "animation_frames": 1}
    return app


def test_radar_bbox_lookup_runs_on_the_worker(monkeypatch: Any) -> None:
    """Before: the draw path looked the state bbox up synchronously, up to
    six HTTP requests on the UI thread after every location change."""
    from nws_weather_tui.radar import state
    from nws_weather_tui.geo import expand_bbox_km

    monkeypatch.setattr(state, "save_radar_png", lambda png: None)
    workers = _capture_threads(monkeypatch)
    client = _RadarClient(frames=[(_blank_png(), 1_700_000_000_000)])
    app = _fresh_radar_app(client)

    state.maybe_refresh_radar(app, 80, 20)  # type: ignore[arg-type]
    assert app._bg_radar_running and len(workers) == 1
    assert client.bbox_calls == []  # nothing fetched on the calling thread
    assert app.radar.bbox is None and app.radar.src_cols == 0

    workers[0]()  # the worker: bbox lookup, fit to the panel, frames
    assert client.bbox_calls == ["CO"]
    expected, req_w, req_h = state.fit_bbox_to_panel(
        expand_bbox_km(client.STATE_BBOX, 20.0), 80, 20
    )
    assert client.requests == [("KFTG", expected, req_w, req_h)]

    state.apply_bg_radar(app)  # type: ignore[arg-type]
    assert not app._bg_radar_running and app.radar.err is None
    assert app.radar.bbox == expected and app.radar.bbox_state == "CO"
    assert (app.radar.src_cols, app.radar.src_rows) == (80, 20)
    assert len(app.radar.frames) == 1 and app.radar.cells
    state.maybe_refresh_radar(app, 80, 20)  # type: ignore[arg-type]
    assert len(workers) == 1  # same view, fresh frames: nothing to do


def test_failed_background_fetch_still_backs_off(monkeypatch: Any) -> None:
    """A failure records the view it was for, so the retry wait applies."""
    from nws_weather_tui.radar import state

    workers = _capture_threads(monkeypatch)
    app = _fresh_radar_app(_RadarClient(error="all radar sources failed"))

    state.maybe_refresh_radar(app, 80, 20)  # type: ignore[arg-type]
    workers[0]()
    state.apply_bg_radar(app)  # type: ignore[arg-type]
    assert app.radar.err == "all radar sources failed" and app.radar.frames == []
    assert app.radar.bbox_state == "CO" and (app.radar.src_cols, app.radar.src_rows) == (80, 20)

    for _ in range(5):
        state.maybe_refresh_radar(app, 80, 20)  # type: ignore[arg-type]
    assert len(workers) == 1
    app.radar.last -= state.RADAR_RETRY_S + 1
    state.maybe_refresh_radar(app, 80, 20)  # type: ignore[arg-type]
    assert len(workers) == 2


def _blank_png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def test_failed_radar_fetch_backs_off(monkeypatch: Any) -> None:
    """Before: every redraw after a failure started another full fetch."""
    from nws_weather_tui.radar import state

    started: List[Any] = []

    class Thread:
        def __init__(self, target: Any, args: Any, daemon: bool) -> None:
            pass

        def start(self) -> None:
            started.append(1)

    monkeypatch.setattr(state.threading, "Thread", Thread)
    app = _RadarApp()
    for _ in range(5):  # five redraws in quick succession
        state.maybe_refresh_radar(app, 80, 20)  # type: ignore[arg-type]
    assert started == []
    app.radar.last -= state.RADAR_RETRY_S + 1
    state.maybe_refresh_radar(app, 80, 20)  # type: ignore[arg-type]
    assert started == [1] and app._bg_radar_running


@pytest.mark.parametrize("dx,dy", [
    (7, 5), (5, 7), (-7, 5), (-5, 7), (7, -5), (5, -7), (-7, -5), (-5, -7),  # every octant
    (7, 0), (0, -7), (6, 6), (-6, 6), (0, 0),                               # axes, diagonals, a point
])
def test_line_raster_is_four_connected(dx: int, dy: int) -> None:
    """Every direction: the line joins its endpoints through cells that share
    an edge, never only a corner, and stays a thin line (no extra cells)."""
    from nws_weather_tui.radar.overlays import _draw_segment

    x0, y0 = 8, 8
    grid = [[" "] * 17 for _ in range(17)]
    _draw_segment(grid, x0, y0, x0 + dx, y0 + dy, "#")
    cells = {(x, y) for y, row in enumerate(grid) for x, ch in enumerate(row) if ch == "#"}
    start, end = (x0, y0), (x0 + dx, y0 + dy)
    assert start in cells and end in cells
    seen, todo = {start}, [start]
    while todo:  # flood fill through edge neighbours only
        x, y = todo.pop()
        for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if n in cells and n not in seen:
                seen.add(n)
                todo.append(n)
    assert end in seen, f"corner-only step between {start} and {end}"
    assert seen == cells, "stray cells off the line"
    assert len(cells) == abs(dx) + abs(dy) + 1  # the shortest 4-connected path


def test_line_raster_clips_to_the_grid() -> None:
    from nws_weather_tui.radar.overlays import _draw_segment

    grid = [[" "] * 5 for _ in range(4)]
    _draw_segment(grid, -3, 1, 9, 2, "#")  # starts and ends off the grid
    assert [len(row) for row in grid] == [5] * 4
    assert "".join(grid[1]) + "".join(grid[2]) != " " * 10


def test_city_labels_never_touch() -> None:
    from nws_weather_tui.radar.overlays import city_overlay_and_hits_for_bbox

    for cols, rows in ((40, 6), (80, 12), (160, 40)):
        lines, hits = city_overlay_and_hits_for_bbox(
            cols, rows, (-125.0, 24.0, -66.0, 50.0), max_labels=60)
        for line in lines:
            for word in line.split():
                assert word.count("@") == 1 and len(word.strip("@")) in (3, 4), (cols, line)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def test_dashboard_missing_wind_is_not_calm(monkeypatch: Any) -> None:
    import curses

    from nws_weather_tui.models import CurrentConditions
    from nws_weather_tui.views import dashboard

    monkeypatch.setattr(curses, "color_pair", lambda n: n << 8)

    class App:
        units = "us"

    def row(wind: Optional[float]) -> str:
        c = CurrentConditions(
            station="KCMH", timestamp=None, temperature_c=20.0, wind_mps=wind,
            wind_dir_deg=None, gust_mps=None, humidity_pct=None, pressure_pa=None,
            visibility_m=None, text_description="Clear", icon_key="clear")
        return dashboard._row(App(), {"current": c, "ts": time.time()})["wind"][0]  # type: ignore[arg-type]

    assert row(None) == "—"
    assert row(0.0) == "calm"
    assert row(4.47) == "10 mph"


# ---------------------------------------------------------------------------
# TTLCache
# ---------------------------------------------------------------------------

def test_cache_tells_a_cached_none_from_a_miss() -> None:
    from nws_weather_tui.api.cache import MISSING, TTLCache

    c = TTLCache()
    assert c.get("k") is None and c.get("k", MISSING) is MISSING
    c.set("k", None, 600)
    assert c.get("k") is None and c.get("k", MISSING) is None  # a remembered miss
    c.set("k", None, 0)  # expired at once
    assert c.get("k", MISSING) is MISSING
    c.set("k", "v", 600)
    c.clear()
    assert c.get("k", MISSING) is MISSING


def test_cache_survives_concurrent_use() -> None:
    """The main loop, the weather thread and the radar/dashboard pools share
    one cache. Before: a sweep iterating the dict while another thread
    inserted raised 'dictionary changed size during iteration'."""
    from nws_weather_tui.api.cache import TTLCache

    c = TTLCache()
    c._SWEEP_INTERVAL = 1  # sweep (iterate the whole dict) on every set
    errors: List[BaseException] = []
    stop = threading.Event()

    def writer(tag: str) -> None:
        try:
            i = 0
            while not stop.is_set() and i < 5_000:
                c.set(f"{tag}:{i}", i, 0 if i % 2 else 600)  # half expire at once
                c.get(f"{tag}:{i - 1}")
                if i % 2_500 == 0:
                    c.clear()
                i += 1
        except BaseException as e:  # noqa: BLE001 - the failure under test
            errors.append(e)
            stop.set()

    threads = [threading.Thread(target=writer, args=(t,)) for t in "abcd"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []


# ---------------------------------------------------------------------------
# Radar drawing: runs of equal attributes go out as one addstr each
# ---------------------------------------------------------------------------

class _Canvas:
    """Records what addstr paints, cell by cell, whatever the call shape."""

    def __init__(self, cols: int) -> None:
        self.cols = cols
        self.cells: Dict[int, tuple] = {}
        self.calls = 0

    def addstr(self, y: int, x: int, s: str, attr: int = 0) -> None:
        self.calls += 1
        for i, ch in enumerate(s):
            if x + i >= self.cols:
                raise Exception("off the edge")
            self.cells[x + i] = (ch, attr)


def test_halfblock_line_draws_runs_not_cells(monkeypatch: Any) -> None:
    import curses

    from nws_weather_tui.radar.renderer import (
        _cell_attr, draw_radar_halfblock_line, radar_cell_attr,
    )

    monkeypatch.setattr(curses, "color_pair", lambda n: n << 8)
    _cell_attr.cache_clear()
    row = [(" ", 0, 0)] * 3 + [("▀", 4, 0)] * 5 + [("█", 7, 7)] * 4 + [("▄", 2, 0)] * 2
    fast = _Canvas(40)
    draw_radar_halfblock_line(fast, 0, row, 40, x_off=5)
    slow = _Canvas(40)
    for i, (ch, fg, bg) in enumerate(row):
        slow.addstr(0, 5 + i, ch, radar_cell_attr(ch, fg, bg))
    assert fast.cells == slow.cells
    assert fast.calls == 4 and slow.calls == len(row)


def test_overlay_lines_skip_spaces_and_merge_runs(monkeypatch: Any) -> None:
    import curses

    from nws_weather_tui.radar.renderer import draw_city_overlay_line, draw_state_overlay_line

    monkeypatch.setattr(curses, "color_pair", lambda n: n << 8)
    state = _Canvas(30)
    draw_state_overlay_line(state, 0, "  ──·  │ ", 30, x_off=2)
    assert sorted(state.cells) == [4, 5, 6, 9]  # spaces leave the radar showing
    assert state.calls == 2

    city = _Canvas(30)
    draw_city_overlay_line(city, 0, " @DEN ◉you", 30, x_off=0)
    attrs = {x: a for x, (_, a) in city.cells.items()}
    assert attrs[1] == curses.color_pair(12) | curses.A_BOLD   # @ marker
    assert attrs[2] == attrs[3] == attrs[4] == curses.color_pair(3) | curses.A_BOLD
    assert attrs[6] == curses.color_pair(4) | curses.A_BOLD    # ◉ you
    assert city.calls == 4  # "@", "DEN", "◉", "you"


def test_unchanged_config_is_not_rewritten(tmp_path: Any, monkeypatch: Any) -> None:
    import os
    import time

    from nws_weather_tui import constants
    from nws_weather_tui.constants import DEFAULT_CONFIG, load_config, save_json

    path = str(tmp_path / "config.json")
    monkeypatch.setattr(constants, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(constants, "CONFIG_PATH", path)
    save_json(path, DEFAULT_CONFIG)
    before = os.stat(path).st_mtime_ns
    time.sleep(0.02)
    cfg, warning = load_config()
    assert warning is None and cfg == DEFAULT_CONFIG
    assert os.stat(path).st_mtime_ns == before  # nothing to add: left alone
    save_json(path, {"units": "si"})
    load_config()
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["radar"] == DEFAULT_CONFIG["radar"]  # new defaults filled in
