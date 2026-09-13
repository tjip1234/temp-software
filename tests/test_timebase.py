"""The device clock fit: offsets from the connect burst, slopes only from minutes."""

from tjiptemp.core.timebase import MIN_SPAN_FOR_DRIFT_S, SyncSample, Timebase


def _exchange(dev_us: float, ppm: float, rtt_us: float = 800.0, base_ns: int = 1_789_000_000 * 10**9):
    """One TIME_SYNC exchange against a device clock running ``ppm`` slow."""
    host_mid = base_ns + dev_us * 1000.0 * (1.0 + ppm * 1e-6)
    half = rtt_us * 1000.0 / 2.0
    return SyncSample(
        t1_host_ns=int(host_mid - half), t2_dev_us=int(dev_us),
        t3_dev_us=int(dev_us), t4_host_ns=int(host_mid + half),
    )


def test_the_connect_burst_fits_an_offset_but_no_slope():
    tb = Timebase()
    for i in range(8):                       # eight syncs, 50 ms apart
        tb.add(_exchange(1_000_000 + i * 50_000, ppm=0.0, rtt_us=800 + (i % 3) * 300))
    assert tb.ready
    assert not tb.fit.drift_fitted, "0.35 s of data cannot tell a slope from jitter"
    assert tb.fit.slope == 1000.0
    assert tb.fit.to_json()["drift_ppm"] is None
    assert "offset only" in tb.quality_text()


def test_drift_is_fitted_once_the_syncs_span_minutes():
    tb = Timebase()
    for i in range(8):
        tb.add(_exchange(1_000_000 + i * 50_000, ppm=30.0))
    for k in range(1, 5):                    # the steady cadence, every 30 s
        tb.add(_exchange(1_000_000 + k * 30_000_000, ppm=30.0))
    assert (4 * 30.0) >= MIN_SPAN_FOR_DRIFT_S
    assert tb.fit.drift_fitted
    assert abs(tb.fit.drift_ppm - 30.0) < 2.0
    assert "drift +3" in tb.quality_text()
