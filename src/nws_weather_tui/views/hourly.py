#!/usr/bin/env python3
"""
NWS Weather TUI — Hourly forecast view.

Rather than charting temperature (which just traces the day/night cycle),
the top of the page calls out what is actually *notable* in the coming
hours: when precipitation starts and stops, thunder risk, gusts, feels-like
divergence, swings that run against the diurnal cycle (fronts), humidity
and cloud transitions. Below that a condition ribbon shows at a glance
when the sky is clear, cloudy or wet across the whole forecast. When the
terminal is wide enough every day gets its own column with hours running
down the rows; otherwise a scrolling table breaks the hours up by day with
sunrise/sunset markers and colour-coded values.
"""

from __future__ import annotations

import curses
import datetime as dt
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from nws_weather_tui.conversions import c_to_f, mm_to_in
from nws_weather_tui.formatting import fmt_num, fmt_time
from nws_weather_tui.geo import clamp
from nws_weather_tui.helpers import get_sunrise_sunset, safe_addstr
from nws_weather_tui.icons import ICON_TINY
from nws_weather_tui.models import HourlyPeriod
from nws_weather_tui.views.style import (
    C_BLUE, C_CYAN, C_GREEN, C_MAGENTA, C_RED, C_YELLOW, temp_attr,
)

if TYPE_CHECKING:
    from nws_weather_tui.app import App

KMH_PER_MPH = 1.609344
MIN_POP = 10  # precip chances below this are noise; leave the cell blank

# A row in the scrollable table: ("day", date) | ("sun", (label, when)) |
# ("hour", index into the hourly list).
TableRow = Tuple[str, Any]
# One "at a glance" line: (tag, tag colour attr, text).
Insight = Tuple[str, int, str]


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

def _is_f(hrs: List[HourlyPeriod]) -> bool:
    unit = next((h.temperature_unit for h in hrs if h.temperature_unit), "F")
    return unit.upper() == "F"


def _deg(c: Optional[float], is_f: bool) -> Optional[float]:
    """Convert an API degC value to the display unit."""
    return c_to_f(c) if is_f else c


def _delta(f_delta: float, is_f: bool) -> float:
    """Scale a threshold expressed in °F to the display unit."""
    return f_delta if is_f else f_delta * 5.0 / 9.0


def _speed(kmh: Optional[float], is_f: bool) -> Optional[float]:
    if kmh is None:
        return None
    return kmh / KMH_PER_MPH if is_f else kmh


def _local(t: Optional[dt.datetime]) -> Optional[dt.datetime]:
    return t.astimezone() if t is not None else None


def _hr(h: HourlyPeriod, app: "App") -> str:
    """Compact hour label: '4pm' / '16:00'."""
    t = _local(h.start)
    if t is None:
        return "?"
    if app.use_24h:
        return t.strftime("%H:%M")
    return t.strftime("%I%p").lstrip("0").lower()


def _at(h: HourlyPeriod, app: "App") -> str:
    """Hour label with a weekday once it's no longer today: '4pm' / 'Wed 4pm'."""
    t = _local(h.start)
    if t is None or t.date() == dt.date.today():
        return _hr(h, app)
    return f"{t.strftime('%a')} {_hr(h, app)}"


def _span(hrs: List[HourlyPeriod], i: int, j: int, app: "App") -> str:
    """Describe hours i..j inclusive as a time range."""
    if i == 0 and j == len(hrs) - 1:
        return "all period"
    if i == 0:
        return f"now–{_at(hrs[j], app)}"
    if j == len(hrs) - 1:
        return f"from {_at(hrs[i], app)}"
    if i == j:
        return f"around {_at(hrs[i], app)}"
    ti, tj = _local(hrs[i].start), _local(hrs[j].start)
    same_day = ti is not None and tj is not None and ti.date() == tj.date()
    end = _hr(hrs[j], app) if same_day else _at(hrs[j], app)
    return f"{_at(hrs[i], app)}–{end}"


def _wind_label(h: HourlyPeriod, with_unit: bool) -> str:
    """'SW 12 mph' / 'SW 12', or 'calm' when there's no wind to speak of."""
    if h.wind_speed_num is not None and h.wind_speed_num < 1:
        return "calm"
    if with_unit:
        return f"{h.wind_dir} {h.wind_speed}".strip()
    speed = "" if h.wind_speed_num is None else f"{h.wind_speed_num:.0f}"
    return f"{h.wind_dir} {speed}".strip()


def _runs(flags: List[bool]) -> List[Tuple[int, int]]:
    """Contiguous True runs as (start, end) inclusive index pairs."""
    out: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for i, f in enumerate(flags + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((start, i - 1))
            start = None
    return out


def _argmax(
    hrs: List[HourlyPeriod], key: Callable[[HourlyPeriod], Optional[float]],
    lo: int = 0, hi: Optional[int] = None,
) -> Optional[int]:
    hi = len(hrs) - 1 if hi is None else hi
    best: Optional[int] = None
    for i in range(lo, hi + 1):
        v = key(hrs[i])
        if v is not None and (best is None or v > (key(hrs[best]) or 0)):
            best = i
    return best


# ---------------------------------------------------------------------------
# "At a glance" insights
# ---------------------------------------------------------------------------

def _precip_kind(text: str) -> str:
    t = text.lower()
    if "thunder" in t or "storm" in t:
        return "Storms"
    if "freezing" in t or "sleet" in t or "wintry" in t or "ice" in t:
        return "Wintry mix"
    if "snow" in t or "flurr" in t:
        return "Snow"
    if "drizzle" in t:
        return "Drizzle"
    if "rain" in t or "shower" in t:
        return "Rain"
    return "Precip"


def _precip_insight(hrs: List[HourlyPeriod], app: "App") -> Insight:
    pops = [h.pop or 0.0 for h in hrs]
    runs = _runs([p >= 30 for p in pops])
    peak = max(pops) if pops else 0.0

    total_mm = sum(h.precip_mm for h in hrs if h.precip_mm is not None)
    snow_mm = sum(h.snow_mm for h in hrs if h.snow_mm is not None)
    amount = ""
    if total_mm >= 0.25:
        if app.units == "us":
            amount = f" · {fmt_num(mm_to_in(total_mm), 2)} in"
            if snow_mm >= 2.5:
                amount += f" ({fmt_num(mm_to_in(snow_mm), 1)} in snow)"
        else:
            amount = f" · {fmt_num(total_mm, 1)} mm"
            if snow_mm >= 2.5:
                amount += f" ({fmt_num(snow_mm / 10.0, 1)} cm snow)"

    if not runs:
        if peak >= 15:
            return ("Precip", curses.color_pair(C_CYAN),
                    f"Mostly dry — only a slight chance (max {peak:.0f}%)")
        horizon = f"{len(hrs)}h" if len(hrs) <= 48 else f"{len(hrs) // 24} days"
        return ("Precip", curses.color_pair(C_GREEN),
                f"Dry for the next {horizon}")

    i, j = runs[0]
    pk = _argmax(hrs, lambda h: h.pop, i, j) or i
    kind = _precip_kind(hrs[pk].short_forecast)
    p = pops[pk]
    odds = "likely" if p >= 60 else "possible"
    if i == 0 and j < len(hrs) - 1:
        text = f"{kind} {odds} now, easing after {_at(hrs[j], app)}"
    else:
        text = f"{kind} {odds} {_span(hrs, i, j, app)}"
    text += f" · peak {p:.0f}% at {_at(hrs[pk], app)}{amount}"
    if len(runs) > 1:
        text += f" · again {_span(hrs, *runs[1], app)}"
    attr = curses.color_pair(C_BLUE) | (curses.A_BOLD if p >= 60 else 0)
    return ("Precip", attr, text)


def _thunder_insight(hrs: List[HourlyPeriod], app: "App") -> Optional[Insight]:
    runs = _runs([(h.thunder_pct or 0) >= 15 for h in hrs])
    if not runs:
        return None
    i, j = runs[0]
    pk = _argmax(hrs, lambda h: h.thunder_pct, i, j) or i
    p = hrs[pk].thunder_pct or 0
    return ("Thunder", curses.color_pair(C_MAGENTA) | curses.A_BOLD,
            f"Lightning risk {_span(hrs, i, j, app)} · peak {p:.0f}%")


def _wind_insight(hrs: List[HourlyPeriod], app: "App", is_f: bool) -> Optional[Insight]:
    unit = "mph" if is_f else "km/h"
    pk = _argmax(hrs, lambda h: h.gust_kmh)
    if pk is not None:
        gust = _speed(hrs[pk].gust_kmh, is_f) or 0.0
        gusty = _speed(40.0, is_f) or 0.0  # ~25 mph
        if gust >= gusty:
            windy = [(_speed(h.gust_kmh, is_f) or 0) >= gusty for h in hrs]
            i, j = next(r for r in _runs(windy) if r[0] <= pk <= r[1])
            strong = gust >= (_speed(64.0, is_f) or 0)  # ~40 mph
            attr = curses.color_pair(C_RED if strong else C_YELLOW) | curses.A_BOLD
            return ("Wind", attr,
                    (f"Gusty {_span(hrs, i, j, app)} · up to {gust:.0f} {unit}"
                     f" at {_at(hrs[pk], app)}"))
        return ("Wind", curses.color_pair(C_GREEN),
                f"Light — gusts stay under {max(gust, 1):.0f} {unit}")
    spk = _argmax(hrs, lambda h: h.wind_speed_num)
    if spk is None:
        return None
    return ("Wind", curses.A_DIM,
            f"Strongest {hrs[spk].wind_speed} at {_at(hrs[spk], app)}")


def _feels_insight(hrs: List[HourlyPeriod], app: "App", is_f: bool) -> Optional[Insight]:
    best: Optional[int] = None
    best_gap = 0.0
    for i, h in enumerate(hrs):
        feels = _deg(h.apparent_c, is_f)
        if feels is None or h.temperature is None:
            continue
        gap = feels - h.temperature
        if abs(gap) > abs(best_gap):
            best, best_gap = i, gap
    if best is None or abs(best_gap) < _delta(5, is_f):
        return None
    side = [
        h.apparent_c is not None and h.temperature is not None
        and ((_deg(h.apparent_c, is_f) or 0) - h.temperature) * best_gap > 0
        and abs((_deg(h.apparent_c, is_f) or 0) - h.temperature) >= _delta(3, is_f)
        for h in hrs
    ]
    i, j = next(r for r in _runs(side) if r[0] <= best <= r[1])
    colder = best_gap < 0
    why = "wind chill" if colder else "heat index"
    attr = curses.color_pair(C_CYAN if colder else C_RED)
    return ("Feels", attr,
            (f"Up to {abs(best_gap):.0f}° {'colder' if colder else 'warmer'} "
             f"than the air {_span(hrs, i, j, app)} ({why})"))


def _swing_insight(hrs: List[HourlyPeriod], app: "App", is_f: bool) -> Optional[Insight]:
    """Temperature moves that run against the day/night cycle — the only
    temperature changes worth calling out (fronts, outflow, clearing)."""
    window = 3
    thresh = _delta(7, is_f)
    best: Optional[Tuple[float, int, int]] = None
    for i in range(len(hrs) - window):
        a, b = hrs[i], hrs[i + window]
        t0 = _local(a.start)
        if a.temperature is None or b.temperature is None or t0 is None:
            continue
        change = b.temperature - a.temperature
        daytime = 8 <= t0.hour < 14
        overnight = t0.hour >= 20 or t0.hour < 5
        unusual = (change < 0 and daytime) or (change > 0 and overnight)
        if unusual and abs(change) >= thresh and (best is None or abs(change) > abs(best[0])):
            best = (change, i, i + window)
    if best is None:
        return None
    change, i, j = best
    a, b = hrs[i].temperature, hrs[j].temperature
    if change < 0:
        return ("Front", curses.color_pair(C_CYAN) | curses.A_BOLD,
                (f"Temp falls {a:.0f}°→{b:.0f}° {_span(hrs, i, j, app)}"
                 " — midday drop, likely a front or storms"))
    return ("Front", curses.color_pair(C_YELLOW) | curses.A_BOLD,
            (f"Temp rises {a:.0f}°→{b:.0f}° {_span(hrs, i, j, app)}"
             " — overnight warm-up"))


def _air_insight(hrs: List[HourlyPeriod], app: "App", is_f: bool) -> Optional[Insight]:
    dews = [_deg(h.dewpoint_c, is_f) for h in hrs]
    if all(d is None for d in dews):
        return None
    pk = max((i for i, d in enumerate(dews) if d is not None), key=lambda i: dews[i] or 0)
    top = dews[pk] or 0.0
    u = "°"
    for word, thresh_f, color in (("Oppressive", 70, C_RED), ("Muggy", 65, C_YELLOW)):
        thresh = thresh_f if is_f else (thresh_f - 32) * 5 / 9
        if top >= thresh:
            i, j = next(r for r in _runs([(d or -99) >= thresh for d in dews])
                        if r[0] <= pk <= r[1])
            return ("Air", curses.color_pair(color),
                    f"{word} {_span(hrs, i, j, app)} · dew point up to {top:.0f}{u}")
    rhs = [h.humidity_pct for h in hrs if h.humidity_pct is not None]
    lo = min(rhs) if rhs else None
    if lo is not None and lo <= 20:
        return ("Air", curses.color_pair(C_YELLOW),
                (f"Very dry — humidity down to {lo:.0f}% "
                 f"(dew point {min(d for d in dews if d is not None):.0f}{u})"))
    comfy_hi = 60 if is_f else 15.5
    if top < comfy_hi:
        return ("Air", curses.color_pair(C_GREEN),
                f"Comfortable — dew point {top:.0f}{u} or lower")
    return ("Air", curses.A_DIM, f"Slightly humid — dew point up to {top:.0f}{u}")


def _sky_insight(hrs: List[HourlyPeriod], app: "App") -> Optional[Insight]:
    sky = [h.sky_pct for h in hrs]
    if all(s is None for s in sky):
        return None

    def state(s: Optional[float]) -> Optional[str]:
        if s is None:
            return None
        return "clear" if s < 35 else ("cloudy" if s > 70 else None)

    cur = next((state(s) for s in sky[:3] if state(s)), None)
    for i, s in enumerate(sky):
        st = state(s)
        if st and cur and st != cur:
            verb = "Clearing" if st == "clear" else "Clouding over"
            return ("Sky", curses.color_pair(C_CYAN),
                    f"{verb} around {_at(hrs[i], app)} ({s:.0f}% cover)")
        cur = cur or st
    avg = sum(s for s in sky if s is not None) / max(1, sum(s is not None for s in sky))
    desc = (
        "Mostly clear" if avg < 30 else "Mostly cloudy" if avg > 70
        else "Mixed sun and cloud"
    )
    return ("Sky", curses.A_DIM, f"{desc} throughout (avg {avg:.0f}% cover)")


def _insights(hrs: List[HourlyPeriod], app: "App") -> List[Insight]:
    is_f = _is_f(hrs)
    out: List[Optional[Insight]] = [
        _precip_insight(hrs, app),
        _thunder_insight(hrs, app),
        _swing_insight(hrs, app, is_f),
        _wind_insight(hrs, app, is_f),
        _feels_insight(hrs, app, is_f),
        _air_insight(hrs, app, is_f),
        _sky_insight(hrs, app),
    ]
    return [i for i in out if i is not None]


# ---------------------------------------------------------------------------
# Condition ribbon
# ---------------------------------------------------------------------------

def _ribbon_cell(h: HourlyPeriod) -> Tuple[str, int]:
    pop = h.pop or 0
    text = h.short_forecast.lower()
    if pop >= 30 or (h.precip_mm or 0) >= 0.25:
        if (h.thunder_pct or 0) >= 15 or "thunder" in text:
            color = C_MAGENTA
        elif "snow" in text or "flurr" in text:
            color = 14
        else:
            color = C_BLUE
        ch = "█" if pop >= 60 else "▓"
        return ch, curses.color_pair(color) | curses.A_BOLD
    if pop >= 15:
        return "▒", curses.color_pair(C_BLUE)
    sky = h.sky_pct
    if sky is None:
        sky = 80.0 if "cloud" in text and "partly" not in text else 40.0
        if "sunny" in text or "clear" in text:
            sky = 10.0 if "mostly" not in text else 25.0
    night = h.is_daytime is False
    if sky < 35:
        return ("·", curses.A_DIM) if night else ("▀", curses.color_pair(C_YELLOW))
    if sky <= 70:
        return "░", curses.A_DIM if night else curses.A_NORMAL
    return "▒", curses.A_DIM


def _group_rep(grp: List[HourlyPeriod]) -> HourlyPeriod:
    """The hour that best represents a ribbon cell: the wettest if any
    precipitation is on the cards, otherwise the middle hour."""
    wet = max(grp, key=lambda h: h.pop or 0)
    if (wet.pop or 0) >= 15:
        return wet
    return grp[len(grp) // 2]


def _draw_ribbon(win, y: int, cols: int, hrs: List[HourlyPeriod], app: "App") -> int:
    """Draw the hour ticks + condition ribbon + legend. Returns rows used."""
    avail = cols - 2
    # Several hours per cell when the whole period won't fit one per column.
    step = max(1, -(-len(hrs) // max(1, avail)))
    groups = [hrs[k:k + step] for k in range(0, len(hrs), step)]
    n = len(groups)
    if n < 6:
        return 0
    cell = clamp(avail // n, 1, 3)
    x0 = max(0, (cols - n * cell) // 2)
    # Past two days, hour ticks get too crowded to tell days apart — label
    # each midnight with its weekday instead.
    by_day = len(hrs) > 48

    # Tick labels, only where they don't collide with the previous one.
    next_free = 0
    for g, grp in enumerate(groups):
        label = ""
        bold = g == 0
        if g == 0:
            label = "now"
        elif by_day:
            midnight = next(
                (t for t in (_local(h.start) for h in grp) if t is not None and t.hour == 0),
                None,
            )
            if midnight is not None:
                label, bold = midnight.strftime("%a"), True
        else:
            t = _local(grp[0].start)
            if t is not None and t.hour % 3 == 0:
                label, bold = _hr(grp[0], app), t.hour == 0
        x = x0 + g * cell
        if label and x >= next_free and x + len(label) <= cols - 1:
            safe_addstr(win, y, x, label, curses.A_BOLD if bold else curses.A_DIM)
            next_free = x + len(label) + 1

    for g, grp in enumerate(groups):
        ch, attr = _ribbon_cell(_group_rep(grp))
        safe_addstr(win, y + 1, x0 + g * cell, ch * cell, attr)

    legend: List[Tuple[str, int]] = [
        ("▀ sun  ", curses.color_pair(C_YELLOW)),
        ("░ partly  ", curses.A_NORMAL),
        ("▒ cloudy  ", curses.A_DIM),
        ("· clear night  ", curses.A_DIM),
        ("▓█ precip  ", curses.color_pair(C_BLUE) | curses.A_BOLD),
        ("█ storms", curses.color_pair(C_MAGENTA) | curses.A_BOLD),
    ]
    width = sum(len(s) for s, _ in legend)
    if width <= cols - 1:
        x = max(0, (cols - width) // 2)
        for s, a in legend:
            safe_addstr(win, y + 2, x, s, a)
            x += len(s)
        return 3
    return 2


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------

def _dew_attr(v: Optional[float], is_f: bool) -> int:
    if v is None:
        return curses.A_DIM
    f = v if is_f else (c_to_f(v) or 0)
    if f >= 70:
        return curses.color_pair(C_RED)
    if f >= 65:
        return curses.color_pair(C_YELLOW)
    if f >= 55:
        return curses.A_NORMAL
    return curses.A_DIM


def _pop_attr(p: Optional[float]) -> int:
    if not p:
        return curses.A_DIM
    if p >= 60:
        return curses.color_pair(C_BLUE) | curses.A_BOLD
    if p >= 30:
        return curses.color_pair(C_BLUE)
    return curses.A_DIM


def _pop_bar(p: Optional[float]) -> str:
    if not p:
        return " "
    return " ▁▂▃▄▅▆▇█"[clamp(round(p / 12.5), 1, 8)]


def _build_rows(hrs: List[HourlyPeriod], app: "App") -> List[TableRow]:
    """Interleave day headers and sunrise/sunset markers with the hours."""
    events: List[Tuple[dt.datetime, str]] = []
    first = _local(hrs[0].start)
    last = _local(hrs[-1].start)
    if first is not None and last is not None:
        day = first.date() - dt.timedelta(days=1)
        while day <= last.date() + dt.timedelta(days=1):
            rise, sset = get_sunrise_sunset(app.lat, app.lon, day)
            for when, label in ((rise, "sunrise"), (sset, "sunset")):
                in_range = when is not None and first < when <= last + dt.timedelta(hours=1)
                # Adjacent days' lookups can yield the same event; keep one.
                if in_range and all(abs((when - e).total_seconds()) > 3600 for e, _ in events):
                    events.append((when, label))
            day += dt.timedelta(days=1)
    events.sort()

    rows: List[TableRow] = []
    prev_date: Optional[dt.date] = None
    for idx, h in enumerate(hrs):
        t = _local(h.start)
        if t is not None and t.date() != prev_date:
            rows.append(("day", t.date()))
            prev_date = t.date()
        rows.append(("hour", idx))
        end = t + dt.timedelta(hours=1) if t is not None else None
        while events and end is not None and events[0][0] < end:
            when, label = events.pop(0)
            rows.append(("sun", (label, when)))
    return rows


def _day_label(d: dt.date) -> str:
    today = dt.date.today()
    if d == today:
        name = "Today"
    elif d == today + dt.timedelta(days=1):
        name = "Tomorrow"
    else:
        name = d.strftime("%A")
    return f"{name} · {d.strftime('%b')} {d.day}"


def draw_hourly(app: "App", win) -> None:
    win.erase()
    rows, cols = win.getmaxyx()
    hrs = app.hourly_periods
    if not hrs:
        safe_addstr(
            win, 0, 0,
            "No hourly forecast available. Press r to refresh.",
            curses.A_DIM,
        )
        win.noutrefresh()
        return

    is_f = _is_f(hrs)
    first, last = _local(hrs[0].start), _local(hrs[-1].start)
    span = ""
    if first is not None and last is not None:
        span = f"  {first.strftime('%a')} {_hr(hrs[0], app)} → " \
               f"{last.strftime('%a')} {_hr(hrs[-1], app)}"
    title = f"Next {len(hrs)} hours"
    safe_addstr(win, 0, 0, title[: cols - 1], curses.A_BOLD)
    safe_addstr(win, 0, len(title), span[: max(0, cols - 30)], curses.A_DIM)

    # ---- At a glance ------------------------------------------------------
    y = 2
    insights = _insights(hrs, app)
    # Keep enough room for a useful table on short terminals.
    max_insights = clamp(rows - 14, 2, len(insights)) if insights else 0
    tag_w = max((len(t) for t, _, _ in insights), default=0) + 2
    for tag, attr, text in insights[:max_insights]:
        safe_addstr(win, y, 1, tag.rjust(tag_w - 2), attr)
        safe_addstr(win, y, tag_w + 1, text[: max(0, cols - tag_w - 2)])
        y += 1
    y += 1

    days = _days(hrs)
    col_w = _grid_col_w(cols, len(days))
    if col_w is not None and len(days) >= 2:
        # Only spend rows on the ribbon if the 24 hour rows still fit after it.
        if rows - y - 4 >= GRID_HEAD + 24 + 1:
            used = _draw_ribbon(win, y, cols, hrs, app)
            y += used + (1 if used else 0)
        _draw_grid(win, y, rows, cols, hrs, app, days, col_w, is_f)
        win.noutrefresh()
        return

    if rows - y >= 12:
        used = _draw_ribbon(win, y, cols, hrs, app)
        y += used + (1 if used else 0)
    _draw_table(win, y, rows, cols, hrs, app, days, is_f)
    win.noutrefresh()


def _draw_table(
    win, y: int, rows: int, cols: int, hrs: List[HourlyPeriod], app: "App",
    days: List[Tuple[dt.date, Dict[int, int]]], is_f: bool,
) -> None:
    """One row per hour, scrolled vertically, for terminals too narrow for
    the day-column grid."""
    # (key, header, width, min terminal cols to show it)
    columns = [
        ("time", "Time", 7, 0),
        ("icon", "", 3, 0),
        ("temp", "Temp", 6, 0),
        ("feels", "Feels", 6, 70),
        ("dew", "Dew", 5, 95),
        ("sky", "Sky", 5, 105),
        ("wind", "Wind", 13, 0),
        ("gust", "Gust", 5, 80),
        ("pop", "Precip", 7, 0),
    ]
    columns = [c for c in columns if cols >= c[3]]
    x = 1
    xs: Dict[str, int] = {}
    for key, _, w, _ in columns:
        xs[key] = x
        x += w + 1
    x_fc = x
    w_fc = max(0, cols - x_fc - 1)

    for key, head, w, _ in columns:
        safe_addstr(win, y, xs[key], head.rjust(w) if key in ("temp", "feels", "dew", "sky",
                                                            "gust", "pop") else head,
                    curses.A_BOLD)
    if w_fc >= 8:
        safe_addstr(win, y, x_fc, "Forecast", curses.A_BOLD)
    y += 1

    table = _build_rows(hrs, app)
    view_rows = max(1, rows - y - 1)
    app.hr_scroll = clamp(app.hr_scroll, 0, max(0, len(table) - view_rows))
    for kind, val in table[app.hr_scroll: app.hr_scroll + view_rows]:
        if y >= rows - 1:
            break
        if kind == "day":
            label = f" {_day_label(val)} "
            safe_addstr(win, y, 0, "──" + label + "─" * max(0, cols - 4 - len(label)),
                        curses.color_pair(C_CYAN) | curses.A_BOLD)
        elif kind == "sun":
            label, when = val
            glyph = "↑" if label == "sunrise" else "↓"
            text = f"☀{glyph} {label} {fmt_time(when, app.use_24h)}"
            safe_addstr(win, y, xs["time"], "┄" * 7, curses.A_DIM)
            safe_addstr(win, y, xs["time"] + 9, text[: max(0, cols - 11)],
                        curses.color_pair(C_YELLOW))
        else:
            _draw_hour_row(win, y, val, hrs, app, xs, x_fc, w_fc, is_f)
        y += 1

    shown_to = min(len(table), app.hr_scroll + view_rows)
    hint = "j/k ↑↓ scroll"
    if len(table) > view_rows:
        hint = f"rows {app.hr_scroll + 1}–{shown_to} of {len(table)} · " + hint
    if len(days) >= 2:
        # +2 for the 1-column margin either side of the body window.
        need = GRID_GUTTER + len(days) * (GRID_MIN_W + 1) + 1 + 2
        hint += f" · widen to {need}+ cols to see days side by side"
    safe_addstr(win, rows - 1, 0, hint[: cols - 1], curses.A_DIM)


# ---------------------------------------------------------------------------
# Day-column grid (wide terminals)
# ---------------------------------------------------------------------------

GRID_GUTTER = 6   # hour-of-day labels down the left edge
GRID_HEAD = 2     # day name + high/low rows above the columns
GRID_MIN_W = 15   # narrowest day column: marker, icon, temp, precip
GRID_WIND_W = 22  # wide enough to add wind
GRID_TEXT_W = 32  # wide enough to add the short forecast


def _days(hrs: List[HourlyPeriod]) -> List[Tuple[dt.date, Dict[int, int]]]:
    """Hour indices grouped by local date, keyed by hour of day."""
    out: List[Tuple[dt.date, Dict[int, int]]] = []
    for idx, h in enumerate(hrs):
        t = _local(h.start)
        if t is None:
            continue
        if not out or out[-1][0] != t.date():
            out.append((t.date(), {}))
        out[-1][1][t.hour] = idx
    return out


def _grid_col_w(cols: int, ndays: int) -> Optional[int]:
    """Width of each day column if every day fits side by side, else None."""
    if ndays <= 0:
        return None
    w = (cols - 1 - GRID_GUTTER) // ndays - 1  # 1 for the separator
    return w if w >= GRID_MIN_W else None


def _sun_marks(days: List[Tuple[dt.date, Dict[int, int]]], app: "App") -> Dict[
    Tuple[dt.date, int], str
]:
    """(date, hour) -> '↑' / '↓' for the hour holding sunrise / sunset."""
    marks: Dict[Tuple[dt.date, int], str] = {}
    for d, _ in days:
        rise, sset = get_sunrise_sunset(app.lat, app.lon, d)
        for when, glyph in ((rise, "↑"), (sset, "↓")):
            t = _local(when)
            if t is not None:
                marks[(t.date(), t.hour)] = glyph
    return marks


def _day_summaries(
    days: List[Tuple[dt.date, Dict[int, int]]], hrs: List[HourlyPeriod], app: "App",
    is_f: bool,
) -> List[Tuple[str, List[Tuple[str, int]]]]:
    """Rows of (gutter label, per-day (text, attr)) summarising each day."""
    light: List[Tuple[str, int]] = []
    feels: List[Tuple[str, int]] = []
    gusts: List[Tuple[str, int]] = []
    rain: List[Tuple[str, int]] = []
    unit = "mph" if is_f else "km/h"
    for d, by_hour in days:
        rise, sset = get_sunrise_sunset(app.lat, app.lon, d)
        if rise and sset:
            # The lookup can hand back the previous evening's sunset (UTC
            # date boundary), so wrap into a single day.
            mins = int((sset - rise).total_seconds() // 60) % (24 * 60)
            light.append((f"{mins // 60}h {mins % 60:02d}m daylight", curses.A_DIM))
        else:
            light.append(("", 0))
        day = [hrs[i] for i in by_hour.values()]
        fl = [_deg(h.apparent_c, is_f) for h in day if h.apparent_c is not None]
        if fl:
            lo, hi = min(fl), max(fl)
            feels.append((f"feels {lo:.0f}°–{hi:.0f}°", temp_attr(hi, is_f)))
        else:
            feels.append(("", 0))
        g = [_speed(h.gust_kmh, is_f) or 0 for h in day if h.gust_kmh is not None]
        if g:
            gusty = max(g) >= (_speed(40.0, is_f) or 0)
            attr = (curses.color_pair(C_YELLOW) | curses.A_BOLD) if gusty else curses.A_DIM
            gusts.append((f"gusts to {max(g):.0f} {unit}", attr))
        else:
            gusts.append(("", 0))
        mm = sum(h.precip_mm for h in day if h.precip_mm is not None)
        if mm >= 0.25:
            amt = f"{mm_to_in(mm):.2f} in" if app.units == "us" else f"{mm:.1f} mm"
            rain.append((f"{amt} precip", curses.color_pair(C_BLUE)))
        else:
            rain.append(("dry", curses.A_DIM))
    return [("sun", light), ("feel", feels), ("wind", gusts), ("wet", rain)]


def _hour_label(hour: int, app: "App") -> str:
    if app.use_24h:
        return f"{hour:02d}:00"
    return f"{(hour % 12) or 12}{'am' if hour < 12 else 'pm'}"


def _draw_grid(
    win, y: int, rows: int, cols: int, hrs: List[HourlyPeriod], app: "App",
    days: List[Tuple[dt.date, Dict[int, int]]], w: int, is_f: bool,
) -> None:
    """Days as columns, hours of the day as rows, so a whole week reads
    across without scrolling."""
    xs = [GRID_GUTTER + k * (w + 1) for k in range(len(days))]
    sep = curses.A_DIM
    # Short labels for every column if any long one won't fit, so they match.
    short = any(len(_day_label(d)) > w for d, _ in days)

    # ---- Day headers: name, then high/low and peak precip chance --------
    for k, (d, by_hour) in enumerate(days):
        x = xs[k] + 1
        safe_addstr(win, y, xs[k], "│", sep)
        safe_addstr(win, y + 1, xs[k], "│", sep)
        label = f"{d.strftime('%a %b')} {d.day}" if short else _day_label(d)
        safe_addstr(win, y, x, label[:w], curses.color_pair(C_CYAN) | curses.A_BOLD)

        temps = [hrs[i].temperature for i in by_hour.values()
                 if hrs[i].temperature is not None]
        if not temps:
            continue
        hi, lo = max(temps), min(temps)
        hi_s, lo_s = f"{hi:.0f}°", f"{lo:.0f}°"
        safe_addstr(win, y + 1, x, hi_s, temp_attr(hi, is_f))
        safe_addstr(win, y + 1, x + len(hi_s), "/", curses.A_DIM)
        safe_addstr(win, y + 1, x + len(hi_s) + 1, lo_s, temp_attr(lo, is_f))
        peak = max((hrs[i].pop or 0 for i in by_hour.values()), default=0)
        px = x + len(hi_s) + 1 + len(lo_s) + 1
        if peak >= 15 and px + 4 <= x + w:
            safe_addstr(win, y + 1, px, f"{peak:.0f}%", _pop_attr(peak))
    y += GRID_HEAD

    # ---- Hour rows -------------------------------------------------------
    view_rows = max(1, rows - y - 1)
    app.hr_scroll = clamp(app.hr_scroll, 0, max(0, 24 - view_rows))
    marks = _sun_marks(days, app)
    for hour in range(app.hr_scroll, min(24, app.hr_scroll + view_rows)):
        safe_addstr(win, y, 0, _hour_label(hour, app).rjust(GRID_GUTTER - 1), curses.A_DIM)
        for k, (d, by_hour) in enumerate(days):
            safe_addstr(win, y, xs[k], "│", sep)
            idx = by_hour.get(hour)
            if idx is not None:
                _draw_grid_cell(win, y, xs[k] + 1, w, hrs[idx], idx == 0,
                                marks.get((d, hour), ""), is_f)
        y += 1

    # Spare rows below the hours: a per-day summary.
    summary = _day_summaries(days, hrs, app, is_f)
    if view_rows >= 24 and rows - 1 - y >= len(summary) + 1:
        safe_addstr(win, y, 0, "─" * (GRID_GUTTER - 1), sep)
        for k in range(len(days)):
            safe_addstr(win, y, xs[k], "┼" + "─" * w, sep)
        y += 1
        for label, per_day in summary:
            safe_addstr(win, y, 0, label.rjust(GRID_GUTTER - 1), curses.A_DIM)
            for k, (text, attr) in enumerate(per_day):
                safe_addstr(win, y, xs[k], "│", sep)
                safe_addstr(win, y, xs[k] + 2, text[: w - 1], attr)
            y += 1

    hint = "▸ now  ↑ sunrise  ↓ sunset"
    if view_rows < 24:
        shown_to = min(24, app.hr_scroll + view_rows)
        hint = f"hours {app.hr_scroll + 1}–{shown_to} of 24 · j/k ↑↓ scroll · " + hint
    safe_addstr(win, rows - 1, 0, hint[: cols - 1], curses.A_DIM)


def _draw_grid_cell(
    win, y: int, x: int, w: int, h: HourlyPeriod, now: bool, sun: str, is_f: bool,
) -> None:
    """One hour in a day column: marker, icon, temp, precip [, wind [, text]]."""
    if now:
        safe_addstr(win, y, x, "▸", curses.color_pair(C_CYAN) | curses.A_BOLD)
    elif sun:
        safe_addstr(win, y, x, sun, curses.color_pair(C_YELLOW))

    icon = ICON_TINY.get(h.icon_key, "?") or "?"
    safe_addstr(win, y, x + 1, icon[:2])

    temp = "—" if h.temperature is None else f"{h.temperature:.0f}°"
    attr = temp_attr(h.temperature, is_f) | (curses.A_BOLD if now else 0)
    safe_addstr(win, y, x + 4, temp.rjust(4), attr)

    if h.pop and h.pop >= MIN_POP:
        pop_attr = _pop_attr(h.pop)
        safe_addstr(win, y, x + 9, f"{h.pop:.0f}%".rjust(4), pop_attr)
        safe_addstr(win, y, x + 13, _pop_bar(h.pop), pop_attr)

    if w >= GRID_WIND_W:
        wind = _wind_label(h, with_unit=False)
        gust = _speed(h.gust_kmh, is_f)
        gusty = gust is not None and gust >= (_speed(40.0, is_f) or 0)
        wattr = (curses.color_pair(C_YELLOW) | curses.A_BOLD) if gusty else curses.A_DIM
        safe_addstr(win, y, x + 15, wind[:6], wattr)

    if w >= GRID_TEXT_W:
        fc = h.short_forecast or "—"
        if (h.thunder_pct or 0) >= 15 and "thunder" not in fc.lower():
            fc = f"{fc} (thunder)"
        safe_addstr(win, y, x + 22, fc[: w - 22])


def _draw_hour_row(
    win, y: int, idx: int, hrs: List[HourlyPeriod], app: "App",
    xs: Dict[str, int], x_fc: int, w_fc: int, is_f: bool,
) -> None:
    h = hrs[idx]
    now = idx == 0
    tstr = "now" if now else fmt_time(h.start, app.use_24h)
    safe_addstr(win, y, xs["time"], tstr[:7].rjust(7), curses.A_BOLD if now else curses.A_NORMAL)

    icon = ICON_TINY.get(h.icon_key, "?") or "?"
    safe_addstr(win, y, xs["icon"] + 1, icon[:2])

    temp = "—" if h.temperature is None else f"{h.temperature:.0f}°"
    safe_addstr(win, y, xs["temp"], temp.rjust(6), temp_attr(h.temperature, is_f))

    if "feels" in xs:
        feels = _deg(h.apparent_c, is_f)
        if feels is None:
            safe_addstr(win, y, xs["feels"], "—".rjust(6), curses.A_DIM)
        elif h.temperature is not None and abs(feels - h.temperature) < _delta(3, is_f):
            pass  # same as the air temperature — leave blank
        else:
            safe_addstr(win, y, xs["feels"], f"{feels:.0f}°".rjust(6),
                        temp_attr(feels, is_f))

    if "dew" in xs:
        dew = _deg(h.dewpoint_c, is_f)
        dstr = "—" if dew is None else f"{dew:.0f}°"
        safe_addstr(win, y, xs["dew"], dstr.rjust(5), _dew_attr(dew, is_f))

    if "sky" in xs:
        sstr = "—" if h.sky_pct is None else f"{h.sky_pct:.0f}%"
        safe_addstr(win, y, xs["sky"], sstr.rjust(5), curses.A_DIM)

    wind = _wind_label(h, with_unit=True)
    safe_addstr(win, y, xs["wind"], wind[:13], curses.A_DIM if wind == "calm" else 0)

    if "gust" in xs:
        gust = _speed(h.gust_kmh, is_f)
        if gust is None:
            safe_addstr(win, y, xs["gust"], "—".rjust(5), curses.A_DIM)
        else:
            gusty = gust >= (_speed(40.0, is_f) or 0)
            attr = (curses.color_pair(C_YELLOW) | curses.A_BOLD) if gusty else curses.A_DIM
            safe_addstr(win, y, xs["gust"], f"{gust:.0f}".rjust(5), attr)

    if h.pop is not None and h.pop >= MIN_POP:
        pop_attr = _pop_attr(h.pop)
        safe_addstr(win, y, xs["pop"], f"{h.pop:.0f}%".rjust(5), pop_attr)
        safe_addstr(win, y, xs["pop"] + 6, _pop_bar(h.pop), pop_attr)

    if w_fc >= 8:
        fc = h.short_forecast or "—"
        if (h.thunder_pct or 0) >= 15 and "thunder" not in fc.lower():
            fc += f" (thunder {h.thunder_pct:.0f}%)"
        safe_addstr(win, y, x_fc, fc[:w_fc])
