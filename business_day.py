"""
Nexo POS - Business day / timezone helpers

Feature 3 requires business-day boundaries to respect each store's own
timezone, not UTC or the server's local time. All timestamps in this app are
stored as naive UTC strings (SQLite's CURRENT_TIMESTAMP). This module is the
one place that converts between "business_date in store-local time" and the
naive-UTC datetime range needed to filter rows in SQL — no ad hoc date math
anywhere else.

Assumption (flagged in the Phase 4 summary): the business day runs local
midnight-to-midnight. The brief didn't specify a different cutover hour (e.g.
a store that closes its books at 6am); add a per-store business_day_start_hour
column if one is needed later — this module's shape doesn't need to change,
only business_date_bounds_utc's use of time.min.
"""

from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = ZoneInfo("UTC")

SQLITE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def is_valid_timezone(name):
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False


def business_date_bounds_utc(business_date, timezone_name):
    """[start, end) as naive UTC datetime strings (SQLite CURRENT_TIMESTAMP
    format) covering the full local business day for a store in
    `timezone_name`. `business_date` is a date (or 'YYYY-MM-DD' string)."""
    if isinstance(business_date, str):
        business_date = date.fromisoformat(business_date)
    tz = ZoneInfo(timezone_name)
    local_start = datetime.combine(business_date, time.min, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    start_utc = local_start.astimezone(UTC).replace(tzinfo=None)
    end_utc = local_end.astimezone(UTC).replace(tzinfo=None)
    return start_utc.strftime(SQLITE_DATETIME_FORMAT), end_utc.strftime(SQLITE_DATETIME_FORMAT)


def current_business_date(timezone_name):
    """Today's date in the store's local timezone (for defaulting a form field
    — the actual query bounds always come from business_date_bounds_utc)."""
    return datetime.now(ZoneInfo(timezone_name)).date()


def previous_business_date(business_date):
    if isinstance(business_date, str):
        business_date = date.fromisoformat(business_date)
    return business_date - timedelta(days=1)
