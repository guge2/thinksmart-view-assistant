#!/usr/bin/python3
import json
import logging
import os
import subprocess
import sys
import time

ANCHOR = "/var/lib/thinksmart-clock/anchor.json"
RTC = "/sys/class/rtc/rtc0/since_epoch"
MIN_REALTIME = 1735689600  # 2025-01-01 UTC
MAX_REALTIME = 4102444800  # 2100-01-01 UTC
MAX_ELAPSED = 366 * 86400


def rtc_seconds():
    with open(RTC, encoding="ascii") as source:
        return int(source.read().strip())


def synchronized():
    try:
        result = subprocess.run(
            ["/usr/bin/timedatectl", "show", "--property=NTPSynchronized", "--value"],
            text=True, capture_output=True, timeout=3, check=True,
        )
        return result.stdout.strip() == "yes"
    except (OSError, subprocess.SubprocessError):
        return False


def save():
    if not synchronized():
        logging.info("Network time is not synchronized; keeping the previous clock anchor")
        return
    # Take the readings together to limit the offset error to approximately one second.
    rtc = rtc_seconds()
    realtime = int(time.time())
    if not MIN_REALTIME <= realtime < MAX_REALTIME:
        raise ValueError("synchronized system time is outside the supported range")
    try:
        with open(ANCHOR, encoding="ascii") as source:
            previous = json.load(source)
        old_rtc, old_realtime = previous["rtc"], previous["realtime"]
        if (type(old_rtc) is int and type(old_realtime) is int and 0 <= rtc - old_rtc < 7 * 86400
                and abs((realtime - rtc) - (old_realtime - old_rtc)) <= 2):
            return
    except (OSError, ValueError, KeyError, TypeError):
        pass
    anchor = {"rtc": rtc, "realtime": realtime}
    temporary = ANCHOR + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as target:
        json.dump(anchor, target)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, ANCHOR)
    directory = os.open(os.path.dirname(ANCHOR), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    logging.info("Saved a network-synchronized RTC anchor")


def restore():
    # This runtime marker avoids waiting for D-Bus during early boot.
    if os.path.exists("/run/systemd/timesync/synchronized"):
        logging.info("Network time is already synchronized")
        return
    try:
        with open(ANCHOR, encoding="ascii") as source:
            anchor = json.load(source)
        old_rtc = anchor["rtc"]
        old_realtime = anchor["realtime"]
        if type(old_rtc) is not int or type(old_realtime) is not int:
            raise ValueError("invalid clock anchor")
        current_rtc = rtc_seconds()
        elapsed = current_rtc - old_rtc
        candidate = old_realtime + elapsed
        if not (0 <= elapsed <= MAX_ELAPSED and MIN_REALTIME <= old_realtime < MAX_REALTIME
                and candidate < MAX_REALTIME):
            raise ValueError("RTC was reset or clock anchor is out of range")
        now = time.time()
        if candidate > now:
            time.clock_settime(time.CLOCK_REALTIME, candidate)
            logging.info("Restored system time from the calibrated RTC counter")
        else:
            logging.info("Existing system time is at least as recent as the RTC estimate")
    except FileNotFoundError:
        logging.info("No RTC anchor is available; leaving system time unchanged")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("save", "restore"):
            raise ValueError("expected save or restore")
        (save if sys.argv[1] == "save" else restore)()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        logging.warning("Clock anchor unavailable: %s", error)


if __name__ == "__main__":
    main()
