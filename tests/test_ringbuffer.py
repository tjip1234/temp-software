"""The live buffer's storage layout and what the renderer gets out of it.

The buffer exists so the render loop never copies. That is a property of the
memory layout, not of the API, so it is worth asserting directly — a change that
quietly reintroduced a per-frame copy would otherwise pass every other test.
"""

from __future__ import annotations

import numpy as np
import pytest

from tjiptemp.core.ringbuffer import ChannelRing, DecimatingView


def _fill(ring: ChannelRing, n: int, start: float = 0.0) -> None:
    t = start + np.arange(n, dtype=np.float64)
    v = np.stack([t + c * 1000.0 for c in range(ring.n_channels)], axis=1).astype(np.float32)
    ring.append(t, v)


def test_a_channel_is_a_contiguous_view_not_a_copy():
    ring = ChannelRing(6, capacity=1000)
    _fill(ring, 500)
    _t, v = ring.view()
    for channel in range(6):
        column = v[:, channel]
        assert column.flags["C_CONTIGUOUS"], "the render path would have to copy"
        # A view, not a copy: it must alias the buffer's own memory.
        assert column.base is not None


def test_public_shape_is_still_rows_by_channels():
    ring = ChannelRing(4, capacity=100)
    _fill(ring, 30)
    t, v = ring.view()
    assert t.shape == (30,)
    assert v.shape == (30, 4)
    assert v[0, 0] == pytest.approx(0.0)
    assert v[0, 3] == pytest.approx(3000.0)
    assert v[29, 1] == pytest.approx(1029.0)


def test_values_survive_wrapping():
    """The doubled-store trick has to hold at the wrap, in every channel."""
    ring = ChannelRing(3, capacity=64)
    _fill(ring, 100)                      # 36 rows past the wrap
    t, v = ring.view()
    assert t.size == 64
    assert t[0] == pytest.approx(36.0) and t[-1] == pytest.approx(99.0)
    for channel in range(3):
        assert np.allclose(v[:, channel], t + channel * 1000.0)
        assert v[:, channel].flags["C_CONTIGUOUS"]


def test_latest_row_covers_every_channel():
    ring = ChannelRing(5, capacity=32)
    _fill(ring, 40)
    latest = ring.latest()
    assert latest is not None
    when, row = latest
    assert when == pytest.approx(39.0)
    assert [float(x) for x in row] == pytest.approx([39.0 + c * 1000.0 for c in range(5)])


def test_resize_keeps_the_most_recent_rows():
    ring = ChannelRing(2, capacity=100)
    _fill(ring, 100)
    ring.resize(20)
    t, v = ring.view()
    assert t.size == 20
    assert t[0] == pytest.approx(80.0) and t[-1] == pytest.approx(99.0)
    assert np.allclose(v[:, 1], t + 1000.0)


def test_decimation_keeps_a_lone_spike():
    """The whole point of min/max over stride-sampling: transients must survive."""
    n = 100_000
    t = np.arange(n, dtype=np.float64)
    v = np.zeros(n, dtype=np.float32)
    v[54_321] = 42.0                       # one sample, nowhere near a stride boundary
    td, vd = DecimatingView.decimate(t, v, 2000)
    assert vd.max() == pytest.approx(42.0)
    assert td[int(np.argmax(vd))] == pytest.approx(54_321.0)


def test_decimation_keeps_gaps_as_gaps():
    n = 20_000
    t = np.arange(n, dtype=np.float64)
    v = np.zeros(n, dtype=np.float32)
    v[5_000:9_000] = np.nan                # a fault, or rows that never arrived
    td, vd = DecimatingView.decimate(t, v, 1000)
    assert np.isnan(vd).any(), "a gap must not be interpolated over"
    gap_times = td[np.isnan(vd)]
    assert gap_times.min() >= 5_000 and gap_times.max() <= 9_000
