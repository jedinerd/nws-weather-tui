# AGENTS.md - NWS Weather TUI

## Build/Lint/Test Commands

### Running the Application
```bash
# one-time: editable install into a venv
python -m venv .venv && . .venv/bin/activate && pip install -e .

nws-weather-tui
# or
python -m nws_weather_tui
```

### Linting
```bash
# Run ruff linter
ruff check .

# Fix auto-fixable issues
ruff check --fix .
```

### Testing
```bash
pip install -e '.[test]'
pytest                      # fast suite (~3 s, offline): snapshots, correctness, units, vermin
pytest -m slow              # Python 3.9 run via uvx, pipx install-and-launch check
pytest --update-snapshots   # after an intended rendering change; review the diff
```
- Every screen is snapshot-tested (text and colours) at 80x24, 120x40 and 200x60
  from recorded API responses; see `tests/README.md` for re-recording.
- A layout change can change the radar panel size and so its requests: run
  `python tests/record_fixtures.py --missing-only`.

### Code Style Guidelines

#### Imports
- Use `from __future__ import annotations` for forward references
- Organize imports in three sections (separated by blank lines):
  1. Standard library (`import os`, `import re`, etc.)
  2. Third-party packages (`import curses`, `import requests`, etc.)
  3. Local modules, always absolute from the package root
     (`from nws_weather_tui.api.client import NWSClient`, `from nws_weather_tui.helpers import ...`)
- Sort alphabetically within each group

#### Type Hints
- Always use type hints for function arguments and return types
- Use `Optional[X]` instead of `X | None`; the package supports Python 3.9 (`requires-python = ">=3.9"`)
- Use `Dict[str, Any]` for dict types, not `dict[str, Any]`
- Example: `def func(arg: str) -> Optional[bool]:`

#### Naming Conventions
- **Classes**: PascalCase (e.g., `class App`, `class NWSClient`)
- **Functions/Variables**: snake_case (e.g., `def handle_key`, `self.location_name`)
- **Constants**: SCREAMING_SNAKE_CASE (e.g., `CONFIG_PATH`, `MIN_COLS`)
- **Private methods**: Prefix with underscore (e.g., `_load_points`, `_set_view`)

#### Dataclasses
- Use `@dataclass` for simple data containers
- Use `@dataclass` with `frozen=True` for immutable types when appropriate
- Order: class definition, then dataclass fields

#### Error Handling
- Use bare `except Exception` sparingly; prefer specific exceptions
- Always include context in error messages
- Use `_flash()` to display user-facing errors in the TUI
- Log debug info with `dbg()` helper for development

#### Key Handlers
`handle_key` in `input_handler.py` quits only when the matched handler returns `QUIT`; any
other result, including none, keeps the app running. The favorites editor and dashboard
handlers return a bool themselves: `True` to keep running, `QUIT` to quit.

#### TUI/UI Guidelines
- Use `_flash(message, duration)` for temporary status messages
- Use `_show_loading(message)` for long-running operations
- Use `safe_addstr(stdscr, y, x, text)` instead of `stdscr.addstr()` to prevent crashes
- Use `clamp()` helper for value bounds

#### Code Formatting
- Line length isn't configured: there is no ruff config, so ruff's defaults apply (88 columns for
  `ruff format`; long lines are not a lint error by default)
- Use 4 spaces for indentation
- No trailing whitespace
- One blank line between top-level definitions

#### File Organization
All code lives under `src/nws_weather_tui/`:
- `__init__.py` - Package version only; keep it free of imports
- `__main__.py` - Entry point (`run()`): stderr redirect + `curses.wrapper(app.main)`
- `app.py` - Main application class and `main(stdscr)`
- `input_handler.py` - Key and mouse handling, prompts
- `weather_refresh.py` - Background weather fetch and apply
- `models.py` - Data models and extraction functions
- `text_product.py` - AFD/HWO cleanup and reflow
- `constants.py` - Constants, config defaults and config loading
- `persistence.py` - Config and state save/restore
- `dashboard.py`, `favorites.py` - Favorites dashboard and editor logic
- `helpers.py`, `formatting.py`, `geo.py`, `conversions.py` - Utility functions
- `moon.py`, `windsock.py`, `icons.py`, `curses_init.py` - Moon math, windsock, icons, curses setup
- `api/` - HTTP clients: `client.py` (NWS), `geocode.py`, `geo_boundaries.py`,
  `air_quality.py`, `uv_index.py`, `cache.py` (TTL cache) and `fixtures.py` (FIXTURE record/replay)
- `radar/` - `client.py`, `decode.py`, `renderer.py`, `palette.py`, `state.py` (the
  `RadarState` dataclass held as `App.radar`, plus the background fetch),
  `overlays.py`, `cities.py`
- `views/` - One module per screen (`current.py`, `forecast.py`, `hourly.py`, `alerts.py`,
  `afd.py`, `hwo.py`, `moon.py`, `radar.py`, `favorites.py`, `dashboard.py`, `help.py`),
  `chrome.py` for header/footer, `style.py` for shared colour rules; `__init__.py`
  re-exports the `draw_*` functions

#### Git Conventions
- Use conventional commit messages
- Do not commit secrets, credentials, or API keys
- Do not commit `__pycache__/` or `.ruff_cache/`
