from runtime_core.time import utc_now
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


BEIJING_OFFSET = timedelta(hours=8)
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def get_beijing_time(value=None):
    if value is None:
        value = utc_now()
    return value + BEIJING_OFFSET


def get_beijing_today_start_utc():
    beijing_now = get_beijing_time()
    beijing_midnight = datetime(
        beijing_now.year,
        beijing_now.month,
        beijing_now.day,
    )
    return beijing_midnight - BEIJING_OFFSET


def to_utc_iso(value):
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_date(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def beijing_str_to_utc(value: str, format_string: str = "%Y-%m-%d %H:%M:%S"):
    local_value = datetime.strptime(value, format_string).replace(tzinfo=BEIJING_TZ)
    return local_value.astimezone(timezone.utc).replace(tzinfo=None)
