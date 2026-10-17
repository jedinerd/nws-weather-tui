#!/usr/bin/env python3
"""
NWS Weather TUI — Key dispatch, mouse handling, input prompts.
"""

from __future__ import annotations

import re
import threading
import webbrowser
from typing import Callable, Dict, Optional, Tuple, TYPE_CHECKING

import curses

from nws_weather_tui.favorites import (
    cycle_favorite,
    handle_favorites_key,
    jump_to_favorite,
    toggle_favorite,
)
from nws_weather_tui.geo import clamp
from nws_weather_tui.helpers import safe_addstr

if TYPE_CHECKING:
    from nws_weather_tui.app import App

# A handler quits the app by returning QUIT; anything else (including no
# return value at all) keeps it running.
QUIT = False

# The App attribute holding each scrolling view's position.
_SCROLL_ATTR: Dict[str, str] = {
    "forecast": "fc_scroll",
    "alerts": "alert_line_scroll",
    "hourly": "hr_scroll",
    "help": "help_scroll",
    "afd": "afd_scroll",
    "hwo": "hwo_scroll",
}
# Past any view's end; each view clamps to its own length when it draws.
_SCROLL_END = 9999

_LAT_LON_RE = re.compile(r"^\s*([+-]?\d+\.?\d*)\s*[,\s]\s*([+-]?\d+\.?\d*)\s*$")


def handle_key(app: "App", ch: int) -> bool:
    """Dispatch one key; False means quit."""
    if app.view == "favorites":
        return handle_favorites_key(app, ch)
    if app.view == "dashboard":
        return handle_dashboard_key(app, ch)
    if ch == curses.KEY_RESIZE:
        rows, cols = app.stdscr.getmaxyx()
        app._flash(f"Resized to {cols}×{rows}", 1.5)
        return True

    key_handlers: Dict[int, Callable[[], object]] = {
        ord("q"): lambda: QUIT,
        ord("Q"): lambda: QUIT,
        ord("?"): lambda: _toggle_help(app),
        curses.KEY_MOUSE: lambda: _handle_mouse_event(app),
        ord("c"): lambda: _set_view(app, "current"),
        ord("f"): lambda: _set_view(app, "forecast"),
        ord("h"): lambda: _set_view(app, "hourly"),
        ord("a"): lambda: _set_view(app, "alerts"),
        ord("m"): lambda: _set_view(app, "moon"),
        ord("d"): lambda: _set_view(app, "afd"),
        ord("H"): lambda: _set_view(app, "hwo"),
        ord("D"): lambda: _set_view(app, "dashboard"),
        ord("l"): lambda: search_location(app),
        ord("r"): lambda: _do_refresh(app),
        ord("u"): lambda: _toggle_units(app),
        ord("t"): lambda: _toggle_time_format(app),
        ord("p"): lambda: _toggle_pause(app),
        ord("A"): lambda: _toggle_radar_anim(app),
        ord("o"): lambda: _open_radar_browser(app),
        ord("e"): lambda: _set_view(app, "favorites"),
        ord("n"): lambda: cycle_favorite(app, 1),
        ord("b"): lambda: cycle_favorite(app, -1),
        ord("F"): lambda: toggle_favorite(app),
        ord("G"): lambda: scroll_to_end(app),
        27: lambda: _set_view(app, "current"),  # Escape
        curses.KEY_NPAGE: lambda: scroll(app, 10),
        curses.KEY_PPAGE: lambda: scroll(app, -10),
    }
    handler = key_handlers.get(ch)
    if handler:
        return handler() is not QUIT
    if ch in (curses.KEY_DOWN, ord("j")):
        scroll(app, 1)
    elif ch in (curses.KEY_UP, ord("k")):
        scroll(app, -1)
    elif ch in (curses.KEY_LEFT, ord("<")):
        _sideways(app, -1)
    elif ch in (curses.KEY_RIGHT, ord(">")):
        _sideways(app, 1)
    return True


def _sideways(app: "App", direction: int) -> None:
    """Left/right: scroll the forecast's day columns, or step a radar frame."""
    if app.view == "forecast":
        scroll(app, direction)
    elif app.view == "current":
        from nws_weather_tui.radar.state import step_radar_frame
        step_radar_frame(app, direction)


def handle_mouse(app: "App", mx: int, my: int, bstate: int) -> None:
    click_mask = (
        getattr(curses, "BUTTON1_CLICKED", 0)
        | getattr(curses, "BUTTON1_PRESSED", 0)
        | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
        | getattr(curses, "BUTTON1_TRIPLE_CLICKED", 0)
        | getattr(curses, "BUTTON1_RELEASED", 0)
    )
    if click_mask and not (bstate & click_mask):
        return
    if app.view != "current" or not app.show_radar_map:
        return
    if app._radar_map_cols <= 0 or app._radar_map_rows <= 0:
        return
    rx = int(mx) - app._radar_map_x
    ry = int(my) - app._radar_map_y
    if rx < 0 or ry < 0 or rx >= app._radar_map_cols or ry >= app._radar_map_rows:
        return
    hit = app._radar_city_hitmap.get((rx, ry))
    if not hit:
        return
    code, lat, lon = hit
    if abs(app.lat - lat) < 1e-4 and abs(app.lon - lon) < 1e-4:
        app._flash(f"Already at {code}.", 1.2)
        return
    app._apply_location(code, lat, lon)
    app._flash(f"Location set: {code}", 1.8)


def prompt_line(app: "App", prompt: str, max_len: int = 96) -> Optional[str]:
    rows, cols = app.stdscr.getmaxyx()
    max_input = clamp(min(max_len, cols - len(prompt) - 4), 1, max_len)
    old_paused = app.paused
    app.paused = True
    curses.echo()
    curses.curs_set(1)
    app.stdscr.timeout(-1)
    app.stdscr.nodelay(False)
    try:
        safe_addstr(app.stdscr, rows - 1, 0, " " * max(1, cols - 1))
        safe_addstr(app.stdscr, rows - 1, 1, prompt[: cols - 2], curses.A_BOLD)
        app.stdscr.refresh()
        x = clamp(len(prompt) + 1, 1, max(1, cols - 2))
        app.stdscr.move(rows - 1, x)
        raw = app.stdscr.getstr(rows - 1, x, max_input)
        return raw.decode("utf-8", errors="ignore").strip()
    except Exception:
        return None
    finally:
        curses.noecho()
        curses.curs_set(0)
        app.paused = old_paused
        app.stdscr.timeout(500)
        app.stdscr.nodelay(False)


def _parse_lat_lon(text: str) -> Optional[Tuple[float, float]]:
    """Try to parse 'lat,lon' or 'lat lon' from text. Returns (lat, lon) or None."""
    m = _LAT_LON_RE.match(text)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return (lat, lon)
    return None


def geocode_with_spinner(app: "App", query: str) -> Optional[Tuple[str, float, float]]:
    """Run geocode_us() on a background thread so the UI stays responsive
    (spinner keeps animating, terminal stays repaintable) instead of
    freezing on the main thread for the duration of the HTTP request."""
    result: dict = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            result["value"] = app.client.geocode_us(query)
        except Exception as e:
            result["error"] = str(e)
        done.set()

    threading.Thread(target=_worker, daemon=True).start()
    app.stdscr.nodelay(True)
    try:
        while not done.is_set():
            app._show_loading("Searching location...")
            app.stdscr.getch()  # drain/discard so keys typed meanwhile don't queue up
            done.wait(0.05)
    finally:
        app.stdscr.nodelay(False)
        app.stdscr.timeout(500)
    if "error" in result:
        raise RuntimeError(result["error"])
    return result.get("value")


def _confirm_location(app: "App", name: str, lat: float, lon: float) -> None:
    """Ask before switching: a search can land somewhere unexpected."""
    confirm = prompt_line(app, f"Apply '{name}'? (y/n): ", max_len=3)
    if confirm and confirm.lower().startswith("y"):
        app._apply_location(name, lat, lon)
        app._flash(f"Location set: {name}", 1.8)
    else:
        app._flash("Location search canceled.", 1.4)


def search_location(app: "App") -> bool:
    q = prompt_line(app, "Location (city/state, ZIP, or lat,lon): ")
    if q is None or not q.strip():
        app._flash("Location search canceled.", 1.4)
        return True
    q = q.strip()
    try:
        coords = _parse_lat_lon(q)
        if coords:
            lat, lon = coords
            _confirm_location(app, f"{lat:.4f}, {lon:.4f}", lat, lon)
            return True
        g = geocode_with_spinner(app, q)
        if not g:
            app._flash(f"No match found for '{q}'.", 2.5)
            return True
        _confirm_location(app, *g)
    except Exception as e:
        app._flash(f"Location search failed: {e}", 3.5)
    return True


def _set_view(app: "App", view: str) -> None:
    app.view = view


def _toggle_help(app: "App") -> None:
    app.view = "current" if app.view == "help" else "help"


def _handle_mouse_event(app: "App") -> None:
    try:
        _id, mx, my, _z, bstate = curses.getmouse()
        handle_mouse(app, mx, my, bstate)
    except Exception:
        pass


def _do_refresh(app: "App") -> None:
    app._radar_last = 0.0
    from nws_weather_tui.weather_refresh import refresh_all
    refresh_all(app, force=True, allow_offline=True)


def _toggle_units(app: "App") -> None:
    app.units = "si" if app.units == "us" else "us"
    app.client.cache.clear()
    from nws_weather_tui.weather_refresh import refresh_all
    refresh_all(app, force=True, allow_offline=True)
    app._save_cfg()


def _toggle_time_format(app: "App") -> None:
    app.use_24h = not app.use_24h
    app._flash("Toggled time format.", 1.2)
    app._save_cfg()


def _toggle_pause(app: "App") -> None:
    app.paused = not app.paused
    app._flash("Paused." if app.paused else "Resumed.", 1.2)


def _toggle_radar_anim(app: "App") -> None:
    from nws_weather_tui.radar.state import toggle_radar_anim
    toggle_radar_anim(app)


def _open_radar_browser(app: "App") -> None:
    url = "https://radar.weather.gov/"
    try:
        webbrowser.open(url)
    except Exception:
        pass
    app._flash(f"Opened {url}", 3.0)


def scroll(app: "App", direction: int) -> None:
    attr = _SCROLL_ATTR.get(app.view)
    if attr:
        setattr(app, attr, max(0, getattr(app, attr) + direction))


def scroll_to_end(app: "App") -> None:
    attr = _SCROLL_ATTR.get(app.view)
    if not attr:
        return
    if app.view == "forecast":
        end = max(0, len(app.forecast_periods) - 1)
    elif app.view == "hourly":
        end = max(0, len(app.hourly_periods) - 1)
    else:
        end = _SCROLL_END
    setattr(app, attr, end)


def handle_dashboard_key(app: "App", ch: int) -> bool:
    if ch in (ord("D"), ord("e"), 27):  # D, e, or Escape -> back
        app.view = "current"
        return True
    if ch in (ord("q"), ord("Q")):
        return QUIT
    if not app.favorites:
        return True
    if ch in (curses.KEY_DOWN, ord("j")):
        app.dash_idx = clamp(app.dash_idx + 1, 0, len(app.favorites) - 1)
    elif ch in (curses.KEY_UP, ord("k")):
        app.dash_idx = clamp(app.dash_idx - 1, 0, len(app.favorites) - 1)
    elif ch == ord("r"):
        from nws_weather_tui.dashboard import refresh_dashboard
        refresh_dashboard(app, force=True)
        app._flash("Refreshing dashboard...", 1.2)
    elif ch in (curses.KEY_ENTER, 10, 13):
        app.view = "current"
        jump_to_favorite(app, app.favorites[app.dash_idx], announce=True)
    return True
