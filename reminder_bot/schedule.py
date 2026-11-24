from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

HOUR = 3600
DAY = 24 * HOUR


@dataclass(frozen=True)
class Plan:
    kind: str = 'default'
    minutes: int = 0
    weekday: int = 0
    starts_at: int = 0
    interval: int = 0
    month_day: int = 1


def local_datetime(timestamp: int, zone: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(timestamp, zone)


def shift_month(month: date, delta: int) -> date:
    year, zero_based_month = divmod(month.year * 12 + month.month - 1 + delta, 12)
    return date(year, zero_based_month + 1, 1)


def wall_timestamp(day: date, minutes: int, zone: ZoneInfo) -> int | None:
    local = datetime.combine(day, time(minutes // 60, minutes % 60), tzinfo=zone)
    timestamp = int(local.timestamp())
    if local_datetime(timestamp, zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        return None
    return timestamp


def next_wall_time(after: int, minutes: int, zone: ZoneInfo, weekday: int | None = None) -> int:
    day = local_datetime(after, zone).date()
    while True:
        if weekday is None or day.weekday() == weekday:
            timestamp = wall_timestamp(day, minutes, zone)
            if timestamp is not None and timestamp > after:
                return timestamp
        day += timedelta(days=1)


def next_recurring(plan: Plan, zone: ZoneInfo, after: int) -> tuple[str, int] | None:
    if plan.kind == 'default':
        return 'midnight', next_wall_time(after, 0, zone)
    if plan.kind == 'daily':
        return 'daily', next_wall_time(after, plan.minutes, zone)
    if plan.kind == 'weekly':
        return 'weekly', next_wall_time(after, plan.minutes, zone, plan.weekday)
    if plan.kind == 'monthly':
        month = local_datetime(after, zone).date().replace(day=1)
        while True:
            day = min(plan.month_day, monthrange(month.year, month.month)[1])
            timestamp = wall_timestamp(month.replace(day=day), plan.minutes, zone)
            if timestamp is not None and timestamp > after:
                return 'monthly', timestamp
            month = shift_month(month, 1)
    if plan.kind == 'interval':
        steps = max(1, (after - plan.starts_at) // plan.interval + 1)
        return 'interval', plan.starts_at + steps * plan.interval
    return None


def regular_occurrences(plan: Plan, zone: ZoneInfo, now: int) -> list[tuple[str, int]]:
    occurrences = []
    if plan.kind == 'once' and plan.starts_at > now:
        occurrences.append(('once', plan.starts_at))
    recurring = next_recurring(plan, zone, now)
    if recurring is not None:
        occurrences.append(recurring)
    return occurrences


def utc_now() -> int:
    return int(datetime.now(UTC).timestamp())
