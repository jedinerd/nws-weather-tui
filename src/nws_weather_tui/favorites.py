#!/usr/bin/env python3
"""
NWS Weather TUI — Favorites management and editor.
"""

from __future__ import annotations

import curses
from typing import Any, Dict, TYPE_CHECKING

from nws_weather_tui.geo import clamp

if TYPE_CHECKING:
    from nws_weather_tui.app import App

# Favorites within this many degrees of the active location count as "here"
# (~5 km), so a saved ZIP centroid still matches a nearby station pick.
HERE_DEG = 0.05


def is_here(app: "App", fav: Dict[str, Any]) -> bool:
    try:
        return (
            abs(float(fav.get("lat", 0)) - app.lat) < HERE_DEG
            and abs(float(fav.get("lon", 0)) - app.lon) < HERE_DEG
        )
    except (TypeError, ValueError):
        return False


def jump_to_favorite(app: "App", fav: Dict[str, Any], announce: bool = False) -> bool:
    """Switch to a favorite's location. False (with a flash) if its lat/lon
    are missing or unusable."""
    try:
        name = str(fav.get("name") or "Favorite")
        lat, lon = float(fav["lat"]), float(fav["lon"])
    except (KeyError, TypeError, ValueError):
        app._flash("Favorite is invalid (missing lat/lon).", 2.0)
        return False
    app._apply_location(name, lat, lon)
    if announce:
        app._flash(f"Jumped to: {fav.get('name', '—')}", 1.8)
    return True


def toggle_favorite(app: "App") -> bool:
    eps = 1e-4
    idx = next(
        (
            i for i, f in enumerate(app.favorites)
            if abs(float(f.get("lat", 0)) - app.lat) < eps
            and abs(float(f.get("lon", 0)) - app.lon) < eps
        ),
        None,
    )
    if idx is not None:
        app.favorites.pop(idx)
        app._flash("Removed from favorites.", 1.2)
    else:
        app.favorites.append(
            {"name": app.location_name, "lat": app.lat, "lon": app.lon}
        )
        app._flash("Added to favorites.", 1.2)
    app.fav_idx = clamp(app.fav_idx, 0, max(0, len(app.favorites) - 1))
    app._dash_data = {}
    app._save_cfg()
    return True


def cycle_favorite(app: "App", direction: int) -> bool:
    if not app.favorites:
        app._flash("No favorites yet. Press F to add this location.", 2.0)
        return True
    app.fav_idx = (app.fav_idx + direction) % len(app.favorites)
    jump_to_favorite(app, app.favorites[app.fav_idx])
    return True


def handle_favorites_key(app: "App", ch: int) -> bool:
    if ch in (ord("e"), 27, ord("q"), ord("Q")):  # leave the editor
        app.view = "current"
        return True
    if ch in (curses.KEY_DOWN, ord("j")):
        _fav_edit_move_cursor(app, 1)
    elif ch in (curses.KEY_UP, ord("k")):
        _fav_edit_move_cursor(app, -1)
    elif ch == ord("d"):
        _fav_edit_delete(app)
    elif ch == ord("r"):
        _fav_edit_rename(app)
    elif ch == ord("a"):
        _fav_edit_add(app)
    elif ch == ord("J"):
        _fav_edit_reorder(app, 1)
    elif ch == ord("K"):
        _fav_edit_reorder(app, -1)
    elif ch in (curses.KEY_ENTER, 10, 13):
        _fav_edit_select(app)
    return True


def _fav_edit_move_cursor(app: "App", direction: int) -> None:
    if not app.favorites:
        return
    app.fav_edit_idx = clamp(
        app.fav_edit_idx + direction, 0, len(app.favorites) - 1
    )


def _fav_edit_delete(app: "App") -> None:
    if not app.favorites:
        return
    removed = app.favorites.pop(app.fav_edit_idx)
    app._flash(f"Deleted: {removed.get('name', '—')}", 1.5)
    app.fav_edit_idx = clamp(app.fav_edit_idx, 0, max(0, len(app.favorites) - 1))
    app._dash_data = {}
    app._save_cfg()


def _fav_edit_rename(app: "App") -> None:
    if not app.favorites:
        return
    old = str(app.favorites[app.fav_edit_idx].get("name", ""))
    from nws_weather_tui.input_handler import prompt_line
    new_name = prompt_line(app, f"Rename '{old}' to: ")
    if new_name:
        app.favorites[app.fav_edit_idx]["name"] = new_name
        app._flash(f"Renamed to: {new_name}", 1.5)
        app._save_cfg()
    else:
        app._flash("Rename canceled.", 1.2)


def _fav_edit_add(app: "App") -> None:
    from nws_weather_tui.input_handler import geocode_with_spinner, prompt_line
    q = prompt_line(app, "Add favorite (city/state or ZIP): ")
    if q is None or not q.strip():
        app._flash("Add canceled.", 1.2)
        return
    try:
        g = geocode_with_spinner(app, q.strip())
        if not g:
            app._flash(f"No match found for '{q}'.", 2.5)
            return
        name, lat, lon = g
        app.favorites.append({"name": name, "lat": lat, "lon": lon})
        app.fav_edit_idx = len(app.favorites) - 1
        app._flash(f"Added: {name}", 1.5)
        app._dash_data = {}
        app._save_cfg()
    except Exception as e:
        app._flash(f"Add failed: {e}", 3.5)


def _fav_edit_reorder(app: "App", direction: int) -> None:
    if not app.favorites:
        return
    new_idx = app.fav_edit_idx + direction
    if new_idx < 0 or new_idx >= len(app.favorites):
        return
    fav = app.favorites
    fav[app.fav_edit_idx], fav[new_idx] = fav[new_idx], fav[app.fav_edit_idx]
    app.fav_edit_idx = new_idx
    app._dash_data = {}
    app._save_cfg()


def _fav_edit_select(app: "App") -> None:
    if not app.favorites:
        return
    app.view = "current"
    jump_to_favorite(app, app.favorites[app.fav_edit_idx], announce=True)
