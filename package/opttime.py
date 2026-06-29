"""
opttime - time/date module for Optimize

    library opttime

    display now()              ! current local time as a string
    display today()             ! current date as a string
    var t1 = timestamp()         ! seconds since epoch (float)
    sleep(1)                      ! pause execution for 1 second
    var t2 = timestamp()
    display elapsed(t1, t2)        ! seconds between two timestamps
"""

import time as _time
import datetime as _datetime


def now():
    """Current local date and time as a string."""
    return _datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    """Current local date as a string."""
    return _datetime.date.today().strftime("%Y-%m-%d")


def clock_time():
    """Current local time only, as a string."""
    return _datetime.datetime.now().strftime("%H:%M:%S")


def timestamp():
    """Seconds since the epoch, as a float. Useful for measuring
    elapsed time between two points in a script."""
    return _time.time()


def elapsed(start_timestamp, end_timestamp):
    """Seconds between two timestamp() values."""
    return end_timestamp - start_timestamp


def wait(seconds):
    """Pause execution for the given number of seconds."""
    _time.sleep(seconds)


def year():
    return _datetime.date.today().year


def month():
    return _datetime.date.today().month


def day():
    return _datetime.date.today().day


def weekday():
    """Day of the week as a string, e.g. 'Monday'."""
    return _datetime.date.today().strftime("%A")