## Weather TUI project
- Target Python 3.9+. No 3.12-only syntax (e.g., nested same-quote f-strings, PEP 695 generics).
- The pipx install is not the repo code. To test changes, run from source or use `pipx install -e .`.
- Release flow: bump `__version__` in `src/nws_weather_tui/__init__.py` (pyproject reads the version from it), commit and push, then tag `vX.Y.Z` with the version exactly and push the tag. CI fails on a tag/version mismatch. Full steps are in `RELEASING.md`.
- Validate UI changes with headless screenshot or test runs. Sanity-check live data such as sunset times and rain totals against a second source.

## In-progress work
- Read `HANDOFF.md` first: branch state, open PRs, decisions and next steps. (Temporary; drop with the handoff commit before opening an upstream PR.)
