from __future__ import annotations

from datetime import timedelta, timezone, tzinfo, UTC
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_timezone(name: str) -> tzinfo:
    """Return an IANA timezone, with a dependency-free WIB fallback on Windows."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name in {"UTC", "Etc/UTC", "Etc/GMT"}:
            return UTC
        if name == "Asia/Jakarta":
            # Western Indonesian Time is UTC+7 year-round and has no daylight saving time.
            return timezone(timedelta(hours=7), name="WIB")
        raise
