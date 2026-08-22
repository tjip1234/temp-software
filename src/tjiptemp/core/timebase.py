"""Mapping the board's monotonic microsecond clock onto host UTC.

The board has no RTC. It counts microseconds since boot, and every sample is
labelled with that count. Turning those into real timestamps is the host's job,
and doing it properly is the difference between "roughly when it happened" and
timestamps you can put in a report.

The naive approach -- stamp each sample with the host clock when it arrives -- is
wrong in a way that looks right. USB and WiFi both deliver in bursts, so arrival
times cluster and then gap, and the resulting timestamps have several milliseconds
of jitter *and* are unevenly spaced even though the samples were taken on a
perfectly regular tick.

Instead we fit. Each TIME_SYNC exchange gives an SNTP-style four-timestamp sample:

    offset = ((t2 - t1) + (t3 - t4)) / 2
    rtt    = (t4 - t1) - (t3 - t2)

Low-RTT exchanges are far more accurate than high-RTT ones (the error is bounded
by rtt/2), so we keep a rolling window, discard outliers, and weighted-least-squares
a line ``utc_ns = a * dev_us + b``. The slope gives the crystal's drift in ppm,
which is genuinely useful: a board reporting 40 ppm is a board whose "100 Hz" is
really 99.996 Hz, and over an eight-hour soak that is more than a second of skew.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field

NS_PER_US = 1000.0

#: How many exchanges to keep. 64 at one every 30 s is half an hour of history --
#: long enough to see drift, short enough to track a warming crystal.
WINDOW = 64
#: Reject an exchange whose round trip exceeds this multiple of the running median.
RTT_OUTLIER_FACTOR = 4.0
#: Minimum exchanges before the fit is trusted for anything but a rough offset.
MIN_FOR_FIT = 4
#: Below this many, or before any sync, we are in "estimated" mode.
MIN_FOR_DRIFT = 8


@dataclass(slots=True)
class SyncSample:
    t1_host_ns: int
    t2_dev_us: int
    t3_dev_us: int
    t4_host_ns: int

    @property
    def offset_ns(self) -> float:
        """Host time minus device time, in nanoseconds."""
        return (
            (self.t2_dev_us * NS_PER_US - self.t1_host_ns)
            + (self.t3_dev_us * NS_PER_US - self.t4_host_ns)
        ) / 2.0 * -1.0

    @property
    def rtt_ns(self) -> float:
        return (self.t4_host_ns - self.t1_host_ns) - (self.t3_dev_us - self.t2_dev_us) * NS_PER_US

    @property
    def dev_mid_us(self) -> float:
        return (self.t2_dev_us + self.t3_dev_us) / 2.0

    @property
    def host_mid_ns(self) -> float:
        return (self.t1_host_ns + self.t4_host_ns) / 2.0


@dataclass(slots=True)
class TimebaseFit:
    """``utc_ns = slope * dev_us + intercept``."""

    slope: float = NS_PER_US       # ns per device microsecond; 1000.0 == perfect
    intercept: float = 0.0
    n_points: int = 0
    rtt_median_ns: float = 0.0
    residual_rms_ns: float = 0.0
    fitted_at: float = 0.0

    @property
    def drift_ppm(self) -> float:
        """Device clock error in parts per million. Positive = device runs slow."""
        return (self.slope / NS_PER_US - 1.0) * 1e6

    @property
    def is_valid(self) -> bool:
        return self.n_points >= MIN_FOR_FIT and self.slope > 0

    @property
    def uncertainty_ns(self) -> float:
        """Honest error bar: dominated by half the round trip, plus fit scatter."""
        return math.hypot(self.rtt_median_ns / 2.0, self.residual_rms_ns)

    def to_utc_ns(self, dev_us):
        return self.slope * dev_us + self.intercept

    def to_json(self) -> dict:
        return {
            "slope_ns_per_us": self.slope,
            "intercept_ns": self.intercept,
            "drift_ppm": round(self.drift_ppm, 3),
            "n_points": self.n_points,
            "rtt_median_us": round(self.rtt_median_ns / 1000.0, 1),
            "residual_rms_us": round(self.residual_rms_ns / 1000.0, 1),
            "uncertainty_us": round(self.uncertainty_ns / 1000.0, 1),
        }


class Timebase:
    """Accumulates sync exchanges and maintains the current device->UTC mapping.

    Deliberately *not* numpy: this runs on a handful of points and lives in the
    hot path of every incoming block, so the pure-Python version is both faster
    and easier to reason about.
    """

    def __init__(self, window: int = WINDOW) -> None:
        self._samples: deque[SyncSample] = deque(maxlen=window)
        self.fit = TimebaseFit()
        self.boot_id: str | None = None
        #: Set when the device reboots; the caller must start a new session.
        self.reboot_detected = False

    # --------------------------------------------------------------- ingestion

    def add(self, sample: SyncSample) -> bool:
        """Record one exchange. Returns True if it was accepted."""
        rtt = sample.rtt_ns
        if rtt < 0 or not math.isfinite(rtt):
            return False  # clock went backwards, or a garbage echo
        if len(self._samples) >= MIN_FOR_FIT:
            median = statistics.median(s.rtt_ns for s in self._samples)
            if median > 0 and rtt > median * RTT_OUTLIER_FACTOR:
                return False  # a scheduling hiccup, not a measurement
        self._samples.append(sample)
        self._refit()
        return True

    def note_boot_id(self, boot_id: str | None) -> None:
        """Watch for a reboot, which invalidates every mapping we have."""
        if boot_id is None:
            return
        if self.boot_id is not None and boot_id != self.boot_id:
            self.reboot_detected = True
            self._samples.clear()
            self.fit = TimebaseFit()
        self.boot_id = boot_id

    def clear_reboot_flag(self) -> None:
        self.reboot_detected = False

    # ----------------------------------------------------------------- fitting

    def _refit(self) -> None:
        samples = list(self._samples)
        n = len(samples)
        if n == 0:
            return

        rtts = sorted(s.rtt_ns for s in samples)
        rtt_median = rtts[n // 2]

        if n == 1:
            s = samples[0]
            self.fit = TimebaseFit(
                slope=NS_PER_US,
                intercept=s.host_mid_ns - NS_PER_US * s.dev_mid_us,
                n_points=1,
                rtt_median_ns=rtt_median,
                fitted_at=time.monotonic(),
            )
            return

        # Weight by 1/rtt^2: an exchange with a 200 us round trip tells us far more
        # than one that spent 8 ms queued behind a WiFi retransmit.
        floor = max(rtt_median * 0.1, 1000.0)
        points = [
            (s.dev_mid_us, s.host_mid_ns, 1.0 / max(s.rtt_ns, floor) ** 2) for s in samples
        ]

        sw = sum(w for _, _, w in points)
        sx = sum(x * w for x, _, w in points)
        sy = sum(y * w for _, y, w in points)
        mean_x = sx / sw
        mean_y = sy / sw
        sxx = sum(w * (x - mean_x) ** 2 for x, _, w in points)
        sxy = sum(w * (x - mean_x) * (y - mean_y) for x, y, w in points)

        if sxx <= 0 or n < MIN_FOR_DRIFT:
            # Not enough time span to separate drift from offset. Assume a perfect
            # clock and fit the offset only -- an honest slope needs minutes of data,
            # and a slope fitted to seconds of data is worse than no slope at all.
            slope = NS_PER_US
            intercept = mean_y - slope * mean_x
        else:
            slope = sxy / sxx
            # A plausible crystal is within +/-200 ppm. Anything outside that is a
            # bad fit, not a bad crystal, so fall back rather than propagate nonsense.
            if abs(slope / NS_PER_US - 1.0) > 200e-6:
                slope = NS_PER_US
            intercept = mean_y - slope * mean_x

        residuals = [y - (slope * x + intercept) for x, y, _ in points]
        rms = math.sqrt(sum(r * r for r in residuals) / len(residuals))

        self.fit = TimebaseFit(
            slope=slope,
            intercept=intercept,
            n_points=n,
            rtt_median_ns=rtt_median,
            residual_rms_ns=rms,
            fitted_at=time.monotonic(),
        )

    # -------------------------------------------------------------- conversion

    def to_utc_ns(self, dev_us):
        """Device microseconds -> host UTC nanoseconds. Vectorises over numpy arrays."""
        return self.fit.slope * dev_us + self.fit.intercept

    def to_utc_s(self, dev_us: float) -> float:
        return self.to_utc_ns(dev_us) / 1e9

    def from_utc_ns(self, utc_ns: float) -> float:
        return (utc_ns - self.fit.intercept) / self.fit.slope

    @property
    def ready(self) -> bool:
        return self.fit.is_valid

    def bootstrap(self, dev_us: int) -> None:
        """Seed a provisional mapping from a single observation.

        Used the moment the first frame arrives so that samples have *some* UTC
        label before the sync burst completes. Accurate to whatever the link
        latency is, and replaced as soon as a real fit exists.
        """
        if self._samples:
            return
        now_ns = time.time_ns()
        self.fit = TimebaseFit(
            slope=NS_PER_US,
            intercept=now_ns - NS_PER_US * dev_us,
            n_points=0,
            rtt_median_ns=0.0,
            fitted_at=time.monotonic(),
        )

    def quality_text(self) -> str:
        """One line for the status bar."""
        f = self.fit
        if f.n_points == 0:
            return "Time: estimated from arrival (no sync yet)"
        if f.n_points < MIN_FOR_DRIFT:
            return f"Time: ±{f.uncertainty_ns / 1000:.0f} µs, offset only ({f.n_points} syncs)"
        return (
            f"Time: ±{f.uncertainty_ns / 1000:.0f} µs, drift {f.drift_ppm:+.1f} ppm "
            f"({f.n_points} syncs)"
        )


@dataclass(slots=True)
class SyncScheduler:
    """When to send the next TIME_SYNC.

    Fast burst on connect to get a usable fit immediately, then a slow cadence to
    track drift without cluttering the link.
    """

    burst_count: int = 8
    burst_interval_s: float = 0.15
    steady_interval_s: float = 30.0
    _sent: int = field(default=0, init=False)
    _next_at: float = field(default=0.0, init=False)

    def reset(self) -> None:
        self._sent = 0
        self._next_at = 0.0

    def due(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return now >= self._next_at

    def mark_sent(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._sent += 1
        interval = self.burst_interval_s if self._sent < self.burst_count else self.steady_interval_s
        self._next_at = now + interval
