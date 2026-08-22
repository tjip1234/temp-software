"""In-memory rolling history for the live view.

The database owns the permanent record; this owns the last few minutes, in a shape
pyqtgraph can hand straight to the GPU. Two constraints drive the design:

* Appending must not allocate. At 100 Hz across several boards, a growing list
  would churn the allocator continuously and show up as jitter in the UI.
* Plotting must not copy the whole buffer every frame. So the buffer stores
  contiguously and hands out views, at the cost of one memmove when it wraps.

The implementation is a double-length array with a write cursor: values are written
twice, at ``i`` and ``i + capacity``, which makes any window of up to ``capacity``
samples a contiguous slice with no copy at all. It costs 2x memory -- 100 Hz x 15
channels x 10 minutes is about 7 MB doubled, which is a fine trade for never
copying on the render path.
"""

from __future__ import annotations

import numpy as np


class ChannelRing:
    """Fixed-capacity rolling buffer of timestamped values for a set of channels."""

    def __init__(self, n_channels: int, capacity: int = 120_000) -> None:
        self.capacity = int(capacity)
        self.n_channels = int(n_channels)
        # Doubled storage so any recent window is contiguous.
        self._t = np.zeros(self.capacity * 2, dtype=np.float64)      # UTC seconds
        self._v = np.full((self.capacity * 2, self.n_channels), np.nan, dtype=np.float32)
        self._seq = np.zeros(self.capacity * 2, dtype=np.int64)
        self._cursor = 0     # next write index in [0, capacity)
        self._count = 0      # total rows ever written

    # ------------------------------------------------------------------ write

    def append(self, times_s: np.ndarray, values: np.ndarray, seqs: np.ndarray | None = None) -> None:
        """Append rows. ``values`` must be ``(n, n_channels)``."""
        n = len(times_s)
        if n == 0:
            return
        if values.shape[0] != n or values.shape[1] != self.n_channels:
            raise ValueError(
                f"expected ({n}, {self.n_channels}) values, got {values.shape}"
            )
        if n >= self.capacity:
            # More than the whole buffer in one go: keep only the tail.
            times_s = times_s[-self.capacity :]
            values = values[-self.capacity :]
            if seqs is not None:
                seqs = seqs[-self.capacity :]
            n = self.capacity

        i = self._cursor
        end = i + n
        if end <= self.capacity:
            self._write(i, times_s, values, seqs)
        else:
            split = self.capacity - i
            self._write(i, times_s[:split], values[:split], None if seqs is None else seqs[:split])
            self._write(0, times_s[split:], values[split:], None if seqs is None else seqs[split:])
        self._cursor = end % self.capacity
        self._count += n

    def _write(self, at: int, times_s, values, seqs) -> None:
        n = len(times_s)
        self._t[at : at + n] = times_s
        self._v[at : at + n] = values
        self._t[at + self.capacity : at + self.capacity + n] = times_s
        self._v[at + self.capacity : at + self.capacity + n] = values
        if seqs is not None:
            self._seq[at : at + n] = seqs
            self._seq[at + self.capacity : at + self.capacity + n] = seqs

    # ------------------------------------------------------------------- read

    @property
    def size(self) -> int:
        """Rows currently held."""
        return min(self._count, self.capacity)

    @property
    def total_written(self) -> int:
        return self._count

    def view(self, last_n: int | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Contiguous ``(times, values)`` views of the most recent rows. No copy."""
        size = self.size
        if size == 0:
            return self._t[:0], self._v[:0]
        n = size if last_n is None else min(last_n, size)
        start = (self._cursor - n) % self.capacity
        if start + n <= self.capacity * 2:
            return self._t[start : start + n], self._v[start : start + n]
        return self._t[start:], self._v[start:]  # pragma: no cover - unreachable

    def channel_view(self, index: int, last_n: int | None = None) -> tuple[np.ndarray, np.ndarray]:
        t, v = self.view(last_n)
        return t, v[:, index]

    def window(self, seconds: float, now_s: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Rows within the last ``seconds``, by timestamp rather than by count."""
        t, v = self.view()
        if t.size == 0:
            return t, v
        cutoff = (t[-1] if now_s is None else now_s) - seconds
        idx = int(np.searchsorted(t, cutoff, side="left"))
        return t[idx:], v[idx:]

    def latest(self) -> tuple[float, np.ndarray] | None:
        """Most recent row, or None if empty."""
        if self.size == 0:
            return None
        i = (self._cursor - 1) % self.capacity
        return float(self._t[i]), self._v[i]

    def latest_valid(self, index: int, max_age_rows: int = 200) -> float:
        """Most recent non-NaN value for a channel, searching back a bounded distance.

        A faulted sensor emits NaN; showing the last good reading with an age is far
        more useful to a user than showing a dash, but only up to a point -- hence
        the bound.
        """
        t, v = self.view(max_age_rows)
        if v.size == 0:
            return float("nan")
        col = v[:, index]
        good = np.flatnonzero(np.isfinite(col))
        return float(col[good[-1]]) if good.size else float("nan")

    def time_span(self) -> tuple[float, float]:
        t, _ = self.view()
        if t.size == 0:
            return (0.0, 0.0)
        return (float(t[0]), float(t[-1]))

    def clear(self) -> None:
        self._cursor = 0
        self._count = 0
        self._v[:] = np.nan

    def resize(self, capacity: int) -> None:
        """Change capacity, keeping as much recent data as fits."""
        capacity = int(capacity)
        if capacity == self.capacity:
            return
        t, v = self.view()
        keep = min(len(t), capacity)
        self.capacity = capacity
        self._t = np.zeros(capacity * 2, dtype=np.float64)
        self._v = np.full((capacity * 2, self.n_channels), np.nan, dtype=np.float32)
        self._seq = np.zeros(capacity * 2, dtype=np.int64)
        self._cursor = 0
        self._count = 0
        if keep:
            self.append(t[-keep:].copy(), v[-keep:].copy())


class DecimatingView:
    """Min/max decimation for plotting long spans without lying about the data.

    Drawing a million points into 1200 pixels means ~800 samples per pixel. Naive
    stride-sampling would drop every spike, so a transient could vanish entirely
    just because you zoomed out. Min/max decimation keeps two points per pixel
    column -- the extremes -- so the envelope stays truthful and spikes remain
    visible at any zoom level.
    """

    @staticmethod
    def decimate(
        t: np.ndarray, v: np.ndarray, target_points: int = 2000
    ) -> tuple[np.ndarray, np.ndarray]:
        n = len(t)
        if n <= target_points or target_points < 4:
            return t, v
        buckets = max(2, target_points // 2)
        per = n // buckets
        if per < 2:
            return t, v
        usable = per * buckets
        tb = t[:usable].reshape(buckets, per)
        vb = v[:usable].reshape(buckets, per)

        with np.errstate(invalid="ignore"):
            all_nan = np.all(np.isnan(vb), axis=1)
            safe = np.where(np.isnan(vb), np.inf, vb)
            lo_idx = np.argmin(safe, axis=1)
            safe = np.where(np.isnan(vb), -np.inf, vb)
            hi_idx = np.argmax(safe, axis=1)

        rows = np.arange(buckets)
        lo_t, lo_v = tb[rows, lo_idx], vb[rows, lo_idx]
        hi_t, hi_v = tb[rows, hi_idx], vb[rows, hi_idx]

        # Emit each bucket's pair in time order so the line never doubles back.
        first_is_lo = lo_idx <= hi_idx
        t_out = np.empty(buckets * 2, dtype=t.dtype)
        v_out = np.empty(buckets * 2, dtype=v.dtype)
        t_out[0::2] = np.where(first_is_lo, lo_t, hi_t)
        t_out[1::2] = np.where(first_is_lo, hi_t, lo_t)
        v_out[0::2] = np.where(first_is_lo, lo_v, hi_v)
        v_out[1::2] = np.where(first_is_lo, hi_v, lo_v)

        # A bucket that was entirely NaN must stay NaN, so the gap shows as a gap.
        blank = np.repeat(all_nan, 2)
        v_out[blank] = np.nan

        tail_t, tail_v = t[usable:], v[usable:]
        if tail_t.size:
            t_out = np.concatenate([t_out, tail_t])
            v_out = np.concatenate([v_out, tail_v])
        return t_out, v_out
