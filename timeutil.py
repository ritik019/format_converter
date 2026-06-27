from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
]


def parse_ist_to_epoch_ms(value):
    s = str(value).strip()
    for fmt in _FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=IST).timestamp() * 1000)
    raise ValueError(f"Unrecognized date-time format: {value!r}")
