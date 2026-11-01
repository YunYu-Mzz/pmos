"""A transient EPERM from the OS is retried; any other error is not.

Anhui 2026-11-01 02:08 (and 00:43, 01:01): macOS briefly refused a launchd
process access to ~/Desktop with EPERM for a few seconds; a state write that
failed then left a job "running" that nobody resumed. EACCES (a real
permission problem) is never retried.
"""
from __future__ import annotations

import errno
import time

EPERM_RETRIES = 30
EPERM_DELAY_SECONDS = 0.5


def eperm_retry(function, *args, sleep=time.sleep, **kwargs):
    for attempt in range(EPERM_RETRIES + 1):
        try:
            return function(*args, **kwargs)
        except PermissionError as exc:
            if exc.errno != errno.EPERM or attempt == EPERM_RETRIES:
                raise
            sleep(EPERM_DELAY_SECONDS)
