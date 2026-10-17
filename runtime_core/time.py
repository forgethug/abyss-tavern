"""UTC clock compatible with the platform's existing naive UTC columns."""
from datetime import datetime, timezone


def utc_now() -> datetime:
    """Keep persisted values naive; attach UTC only at serialization boundaries."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_from_timestamp(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)
