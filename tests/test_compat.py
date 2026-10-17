"""Packaging and compatibility checks.

  vermin            fast: no syntax or stdlib use newer than Python 3.9
  python 3.9 suite  slow: the whole fast suite on 3.9 via `uvx --python 3.9`
  pipx install      slow: `pipx install --force .` into a throwaway PIPX_HOME,
                    then the installed command starts, draws and quits cleanly

Run the slow ones with: pytest -m slow
"""

from __future__ import annotations

import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import time
from typing import Dict, List, Optional

import pytest

from harness.locations import HTTP_FIXTURES_DIR, REPO_ROOT


def _tool(name: str) -> Optional[str]:
    """A CLI from this interpreter's environment (e.g. .venv/bin), else PATH."""
    local = os.path.join(os.path.dirname(sys.executable), name)
    return local if os.access(local, os.X_OK) else shutil.which(name)


def _require(name: str) -> str:
    path = _tool(name)
    if path is None:
        pytest.skip(f"{name} not installed (pip install {'uv' if name == 'uvx' else name})")
    return path  # type: ignore[return-value]


def test_vermin_python39() -> None:
    vermin = _require("vermin")
    proc = subprocess.run(
        [vermin, "--target=3.9-", "--violations", "--eval-annotations", "--no-tips",
         "--no-parse-comments", "--backport", "typing", "--backport", "dataclasses",
         "--backport", "argparse",
         os.path.join(REPO_ROOT, "src"), os.path.join(REPO_ROOT, "tests")],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0 and "Target versions not met" not in proc.stdout, proc.stdout


@pytest.mark.slow
def test_suite_under_python39() -> None:
    """Install from source on the oldest supported Python (so the build
    backend has to work there too), then run the fast suite on it —
    snapshots included."""
    uvx = _require("uvx")
    base = [uvx, "--python", "3.9", "--with", REPO_ROOT, "--with", "freezegun", "--with", "pyte"]
    probe = subprocess.run(
        base + ["python", "-c",
                "import sys, nws_weather_tui as p; print(sys.version_info[:2], p.__file__)"],
        capture_output=True, text=True, timeout=900,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.startswith("(3, 9)") and "site-packages" in probe.stdout, probe.stdout
    proc = subprocess.run(
        base + ["--from", "pytest", "pytest", os.path.join(REPO_ROOT, "tests"),
                "-q", "-m", "not slow", "-p", "no:cacheprovider"],
        capture_output=True, text=True, timeout=900, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]


def _read_until(fd: int, needle: Optional[bytes], timeout: float) -> bytes:
    """Read the pty until `needle` appears (or, with None, for `timeout` s)."""
    out = b""
    deadline = time.time() + timeout
    while time.time() < deadline and (needle is None or needle not in out):
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    return out


@pytest.mark.slow
def test_pipx_install_launches(tmp_path: str) -> None:
    pipx = _require("pipx")
    root = str(tmp_path)
    env: Dict[str, str] = dict(
        os.environ,
        # Everything pipx writes stays in tmp — never the real ~/.local/bin shim.
        PIPX_HOME=os.path.join(root, "pipx"),
        PIPX_BIN_DIR=os.path.join(root, "bin"),
        PIPX_MAN_DIR=os.path.join(root, "man"),
        HOME=os.path.join(root, "home"),
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    os.makedirs(env["HOME"])
    install = subprocess.run([pipx, "install", "--force", REPO_ROOT], env=env,
                             capture_output=True, text=True, timeout=900)
    assert install.returncode == 0, install.stdout + install.stderr
    exe = os.path.join(env["PIPX_BIN_DIR"], "nws-weather-tui")
    assert os.access(exe, os.X_OK), os.listdir(env["PIPX_BIN_DIR"])

    # Launch it for real on a pseudo-terminal, offline against the fixtures.
    env.update(TERM="xterm-256color", LANG="C.UTF-8",
               NWS_TUI_FIXTURE_DIR=HTTP_FIXTURES_DIR, NWS_TUI_FIXTURE_MODE="replay")
    pid, fd = pty.fork()
    if pid == 0:  # child
        import fcntl
        import termios

        fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.execve(exe, [exe], env)
    screen = b""
    status: List[int] = []
    try:
        screen = _read_until(fd, b"Units:US", timeout=30)  # header drawn
        time.sleep(2)  # let the first refresh land and redraw
        screen += _read_until(fd, None, timeout=0.5)
        os.write(fd, b"q")
        deadline = time.time() + 20
        while time.time() < deadline and not status:
            screen += _read_until(fd, None, timeout=0.2)
            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status.append(code)
    finally:
        if not status:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(fd)

    text = screen.decode("utf-8", errors="replace")
    debug_log = os.path.join(env["HOME"], ".config", "nws-weather-tui", "debug.log")
    log = open(debug_log, encoding="utf-8").read() if os.path.exists(debug_log) else ""
    assert status, "the app did not exit after 'q'"
    assert os.WIFEXITED(status[0]) and os.WEXITSTATUS(status[0]) == 0, (status, text[-2000:])
    assert "Columbus, OH" in text and "NWS Weather TUI" in text  # the default location drew
    assert "Traceback" not in text, text[-3000:]
    assert "Traceback" not in log, log[-3000:]
