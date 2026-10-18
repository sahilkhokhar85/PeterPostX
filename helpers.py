"""Pure helper functions (no Telegram / database code)."""
import calendar
import re
from datetime import datetime, timedelta

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def bold(text):
    """Make a whole message bold, but leave <code>...</code> parts as they are (monospace)."""
    text = text.replace("<b>", "").replace("</b>", "")
    out = []
    for part in re.split(r"(<code>.*?</code>)", text, flags=re.S):
        if part.startswith("<code>") or not part.strip():
            out.append(part)
            continue
        lead = part[: len(part) - len(part.lstrip())]
        trail = part[len(part.rstrip()):]
        out.append(f"{lead}<b>{part.strip()}</b>{trail}")
    return "".join(out)


def parse_times(s, max_n=12):
    """'09:00, 6:30pm' -> ['09:00', '18:30'] (sorted, unique) or None if invalid."""
    found = re.findall(r"(\d{1,2}):(\d{2})\s*(am|pm)?", s.lower())
    if not found:
        return None
    out = set()
    for hh, mm, ap in found:
        hh, mm = int(hh), int(mm)
        if ap:
            if not 1 <= hh <= 12:
                return None
            hh = hh % 12 + (12 if ap == "pm" else 0)
        if hh > 23 or mm > 59:
            return None
        out.add(f"{hh:02d}:{mm:02d}")
    if len(out) > max_n:
        return None
    return sorted(out)


def days_label(days):
    s = sorted(set(days))
    if s == list(range(7)):
        return "Every day"
    if s == [0, 1, 2, 3, 4]:
        return "Weekdays (Mon-Fri)"
    if s == [5, 6]:
        return "Weekend (Sat-Sun)"
    return ", ".join(DAY_NAMES[i] for i in s)


def days_short(days):
    s = sorted(set(days))
    if s == list(range(7)):
        return "Daily"
    if s == [0, 1, 2, 3, 4]:
        return "Weekdays"
    if s == [5, 6]:
        return "Weekend"
    return ",".join(DAY_NAMES[i] for i in s)


def apply_day_action(selected, act, arg=None):
    sel = set(selected)
    if act == "t":
        sel ^= {int(arg)}
    elif act == "all":
        sel = set(range(7))
    elif act == "wk":
        sel = set(range(5))
    elif act == "we":
        sel = {5, 6}
    return sorted(sel)


def occurrences(days, times, after_ts, tz, end_date=None, limit=1):
    """Next `limit` timestamps (strictly after after_ts) matching the weekdays/times."""
    out = []
    start = datetime.fromtimestamp(after_ts, tz).date()
    for off in range(0, 800):
        d = start + timedelta(days=off)
        if end_date and d > end_date:
            break
        if d.weekday() not in days:
            continue
        for t in sorted(times):
            hh, mm = map(int, t.split(":"))
            ts = int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).timestamp())
            if ts > after_ts:
                out.append(ts)
                if len(out) >= limit:
                    return out
    return out


def month_matrix(year, month):
    return calendar.Calendar(0).monthdayscalendar(year, month)
