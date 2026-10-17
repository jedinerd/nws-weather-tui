"""Key bindings, the favorites editor and the dashboard keys.

These call the real handlers with an app-shaped stand-in; test_scenarios
checks the same keys arriving through a real terminal.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from nws_weather_tui.radar.state import RadarState

DENVER = {"name": "Denver, CO", "lat": 39.7392, "lon": -104.9903}
COLUMBUS = {"name": "Columbus, OH", "lat": 39.9612, "lon": -82.9988}
VENTURA = {"name": "Ventura, CA", "lat": 34.2805, "lon": -119.2945}


class FakeApp:
    """The attributes and methods the key handlers touch."""

    def __init__(self, favorites: Optional[List[Dict[str, Any]]] = None) -> None:
        self.view = "current"
        self.favorites = [dict(f) for f in (favorites or [])]
        self.fav_idx = self.fav_edit_idx = self.dash_idx = 0
        self.location_name, self.lat, self.lon = DENVER["name"], DENVER["lat"], DENVER["lon"]
        self.units, self.use_24h, self.paused = "us", False, False
        self.show_radar_map = True
        self.radar = RadarState()
        self.forecast_periods: List[Any] = [object()] * 7
        self.hourly_periods: List[Any] = [object()] * 48
        self.fc_scroll = self.hr_scroll = self.alert_line_scroll = 0
        self.help_scroll = self.afd_scroll = self.hwo_scroll = 0
        self._dash_data: Dict[int, Any] = {1: "stale"}
        self.radar.map_x = self.radar.map_y = 0
        self.radar.map_cols = self.radar.map_rows = 0
        self.radar.city_hitmap: Dict[Tuple[int, int], Tuple[str, float, float]] = {}
        self.flashes: List[str] = []
        self.saves = 0
        self.jumps: List[Tuple[str, float, float]] = []

    def _flash(self, msg: str, seconds: float = 2.0) -> None:
        self.flashes.append(msg)

    def _save_cfg(self) -> None:
        self.saves += 1

    def _apply_location(self, name: str, lat: float, lon: float) -> None:
        self.jumps.append((name, lat, lon))


def press(app: FakeApp, *keys: Any) -> List[bool]:
    from nws_weather_tui.input_handler import handle_key

    return [handle_key(app, ord(k) if isinstance(k, str) else k) for k in keys]  # type: ignore[arg-type]


def names(app: FakeApp) -> List[str]:
    return [f["name"] for f in app.favorites]


# ---------------------------------------------------------------------------
# What the help screen promises
# ---------------------------------------------------------------------------

VIEW_KEYS = {"c": "current", "f": "forecast", "h": "hourly", "a": "alerts", "m": "moon",
             "d": "afd", "H": "hwo", "D": "dashboard", "?": "help"}


def test_help_lists_every_view_key() -> None:
    """The Views section of help documents exactly the view keys tested below."""
    from nws_weather_tui.views.help import SECTIONS

    documented = {key for title, rows in SECTIONS if title == "Views" for key, _ in rows}
    assert documented == set(VIEW_KEYS)


@pytest.mark.parametrize("key,view", sorted(VIEW_KEYS.items()))
@pytest.mark.parametrize("start", ["current", "forecast", "afd"])
def test_view_keys(key: str, view: str, start: str) -> None:
    app = FakeApp()
    app.view = start
    assert press(app, key) == [True]
    assert app.view == view


def test_help_toggles_and_escape_goes_home() -> None:
    app = FakeApp()
    press(app, "?")
    assert app.view == "help"
    press(app, "?")
    assert app.view == "current"
    for start in ("forecast", "hourly", "alerts", "moon", "afd", "hwo", "help"):
        app.view = start
        press(app, 27)
        assert app.view == "current", start


@pytest.mark.parametrize("key", ["q", "Q"])
def test_quit(key: str) -> None:
    assert press(FakeApp(), key) == [False]


def test_clock_and_pause_toggles() -> None:
    app = FakeApp()
    press(app, "t")
    assert app.use_24h is True and app.saves == 1  # remembered
    press(app, "t")
    assert app.use_24h is False and app.saves == 2
    press(app, "p")
    assert app.paused is True and app.flashes[-1] == "Paused."
    press(app, "p")
    assert app.paused is False and app.flashes[-1] == "Resumed."


def test_scrolling_stops_at_the_top() -> None:
    import curses

    app = FakeApp()
    for view, attr in (("help", "help_scroll"), ("afd", "afd_scroll"),
                       ("hwo", "hwo_scroll"), ("alerts", "alert_line_scroll")):
        app.view = view
        press(app, "k", "k", curses.KEY_PPAGE)
        assert getattr(app, attr) == 0, view
        press(app, "j", curses.KEY_DOWN, curses.KEY_NPAGE)
        assert getattr(app, attr) == 12, view


def test_jump_to_end() -> None:
    app = FakeApp()
    app.view = "forecast"
    press(app, "G")
    assert app.fc_scroll == len(app.forecast_periods) - 1
    app.view = "hourly"
    press(app, "G")
    assert app.hr_scroll == len(app.hourly_periods) - 1


# ---------------------------------------------------------------------------
# Favorites: F, n, b
# ---------------------------------------------------------------------------

def test_F_adds_then_removes_the_current_location() -> None:
    app = FakeApp([COLUMBUS])
    press(app, "F")
    assert names(app) == ["Columbus, OH", "Denver, CO"] and app.saves == 1
    app.lat += 0.00005  # same place, float noise
    press(app, "F")
    assert names(app) == ["Columbus, OH"] and app.saves == 2
    assert app._dash_data == {}  # dashboard rebuilt for the new list


def test_n_and_b_cycle_through_favorites() -> None:
    app = FakeApp([DENVER, COLUMBUS, VENTURA])
    press(app, "n", "n", "n", "b")
    assert [j[0] for j in app.jumps] == ["Columbus, OH", "Ventura, CA", "Denver, CO",
                                         "Ventura, CA"]


def test_n_without_favorites_explains() -> None:
    app = FakeApp()
    press(app, "n")
    assert app.jumps == [] and "No favorites" in app.flashes[-1]


def test_n_skips_a_broken_favorite_without_crashing() -> None:
    app = FakeApp([{"name": "No coordinates"}])
    press(app, "n")
    assert app.jumps == [] and "invalid" in app.flashes[-1]


# ---------------------------------------------------------------------------
# Favorites editor (e)
# ---------------------------------------------------------------------------

def editor(*favorites: Dict[str, Any]) -> FakeApp:
    app = FakeApp(list(favorites))
    press(app, "e")
    assert app.view == "favorites"
    return app


def test_editor_cursor_stays_in_range() -> None:
    import curses

    app = editor(DENVER, COLUMBUS, VENTURA)
    press(app, "k", curses.KEY_UP)
    assert app.fav_edit_idx == 0
    press(app, "j", "j", "j", curses.KEY_DOWN)
    assert app.fav_edit_idx == 2


def test_editor_reorder() -> None:
    app = editor(DENVER, COLUMBUS, VENTURA)
    press(app, "K")  # already first: nothing moves, nothing saved
    assert names(app) == ["Denver, CO", "Columbus, OH", "Ventura, CA"] and app.saves == 0
    press(app, "J")
    assert names(app) == ["Columbus, OH", "Denver, CO", "Ventura, CA"]
    assert app.fav_edit_idx == 1 and app.saves == 1  # the cursor follows the item
    press(app, "J", "J")  # second J is past the end: no-op
    assert names(app) == ["Columbus, OH", "Ventura, CA", "Denver, CO"] and app.saves == 2


def test_editor_delete() -> None:
    app = editor(DENVER, COLUMBUS)
    press(app, "j", "d")
    assert names(app) == ["Denver, CO"] and app.fav_edit_idx == 0 and app.saves == 1
    press(app, "d")
    assert names(app) == [] and app.fav_edit_idx == 0
    press(app, "d", "j", "J", 10)  # empty list: every key is harmless
    assert app.saves == 2 and app.jumps == []


def test_editor_enter_jumps() -> None:
    app = editor(DENVER, COLUMBUS)
    press(app, "j", 10)
    assert app.view == "current"
    assert app.jumps == [("Columbus, OH", 39.9612, -82.9988)]


@pytest.mark.parametrize("key", ["e", 27, "q", "Q"])
def test_editor_exit_keys_return_home_without_quitting(key: Any) -> None:
    app = editor(DENVER)
    assert press(app, key) == [True]
    assert app.view == "current"


# ---------------------------------------------------------------------------
# Dashboard (D)
# ---------------------------------------------------------------------------

def test_dashboard_keys() -> None:
    import curses

    app = FakeApp([DENVER, COLUMBUS, VENTURA])
    press(app, "D")
    assert app.view == "dashboard"
    press(app, "k", "j", "j", "j", curses.KEY_DOWN)
    assert app.dash_idx == 2
    press(app, curses.KEY_ENTER)
    assert app.view == "current" and app.jumps == [("Ventura, CA", 34.2805, -119.2945)]
    for back in ("D", "e", 27):
        app.view = "dashboard"
        press(app, back)
        assert app.view == "current"
    app.view = "dashboard"
    assert press(app, "q") == [False]  # q still quits from here


def test_dashboard_without_favorites() -> None:
    app = FakeApp()
    app.view = "dashboard"
    assert press(app, "j", 10, "k") == [True, True, True]
    assert app.jumps == []


# ---------------------------------------------------------------------------
# Location search input and radar clicks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,want", [
    ("39.7392,-104.9903", (39.7392, -104.9903)),
    ("  39.7392 , -104.9903 ", (39.7392, -104.9903)),
    ("39.7392 -104.9903", (39.7392, -104.9903)),
    ("+21.3,-157.8", (21.3, -157.8)),
    ("91,0", None),
    ("0,181", None),
    ("Denver, CO", None),
    ("80202", None),
    ("39.7", None),
])
def test_lat_lon_entry(text: str, want: Any) -> None:
    from nws_weather_tui.input_handler import _parse_lat_lon

    assert _parse_lat_lon(text) == want


def test_clicking_a_radar_city_moves_there() -> None:
    import curses

    from nws_weather_tui.input_handler import handle_mouse

    app = FakeApp()
    app.radar.map_x, app.radar.map_y = 50, 4
    app.radar.map_cols, app.radar.map_rows = 40, 20
    app.radar.city_hitmap = {(3, 2): ("COS", 38.8339, -104.8214),
                              (7, 9): ("DEN", DENVER["lat"], DENVER["lon"])}
    click = curses.BUTTON1_CLICKED
    handle_mouse(app, 53, 6, click)
    assert app.jumps == [("COS", 38.8339, -104.8214)]
    handle_mouse(app, 57, 13, click)  # where we already are
    assert len(app.jumps) == 1 and "Already at DEN" in app.flashes[-1]
    for x, y in ((49, 6), (90, 6), (53, 24), (54, 6)):  # off the map, or no city
        handle_mouse(app, x, y, click)
    app.view = "forecast"
    handle_mouse(app, 53, 6, click)  # the map isn't on screen
    assert len(app.jumps) == 1
