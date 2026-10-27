import calendar
import time


EPOCH = "1970-01-01T00:00:00"


# red01's clock is UTC — timegm, not localtime
def parse_ts(ts):
    """Epoch seconds; a missing or malformed stamp reads as the epoch, never a crash.

    `parse_ts` used to raise on the defaults its own callers passed ("1970", ""), so one
    event line without a timestamp took the whole report down.
    """
    try:
        return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return calendar.timegm(time.strptime(EPOCH, "%Y-%m-%dT%H:%M:%S"))


def t_plus(ts_epoch, t0):
    return None if t0 is None else (ts_epoch - t0) / 60.0


def fmt_t(tp):
    return f"T+{tp:.0f}" if tp is not None else "?"
