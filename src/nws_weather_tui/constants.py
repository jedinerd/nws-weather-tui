#!/usr/bin/env python3
"""
NWS Weather TUI — Constants, config defaults, and paths.

City data lives in ``radar/cities.py`` and the radar palette in
``radar/palette.py``; they're re-exported here for backward compatibility.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")

# --- Re-exports from extracted modules ---
from nws_weather_tui.radar.cities import MAJOR_CITIES  # noqa: F401
from nws_weather_tui.radar.palette import (      # noqa: F401
    N_RADAR_COLORS,
    NWS_RADAR_PALETTE,
    radar_curses_color,
    radar_dual_pair,
    radar_single_pair,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

APP_NAME = "nws-weather-tui"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
RADAR_LAST_PNG_PATH = os.path.join(CONFIG_DIR, "radar-last.png")
DEBUG_LOG_PATH = os.path.join(CONFIG_DIR, "debug.log")

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    "location_name": "Columbus, OH",
    "lat": 39.9612,
    "lon": -82.9988,
    "units": "us",
    "use_24h": False,
    "auto_refresh_seconds": 300,
    "http_timeout": 10,
    "user_agent": os.getenv(
        "WEATHER_APP_UA", "NWSWeatherTUI/1.0 (contact: cjrinderneck@proton.me)"
    ),
    "hourly_hours": 0,  # 0 = everything the API returns (~156h)
    "show_radar_map": True,
    "alert_notify": ["bell", "tab"],  # any of: bell, desktop, tab
    "favorites": [],
    "radar": {
        "show_state_lines": True,
        "show_city_labels": True,
        "show_alert_polygons": True,
        "max_city_labels": 20,
        "ascii_ramp": " .:-=+*#%@",
        "animation_frames": 8,
        "animation_interval_s": 0.5,
        "animation_step_min": 5,
    },
    "cache_ttls": {
        "points": 86400,
        "stations": 86400,
        "observation": 300,
        "forecast": 600,
        "forecast_hourly": 600,
        "alerts": 300,
        "radar": 300,
        "air_quality": 900,
        "uv_index": 900,
        "afd": 3600,
        "hwo": 3600,
        "gridpoints": 600,
    },
}

# ---------------------------------------------------------------------------
# File utilities
# ---------------------------------------------------------------------------


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


_save_json_lock = threading.Lock()


def save_json(path: str, obj: Dict[str, Any]) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = f"{path}.{os.getpid()}.tmp"
    with _save_json_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
        os.replace(tmp, path)


def deep_merge(defaults: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(defaults)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = v
        else:
            out[k] = v
    return out


def load_config() -> Tuple[Dict[str, Any], Optional[str]]:
    """Load the config, merged over defaults.

    Returns ``(cfg, warning)``. ``warning`` is set when the file existed but
    couldn't be used; the file is moved aside rather than overwritten, so a
    typo from hand-editing never silently loses favorites.
    """
    ensure_dir(CONFIG_DIR)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
    except FileNotFoundError:
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG), None
    except (OSError, ValueError) as e:
        return _quarantine_config(f"could not be read ({e})")
    if not isinstance(on_disk, dict):
        return _quarantine_config("is not a JSON object")
    cfg = deep_merge(DEFAULT_CONFIG, on_disk)
    if cfg != on_disk:  # fill in new defaults; otherwise leave the file alone
        save_json(CONFIG_PATH, cfg)
    return cfg, None


def _quarantine_config(reason: str) -> Tuple[Dict[str, Any], Optional[str]]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{CONFIG_PATH}.broken-{stamp}"
    try:
        os.replace(CONFIG_PATH, backup)
    except OSError as e:
        return dict(DEFAULT_CONFIG), (
            f"Config {reason}; using defaults. Could not back it up: {e}"
        )
    save_json(CONFIG_PATH, DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG), (
        f"Config {reason}; using defaults. Old file kept as {os.path.basename(backup)}"
    )


BASE = "https://api.weather.gov"

MIN_COLS = 70
MIN_ROWS = 22
