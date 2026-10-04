import calendar
import time


def parse_ts(ts):
    """ISO ts on red01's clock (UTC, optional fraction) -> epoch."""
    return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))


def t_plus(ts_epoch, t0):
    return None if t0 is None else (ts_epoch - t0) / 60.0


def fmt_t(tp):
    return f"T+{tp:.0f}" if tp is not None else "?"
