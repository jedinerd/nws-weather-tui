# Handoff: macOS audio, harness fix, profiler (2026-10-19)

Work-in-progress notes for the next session. **Drop this file and the
CLAUDE.md pointer to it (the "chore: handoff notes" commit) before opening
the profiler PR upstream**: `git rebase -i` is not available to Claude, so use
`git revert --no-edit <sha>` or `git reset` on a branch copy, or ask the user.

## Repos and branches

- `origin` = github.com/jedinerd/nws-weather-tui (the user's fork, push here)
- `upstream` = github.com/crinderneck/nws-weather-tui (PRs go here; the user
  has no push access to upstream)
- The branches stack; each is cut from the one above it:

| Branch | State | What |
|---|---|---|
| `feat/macos-sounddevice` | PR crinderneck/nws-weather-tui#12, open, awaiting review | Weather tune on macOS through sounddevice |
| `fix/harness-macos-hang` | PR crinderneck/nws-weather-tui#13, open, awaiting review | Test driver deadlock on macOS |
| `feat/profiler` (this branch, on top of #13) | **No PR yet** | `tests/profile_app.py` profiler |

The repo has no CI on pull requests (the only workflow runs on release tags),
so a PR shows no checks.

## Setting up on a new machine

```bash
git clone https://github.com/jedinerd/nws-weather-tui && cd nws-weather-tui
git remote add upstream https://github.com/crinderneck/nws-weather-tui
git fetch upstream && git switch feat/profiler
uv venv .venv && uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python -m pytest          # ~7 s, 393 passed on macOS at commit 84d5ab8
```

- On the user's corporate VPN, live network needs
  `REQUESTS_CA_BUNDLE=~/corporate-certs.pem` (TLS is intercepted). In Docker,
  add the cert to the system store (`update-ca-certificates`) and set
  `PIP_CERT=/etc/ssl/certs/ca-certificates.crt`; `PIP_CERT` with the corp
  cert alone fails on Python 3.9's pip.
- Run the app from source: `REQUESTS_CA_BUNDLE=~/corporate-certs.pem .venv/bin/nws-weather-tui`.
- Snapshot and scenario tests hang on macOS without #13's fix. This branch
  includes it.

## 1. macOS audio (#12, `feat/macos-sounddevice`)

- `music.py` pipes raw PCM to `pw-play`/`paplay`/`aplay`. macOS has no player
  that reads raw PCM from stdin (`afplay` takes only a file), so when none of
  those exist, `Music` now plays through sounddevice (PortAudio, CoreAudio).
  Linux behaviour is unchanged; the players are still tried first.
- `sounddevice; sys_platform == 'darwin'` is a regular dependency, not an
  extra: extras don't get added on a pipx reinstall (see upstream PR #4).
  Linux wheels don't bundle PortAudio, which is why Linux wasn't switched.
- **Writer thread, not a callback stream.** The callback version clicked. On a
  Scarlett 4i4, "high" latency is 11 ms, and the callback needs the GIL;
  under GIL load it delivered 12% of real time. A thread writing 1024-frame
  chunks into a 0.2 s PortAudio ring buffer (`SD_LATENCY`, `SD_CHUNK`) had 0
  underflows, because CoreAudio drains the buffer from C. The writer thread
  owns abort and close: aborting a stream while another thread is blocked in
  a write isn't safe.
- The user listened to it: the clicks are gone. 4 unit tests use a fake
  sounddevice module.

## 2. Harness deadlock on macOS (#13, `fix/harness-macos-hang`)

- `curses.wrapper`'s teardown (`nocbreak`, `endwin`) calls
  `tcsetattr(TCSADRAIN)` holding the GIL (CPython's `endwin` has no
  `Py_BEGIN_ALLOW_THREADS`). On macOS, `TIOCSETAW` sleeps in `ttywait()`
  until the pty master is read, and the reader is `ScreenTap._pump`, a
  Python thread that needs the GIL. Linux ptys don't queue output, so the
  bug is macOS-only.
- Fix: `_curses_session` in `tests/harness/driver.py` does the wrapper's
  setup without the teardown (the pty is discarded anyway).
- Verified: the macOS fast suite went from hanging to passing in ~6 s, and
  the Linux runs (Docker arm64, Python 3.12 and 3.9) passed with both the old
  and the new driver.
- The real app isn't affected: its pty reader is the terminal, another
  process. If the terminal stops reading, quitting stalls until it reads
  again (measured 3.03 s for a 3 s stall); it doesn't deadlock. `refresh`,
  `noutrefresh` and `getch` release the GIL.

## 3. Profiler (`feat/profiler`, commit 84d5ab8)

Usage and design are in `tests/README.md` under "Profiling". Files:
`tests/profile_app.py` (CLI and report), `tests/harness/profiling.py` (runs
inside the driver), `driver.py --profile/--mode live`, `tests/test_profile.py`.

Decisions the user made:
- Measure everything: per-screen draw time, startup and data phases, CPU
  flame graphs, memory.
- Data: recorded fixtures by default, `--live` selectable.
- Flame graphs via pyinstrument (a new test dependency the user accepted).
- Large sizes: a monitor ladder preset `--sizes large` = 266x67, 355x90,
  477x90, 533x135, run live, profiler only (no snapshot coverage at large
  sizes).

Non-obvious parts, already handled (don't undo them):
- **time-machine, not freezegun, for profile replay.** freezegun's pure-Python
  `FakeDatetime.astimezone` made Hourly time 12.6 ms against 1.4 ms live.
  Give `time_machine.travel` a **timestamp**: an aware datetime also changes
  the local timezone. time-machine is a second new test dependency (2.16 is
  the last release that supports Python 3.9; unpinned resolves correctly).
- pyinstrument reads the frozen `time.time()` for a session's duration (its
  samples use their own clock), so `_real()` sets the duration from
  `perf_counter`.
- pyinstrument samples one thread. Fetch threads and their
  `ThreadPoolExecutor` workers each get their own profiler, merged with
  `Session.combine`, which needs `async_mode="disabled"`.
- Profile runs use `ScreenTap(emulate=False)`: pyte only drains the pty, so
  it neither competes for the GIL nor shows up in tracemalloc numbers (it
  had been producing negative per-screen memory).
- Fixtures only cover 80x24, 120x40 and 200x60. Radar is requested at the
  panel size (12 px per panel row), and `record_fixtures.py --missing-only`
  can't help, because MRMS keeps only ~2 h of frames and the recording is
  from 2026-10-13. Any other size makes the whole run live.

Findings so far (live, 3 locations):
- Every screen redraws in under 2.5 ms except **Current** (2.9 ms at 200x60
  up to 18.9–20.7 ms at 533x135) and **Moon** (4.0 up to 15.6 ms).
- At 533x135, 92% of Current's redraw is `draw_radar_panel`: `_draw_runs` in
  `radar/renderer.py` loops over every cell in Python 3 times per row
  (halfblocks `:88`, state overlay `:117`, city overlay `:141`) on every
  redraw, rebuilding identical runs.
- Network dominates a real launch: radar fetch 2.5–3.1 s at 200x60 and
  4.3–5.9 s at 533x135; weather fetch ~1.5 s (two 11–12 s outliers).
- Replay numbers match live for drawing (Hourly 1.45 ms replay, 1.43 ms live).

## Next steps

1. **Open the profiler PR** when the user says so: drop the handoff commit,
   push, then `gh pr create -R crinderneck/nws-weather-tui --head
   jedinerd:feat/profiler --base main`. The PR is stacked on #13; say that in
   the description, and that it adds pyinstrument and time-machine to the
   `test` extra. Retarget it if #13 merges first.
2. **Proposed, not started: cache the radar panel's draw runs.** Compute each
   row's (x, text, attr) runs once per frame, panel size and overlay data,
   and only issue the addstr calls on redraw. Rendering must stay
   byte-identical (snapshot tests unchanged). Measure before and after with
   `python tests/profile_app.py --passes timing --locations denver --sizes 200x60,533x135`
   (live, needs the CA bundle). Moon at large sizes is the next candidate.
3. Watch #12 and #13 for review comments.
