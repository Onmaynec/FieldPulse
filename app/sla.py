"""SLA uses immutable per-ticket snapshots; calculations are in the site's time zone."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_WEEK = {
    str(d): [["09:00", "18:00"]] for d in range(5)
}

def parse_hhmm(s):
    hour, minute = (int(v) for v in s.split(":"))
    return time(hour, minute)

def intervals(day, calendar):
    if day.isoformat() in set(calendar.get("holidays", [])):
        return []
    zone = ZoneInfo(calendar["timezone"])
    spans = calendar.get("week", DEFAULT_WEEK).get(str(day.weekday()), [])
    return [
        (datetime.combine(day, parse_hhmm(a), tzinfo=zone).astimezone(timezone.utc),
         datetime.combine(day, parse_hhmm(b), tzinfo=zone).astimezone(timezone.utc))
        for a, b in spans
    ]

def deadline(start, minutes, calendar):
    if minutes < 0:
        raise ValueError("minutes must be nonnegative")
    cursor = aware(start)
    if minutes == 0:
        return cursor
    zone = ZoneInfo(calendar["timezone"])
    first_day = cursor.astimezone(zone).date()
    for offset in range(3660):
        day = first_day + timedelta(days=offset)
        for a, b in intervals(day, calendar):
            if b <= cursor:
                continue
            segment = max(a, cursor)
            available = (b - segment).total_seconds() / 60
            if available >= minutes:
                return segment + timedelta(minutes=minutes)
            minutes -= available
            cursor = b
    raise ValueError("SLA calendar has no available intervals within ten years")

def working_minutes(start, end, calendar):
    start, end = aware(start), aware(end)
    if end <= start:
        return 0.0
    zone = ZoneInfo(calendar["timezone"])
    current_day = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    total = 0.0
    while current_day <= last:
        for a, b in intervals(current_day, calendar):
            total += max(0, (min(end, b) - max(start, a)).total_seconds() / 60)
        current_day += timedelta(days=1)
    return total

def aware(dt):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def apply_pause(original_deadline, start, end, calendar):
    """Extend by working minutes actually blocked by an approved pause."""
    minutes = working_minutes(start, end, calendar)
    return deadline(original_deadline, minutes, calendar)

def snapshot(site, contract, policy):
    c = dict(contract.calendar)
    c["timezone"] = site.timezone
    return {
        "policy_id": policy.id,
        "policy_name": policy.name,
        "priority": policy.priority,
        "reaction_minutes": policy.reaction_minutes,
        "resolution_minutes": policy.resolution_minutes,
        "allowed_pauses": list(policy.allowed_pauses),
        "calendar": c,
    }
