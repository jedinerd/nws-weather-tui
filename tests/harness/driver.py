"""Render every view of the app for one location at one terminal size.

Runs in its own process (curses takes over stdin/stdout): the real App draws
through real ncurses into a pseudo-terminal, a pyte emulator reads the other
end, and after each view an in-band sentinel marks where to read the emulated
screen. Results go to a JSON file:

  python tests/harness/driver.py --location denver --cols 80 --rows 24 --out out.json

In replay mode (the default) time is frozen at the manifest's `frozen_at` and
HTTP is served from tests/fixtures/http; `--mode record` hits the network and
saves every response there instead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import pty
import struct
import sys
import tempfile
import termios
import threading
import time
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # tests/ -> `harness` package

from harness.locations import (  # noqa: E402
    HTTP_FIXTURES_DIR,
    LOCATIONS,
    MANIFEST_PATH,
    app_config,
    views_for,
)

SENTINEL_PREFIX = b"\x1b]7777;snap:"
SENTINEL_END = b"\x07"
IDLE_TIMEOUT_S = float(os.environ.get("NWS_TUI_IDLE_TIMEOUT", "90"))
RAW_DUMP = os.environ.get("NWS_TUI_RAW_DUMP")
# freezegun fakes time.monotonic too (and rewrites module globals bound to
# it), so keep the real clock inside a dict for the settle timeout.
_REAL_CLOCK = {"monotonic": time.monotonic}  # debugging: tee the pty stream here


def _emulator() -> Any:
    """pyte, plus the scroll sequences it lacks but ncurses uses.

    ncurses may redraw by setting a scroll region and sending SU/SD
    (CSI n S / CSI n T); pyte 0.8 silently drops those, leaving stale rows
    that a real terminal wouldn't show. Anything else pyte can't parse is
    recorded so the tests fail instead of trusting a wrong picture.
    """
    import pyte
    from pyte.screens import Margins

    class Screen(pyte.Screen):
        def __init__(self, cols: int, rows: int) -> None:
            super().__init__(cols, rows)
            self.unsupported: List[str] = []

        def _region(self) -> Any:
            return self.margins or Margins(0, self.lines - 1)

        def scroll_up(self, count: int = 1, *args: Any, **kwargs: Any) -> None:
            top, bottom = self._region()
            for _ in range(max(1, count or 1)):
                for y in range(top, bottom):
                    self.buffer[y] = self.buffer[y + 1]
                self.buffer.pop(bottom, None)
            self.dirty.update(range(self.lines))

        def scroll_down(self, count: int = 1, *args: Any, **kwargs: Any) -> None:
            top, bottom = self._region()
            for _ in range(max(1, count or 1)):
                for y in range(bottom, top, -1):
                    self.buffer[y] = self.buffer[y - 1]
                self.buffer.pop(top, None)
            self.dirty.update(range(self.lines))

        def ignore(self, *args: Any, **kwargs: Any) -> None:
            """Sequences with no effect on the picture: keypad mode
            (ESC = / ESC >) and xterm title save/restore (CSI 22;0;0 t)."""

        def debug(self, *args: Any, **kwargs: Any) -> None:
            self.unsupported.append(f"{args!r} {kwargs!r}")

    class Stream(pyte.ByteStream):
        csi = dict(pyte.ByteStream.csi, S="scroll_up", T="scroll_down", t="ignore")
        escape = dict(pyte.ByteStream.escape, **{"=": "ignore", ">": "ignore"})
        events = pyte.ByteStream.events | {"scroll_up", "scroll_down", "ignore"}

    return Screen, Stream


class ScreenTap:
    """Reads the pty master, feeds pyte, and snapshots at each sentinel."""

    def __init__(self, master_fd: int, cols: int, rows: int) -> None:
        screen_cls, stream_cls = _emulator()
        self.fd = master_fd
        self.screen = screen_cls(cols, rows)
        self.stream = stream_cls(self.screen)
        self.snapshots: Dict[str, Dict[str, List[str]]] = {}
        self._buf = b""
        self._cond = threading.Condition()
        self._closed = False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                with self._cond:
                    self._closed = True
                    self._cond.notify_all()
                return
            if RAW_DUMP:
                with open(RAW_DUMP, "ab") as f:
                    f.write(data)
            self._buf += data
            while True:
                i = self._buf.find(SENTINEL_PREFIX)
                if i < 0:
                    # Keep a possible partial sentinel at the tail for next time.
                    keep = len(SENTINEL_PREFIX) - 1
                    cut = max(0, len(self._buf) - keep)
                    self.stream.feed(self._buf[:cut])
                    self._buf = self._buf[cut:]
                    break
                j = self._buf.find(SENTINEL_END, i)
                if j < 0:
                    self.stream.feed(self._buf[:i])
                    self._buf = self._buf[i:]
                    break
                self.stream.feed(self._buf[:i])
                name = self._buf[i + len(SENTINEL_PREFIX):j].decode()
                with self._cond:
                    self.snapshots[name] = {
                        "text": [line.rstrip() for line in self.screen.display],
                        "style": style_lines(self.screen),
                    }
                    self._cond.notify_all()
                self._buf = self._buf[j + 1:]

    def mark(self, name: str, timeout: float = 10.0) -> Dict[str, List[str]]:
        os.write(1, SENTINEL_PREFIX + name.encode() + SENTINEL_END)
        deadline = time.time() + timeout
        with self._cond:
            while name not in self.snapshots:
                left = deadline - time.time()
                if left <= 0 or self._closed:
                    raise RuntimeError(f"screen tap never saw sentinel {name!r}")
                self._cond.wait(left)
            return self.snapshots[name]


def _style_of(ch: Any) -> str:
    parts = []
    if ch.fg != "default":
        parts.append(f"fg={ch.fg}")
    if ch.bg != "default":
        parts.append(f"bg={ch.bg}")
    parts += [flag for flag in ("bold", "reverse", "underscore", "italics") if getattr(ch, flag)]
    return " ".join(parts)


def style_lines(screen: Any) -> List[str]:
    """Run-length style map: one 'row: x0-x1 style' line per styled run, so
    color and attribute regressions show up as readable diffs."""
    out: List[str] = []
    for y in range(screen.lines):
        row = screen.buffer[y]
        runs: List[List[Any]] = []
        for x in range(screen.columns):
            st = _style_of(row[x])
            if runs and runs[-1][2] == st and runs[-1][1] == x - 1:
                runs[-1][1] = x
            else:
                runs.append([x, x, st])
        out += [f"{y:3}: {a}-{b} {st}" for a, b, st in runs if st]
    return out


def _busy(app: Any) -> bool:
    return bool(
        app._bg_weather_running or app._bg_radar_running or app._bg_dash_running
        or app._bg_weather_pending is not None or app._bg_radar_pending is not None
        or app._bg_dash_pending is not None
    )


def _settle(app: Any) -> None:
    """Tick (which draws, possibly kicking off fetches) until background work
    is done and a draw starts nothing new."""
    clock = _REAL_CLOCK["monotonic"]
    start = clock()
    radar_targets: List[Any] = []
    while clock() - start < IDLE_TIMEOUT_S:
        app._tick()
        if not _busy(app):
            app._draw()  # a draw can start a radar/dashboard fetch
            if not _busy(app):
                return
        target = (app.radar.src_cols, app.radar.src_rows)
        if not radar_targets or radar_targets[-1] != target:
            radar_targets.append(target)
        time.sleep(0.02)
    raise RuntimeError(
        f"app still busy after {IDLE_TIMEOUT_S}s (view={app.view}, "
        f"weather={app._bg_weather_running}, radar={app._bg_radar_running}, "
        f"dashboard={app._bg_dash_running}); radar fetch sizes seen: {radar_targets[-12:]}"
    )


def _run(stdscr: Any, args: argparse.Namespace, tap: ScreenTap, out: Dict[str, Any]) -> None:
    from nws_weather_tui.app import App

    stdscr.timeout(0)
    # What the app sees as "now", three ways; the tests check they agree.
    out["clock"] = {
        "local_naive": dt.datetime.now().astimezone().isoformat(),
        "utc_aware": dt.datetime.now(dt.timezone.utc).isoformat(),
        "epoch": time.time(),
    }
    app = App(stdscr, app_config_full(args.location))
    try:
        if args.keys is not None:
            _run_keys(app, tap, args.keys, out)
            return
        for view in (args.views.split(",") if args.views else views_for(args.location)):
            app.view = view
            _settle(app)
            # Steady state: no "Updated." flash or spinner in the footer.
            app.status_msg = ""
            app.status_until = 0.0
            app._is_loading = False
            app._draw()
            out["screens"][view] = tap.mark(view)
    finally:
        out["emulator_unsupported"] = sorted(set(tap.screen.unsupported))
        fx = app.client.fixtures
        if fx is not None:
            out["fixture_misses"] = sorted(set(fx.misses))
            out["fixture_hits"] = len(fx.hits)
        if app._state_thread is not None:
            app._state_thread.join(timeout=5)


# Named keys for --keys, looked up in terminfo so they arrive as the real
# terminal would send them (and curses has to decode them).
_NAMED_KEYS = {"down": "kcud1", "up": "kcuu1", "left": "kcub1", "right": "kcuf1",
               "pgdn": "knp", "pgup": "kpp", "enter": None}


def _key_bytes(script: str) -> bytes:
    """'e<down>Kq' -> bytes; <name> is a terminfo key, anything else literal."""
    import curses
    import re

    out = b""
    for name, literal in re.findall(r"<(\w+)>|(.)", script, re.S):
        if not name:
            out += literal.encode()
        elif name == "enter":
            out += b"\r"
        else:
            seq = curses.tigetstr(_NAMED_KEYS[name])
            if not seq:
                raise RuntimeError(f"terminfo has no {name} key")
            out += seq
    return out


def _run_keys(app: Any, tap: ScreenTap, script: str, out: Dict[str, Any]) -> None:
    """Type `script` into the terminal and run the app's real main loop,
    which has to return on its own (so the script should end with q)."""
    _settle(app)
    os.write(tap.fd, _key_bytes(script))
    app.run()
    out["final"] = {
        "view": app.view,
        "use_24h": app.use_24h,
        "favorites": [f.get("name") for f in app.favorites],
    }
    out["screens"]["final"] = tap.mark("final")


def _fix_aware_now() -> None:
    """freezegun adds tz_offset to now(tz) as well as to naive now(), so
    now(timezone.utc) comes back shifted by the local offset. Only the
    naive (local wall clock) form should get it."""
    from freezegun import api

    def now(cls: Any, tz: Any = None) -> Any:
        frozen = cls._time_to_freeze()
        if tz is not None:
            return api.datetime_to_fakedatetime(tz.fromutc(frozen.replace(tzinfo=tz)))
        return api.datetime_to_fakedatetime(frozen + cls._tz_offset())

    api.FakeDatetime.now = classmethod(now)  # type: ignore[method-assign]


def app_config_full(location: str) -> Dict[str, Any]:
    from nws_weather_tui.constants import DEFAULT_CONFIG, deep_merge

    return deep_merge(DEFAULT_CONFIG, app_config(location))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--location", required=True, choices=sorted(LOCATIONS))
    ap.add_argument("--cols", type=int, required=True)
    ap.add_argument("--rows", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=("replay", "record"), default="replay")
    ap.add_argument("--fixture-dir", default=HTTP_FIXTURES_DIR)
    ap.add_argument("--views", help="comma-separated views to render (default: all)")
    ap.add_argument("--keys", help="type these keys and run the main loop instead "
                                   "(<down>, <enter>, ... for named keys)")
    ap.add_argument("--home", help="HOME to use (and keep) instead of a fresh temp dir, "
                                   "e.g. to start a second run from the first one's state")
    args = ap.parse_args()

    out: Dict[str, Any] = {"location": args.location, "cols": args.cols,
                           "rows": args.rows, "screens": {}}

    # Everything the app persists (config, state, debug log, radar PNG) lands
    # in a throwaway HOME; the constants module reads HOME at import time.
    home = args.home or tempfile.mkdtemp(prefix="nws-tui-home-")
    os.makedirs(home, exist_ok=True)
    os.environ["HOME"] = home
    os.environ["TZ"] = str(LOCATIONS[args.location]["tz"])
    time.tzset()
    os.environ["NWS_TUI_FIXTURE_DIR"] = args.fixture_dir
    os.environ["NWS_TUI_FIXTURE_MODE"] = args.mode
    os.environ["TERM"] = "xterm-256color"
    for var in ("LINES", "COLUMNS"):
        os.environ.pop(var, None)

    freezer = None
    if args.mode == "replay":
        import freezegun

        with open(MANIFEST_PATH, encoding="utf-8") as f:
            frozen_at = json.load(f)["frozen_at"]
        # freezegun's now()/fromtimestamp() add `tz_offset` to UTC (default 0,
        # i.e. a UTC wall clock); match the location's real offset at that
        # instant so they agree with astimezone(), which follows TZ.
        epoch = dt.datetime.fromisoformat(frozen_at).timestamp()
        offset = dt.timedelta(seconds=time.localtime(epoch).tm_gmtoff)
        freezer = freezegun.freeze_time(frozen_at, tz_offset=offset, tick=False)
        _fix_aware_now()
        freezer.start()

    dump_after = float(os.environ.get("NWS_TUI_STACK_DUMP_AFTER", "0") or 0)
    if dump_after > 0:  # debugging hangs: dump every thread's stack, then exit
        import faulthandler

        stack_file = open(os.environ.get("NWS_TUI_STACK_DUMP", args.out + ".stack"), "w")
        faulthandler.dump_traceback_later(dump_after, exit=True, file=stack_file)

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", args.rows, args.cols, 0, 0))
    log = open(os.path.join(home, "stderr.log"), "w", encoding="utf-8")
    saved_out = os.dup(1)
    os.dup2(slave, 0)
    os.dup2(slave, 1)
    sys.stderr = log
    tap = ScreenTap(master, args.cols, args.rows)

    import curses

    try:
        curses.wrapper(_run, args, tap, out)
    except Exception as e:  # report, don't hide: the parent turns this into a failure
        import traceback

        out["error"] = f"{type(e).__name__}: {e}"
        out["traceback"] = traceback.format_exc()
    finally:
        os.dup2(saved_out, 1)
        sys.stderr = sys.__stderr__
        if freezer is not None:
            freezer.stop()
        log.flush()
        with open(os.path.join(home, "stderr.log"), encoding="utf-8") as f:
            out["stderr"] = f.read()
        debug_log = os.path.join(home, ".config", "nws-weather-tui", "debug.log")
        if os.path.exists(debug_log):
            with open(debug_log, encoding="utf-8") as f:
                out["debug_log"] = f.read()
        config = os.path.join(home, ".config", "nws-weather-tui", "config.json")
        if os.path.exists(config):
            with open(config, encoding="utf-8") as f:
                out["config"] = json.load(f)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    return 1 if "error" in out else 0


if __name__ == "__main__":
    sys.exit(main())
