"""GNSS time for the system clock: IMU time stamps -> samples -> chrony (SHM).

The OpenRTK330 stamps every IMU message with GNSS time (UTC) once it has time from the satellites
(a 1980 stamp before that). Without Internet (no NTP) and with a hardware clock that may have lost
its time (seen: a boot at 1970-01-01), this is the only source of the real time on the device.

A message arrives a few ms after its stamp. Measured on the device against an NTP-synced clock
(8 h, with DSO crashing at night, full CPU load, a frozen driver, a frozen dashboard): the *fastest*
message of each second arrived 4.6 ms (median) after its stamp; estimates stayed within 4.5 ms of NTP
(99.9 %) and 4.9 ms at worst, which is about as good as Internet NTP itself. The median message
drifted more, and a burst of delayed messages after a stall once put a median-based estimate 1.2 s
off. So:

* collect (receive time, stamp) of the IMU messages of a short window (WINDOW_S);
* refuse the window unless the stamps are real (after 2020), advance, and there are enough;
* take the message with the smallest delay: clock time = stamp + latency_s, receive time = the local
  time it arrived;
* send it to chrony only when it agrees (within AGREE_S) with the median of the last samples, or
  when CONFIRM samples in a row agree on a new value (the system clock itself stepped).

chrony gets the pairs through the NTP shared-memory protocol (refclock SHM, segment created by
chronyd with perm=0666, so the dashboard needs no root) and decides itself: NTP when there is
Internet, GNSS when not, a step when the clock is far off, the RTC kept by rtcsync.
"""

import ctypes
import ctypes.util
import statistics
from typing import List, Optional, Tuple

MIN_VALID_STAMP = 1577836800.0      # 2020-01-01: before that the receiver has no GNSS time
SHM_KEY_BASE = 0x4E545030           # "NTP0", the NTP SHM convention (unit 0..)


class shmTime(ctypes.Structure):
    """struct shmTime of ntpd / gpsd / chrony (refclock_shm.c), native layout."""
    _fields_ = [("mode", ctypes.c_int), ("count", ctypes.c_int),
                ("clockTimeStampSec", ctypes.c_long), ("clockTimeStampUSec", ctypes.c_int),
                ("receiveTimeStampSec", ctypes.c_long), ("receiveTimeStampUSec", ctypes.c_int),
                ("leap", ctypes.c_int), ("precision", ctypes.c_int), ("nsamples", ctypes.c_int),
                ("valid", ctypes.c_int), ("clockTimeStampNSec", ctypes.c_uint),
                ("receiveTimeStampNSec", ctypes.c_uint), ("dummy", ctypes.c_int * 8)]


def _split(t: float) -> Tuple[int, int]:
    sec = int(t // 1)
    nsec = int(round((t - sec) * 1e9))
    if nsec >= 1_000_000_000:
        sec, nsec = sec + 1, nsec - 1_000_000_000
    return sec, nsec


def put_sample(seg: shmTime, clock_t: float, receive_t: float, precision: int = -7) -> None:
    """Write one sample (mode 1: count changes around the update, then valid)."""
    seg.valid = 0
    seg.count += 1
    cs, cn = _split(clock_t)
    rs, rn = _split(receive_t)
    seg.mode = 1
    seg.clockTimeStampSec, seg.clockTimeStampUSec, seg.clockTimeStampNSec = cs, cn // 1000, cn
    seg.receiveTimeStampSec, seg.receiveTimeStampUSec, seg.receiveTimeStampNSec = rs, rn // 1000, rn
    seg.leap, seg.precision, seg.nsamples = 0, precision, 1
    seg.count += 1
    seg.valid = 1


class ChronyShm:
    """The SHM segment of `refclock SHM <unit>:perm=0666` in chrony.conf. chronyd creates it; if it
    does not exist, chrony is not set up for GNSS time and open() says so."""

    def __init__(self, unit: int = 2):
        self.unit = unit
        self.seg: Optional[shmTime] = None
        self.error = ""

    def open(self) -> bool:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.shmat.restype = ctypes.c_void_p
        libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        shmid = libc.shmget(SHM_KEY_BASE + self.unit, ctypes.sizeof(shmTime), 0)
        if shmid < 0:
            self.error = (f"no chrony SHM segment {self.unit} (errno {ctypes.get_errno()}): "
                          "chrony is not set up for GNSS time")
            return False
        addr = libc.shmat(shmid, None, 0)
        if addr in (None, ctypes.c_void_p(-1).value):
            self.error = f"cannot attach chrony SHM segment {self.unit} (errno {ctypes.get_errno()})"
            return False
        self.seg = shmTime.from_address(addr)
        self.error = ""
        return True

    def put(self, clock_t: float, receive_t: float) -> bool:
        if self.seg is None and not self.open():
            return False
        put_sample(self.seg, clock_t, receive_t)
        return True


class OffsetWindow:
    """IMU messages of one window -> one (clock, receive) sample, or the reason why not."""

    def __init__(self, latency_s: float, min_messages: int = 20):
        self.latency_s, self.min_messages = latency_s, min_messages
        self.items: List[Tuple[float, float]] = []      # (receive time, stamp)

    def add(self, receive_t: float, stamp: float) -> None:
        self.items.append((receive_t, stamp))

    def sample(self) -> Tuple[Optional[Tuple[float, float]], str, Optional[List[float]]]:
        """((clock_t, receive_t) or None, why not, the delays receive - stamp of this window)."""
        items, self.items = self.items, []
        if len(items) < self.min_messages:
            return None, f"{len(items)} IMU messages", None
        stamps = [s for _, s in items]
        if min(stamps) < MIN_VALID_STAMP:
            return None, "no GNSS time yet", None
        if max(stamps) - min(stamps) < 0.5 * (items[-1][0] - items[0][0]):
            return None, "time stamps do not advance", None
        delays = [r - s for r, s in items]
        k = min(range(len(delays)), key=delays.__getitem__)       # the fastest message
        r, s = items[k]
        return (s + self.latency_s, r), "", delays


class GnssClockFeeder:
    """Fed with every IMU message (receive wall time, stamp); sends checked samples to chrony.

    status() is published for the pages and the start-up coordinator ("clock matches GNSS")."""

    WINDOW_S = 0.5          # samples per window of receive time (the IMU is listened to in windows)
    HISTORY = 7             # samples remembered for the agreement check
    AGREE_S = 0.020         # a sample within this of the recent median is accepted
    CONFIRM = 3             # this many in a row agreeing on a new value: accepted (the clock stepped)

    def __init__(self, latency_s: float, shm: Optional["ChronyShm"] = None, clock=None):
        import threading
        import time as _time
        self.latency_s = latency_s
        self.shm = shm
        self._now = clock or _time.monotonic
        self._win = OffsetWindow(latency_s)
        self._win_start: Optional[float] = None
        self._recent: List[float] = []              # offsets (clock - receive) of accepted samples
        self._pending: List[float] = []             # disagreeing offsets in a row
        self._lock = threading.Lock()
        self.sent = self.refused = 0
        self.last_offset_s: Optional[float] = None  # GNSS time - system time, last accepted sample
        self.last_at: Optional[float] = None        # monotonic time of that sample
        self.last_reason = "no IMU message yet"
        self._shm_retry_at = 0.0

    def add(self, receive_t: float, stamp: Optional[float]) -> None:
        if stamp is None:
            return
        now = self._now()
        with self._lock:
            if self._win_start is None:
                self._win_start = now
            elif now - self._win_start >= self.WINDOW_S:
                self._close_window()
                self._win_start = now
            self._win.add(receive_t, stamp)

    def _close_window(self) -> None:
        sample, why, _ = self._win.sample()
        if sample is None:
            self.refused += 1
            self.last_reason = why
            return
        clock_t, recv_t = sample
        off = clock_t - recv_t
        if len(self._recent) >= self.CONFIRM:
            med = sorted(self._recent)[len(self._recent) // 2]
            if abs(off - med) > self.AGREE_S:
                self._pending.append(off)
                if len(self._pending) < self.CONFIRM or \
                        max(self._pending) - min(self._pending) > self.AGREE_S:
                    if len(self._pending) >= self.CONFIRM:
                        self._pending = self._pending[1:]
                    self.refused += 1
                    self.last_reason = f"sample {off * 1000:+.1f} ms off the recent ones: held back"
                    return
                self._recent = list(self._pending)          # the system clock stepped: follow it
        self._pending = []
        self._recent = (self._recent + [off])[-self.HISTORY:]
        self.last_offset_s, self.last_at, self.last_reason = off, self._now(), ""
        if len(self._recent) < self.CONFIRM:
            return                                       # warming up: nothing sent yet
        if self.shm is not None and (self.shm.seg is not None or self._now() >= self._shm_retry_at):
            if self.shm.put(clock_t, recv_t):
                self.sent += 1
            else:
                self._shm_retry_at = self._now() + 60.0

    def status(self) -> dict:
        with self._lock:
            age = None if self.last_at is None else self._now() - self.last_at
            return {"chrony": bool(self.shm and self.shm.seg is not None),
                    "chrony_error": (self.shm.error if self.shm else "not configured") or "",
                    "offset_s": None if self.last_offset_s is None else round(self.last_offset_s, 4),
                    "age_s": None if age is None else round(age, 1),
                    "sent": self.sent, "refused": self.refused, "reason": self.last_reason}
