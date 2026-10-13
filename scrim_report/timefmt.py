import calendar
import time


# red01's clock is UTC — timegm, not localtime
def parse_ts(ts):
    return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))


def t_plus(ts_epoch, t0):
    return None if t0 is None else (ts_epoch - t0) / 60.0


def fmt_t(tp):
    return f"T+{tp:.0f}" if tp is not None else "?"
