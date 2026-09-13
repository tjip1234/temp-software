"""End-to-end tests against the simulated board.

These exercise the whole stack below the UI: framing over a transport, the
handshake, time sync, streaming, deduplication across links, and backfill.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from tjiptemp.core.aggregator import SampleAggregator
from tjiptemp.device.device import Device, DeviceManager, DeviceState
from tjiptemp.protocol.channels import Ch
from tjiptemp.protocol.messages import SampleBlock
from tjiptemp.simulator.board import SimulatedBoard
from tjiptemp.simulator.server import LoopbackTransport, SimulatorRuntime


@pytest.fixture
async def runtime():
    board = SimulatedBoard(serial="TJIP-TEST00000001", rate_hz=50.0, seed=7)
    rt = SimulatorRuntime(board)
    await rt.start()
    yield rt
    await rt.stop()


@pytest.fixture
async def device(runtime):
    dev = Device()
    await dev.add_transport(LoopbackTransport(runtime), start_stream=False)
    yield dev
    await dev.close()


# --------------------------------------------------------------------- basics

async def test_handshake_reports_identity_and_channels(device):
    assert device.serial == "TJIP-TEST00000001"
    assert device.state is DeviceState.ONLINE
    assert device.info["chip"] == "esp32s3"
    assert len(device.channel_order) == 15
    assert device.channels[int(Ch.PT1000)].unit == "degC"
    assert device.channels[int(Ch.TYPEK_UV)].unit == "uV"


async def test_config_and_calibration_are_fetched(device):
    assert device.config["rtd"]["wires"] == 4
    assert device.config["tc"]["type"] == "K"
    assert device.calibration.get("pt1000").model == "cvd"


async def test_time_sync_produces_a_usable_fit(device):
    fit = device.timebase.fit
    assert fit.n_points >= 4
    assert fit.is_valid
    # A loopback link has microsecond round trips, so the fit should be tight.
    assert fit.uncertainty_ns < 50e6
    assert "Time:" in device.timebase.quality_text()


async def test_streaming_delivers_plausible_measurements(device):
    await device.start_streaming(20.0)
    await asyncio.sleep(1.2)

    assert device.live.size > 10, "no samples arrived"
    values = device.latest_values()

    # The simulator sits near room temperature with a slow ramp.
    assert 10.0 < values[int(Ch.PT1000)] < 60.0
    assert 10.0 < values[int(Ch.NTC_EXT1)] < 60.0
    assert 3.2 < values[int(Ch.V_BAT)] < 4.3
    assert 4.5 < values[int(Ch.V_CC)] < 5.5
    assert 0.0 < values[int(Ch.AHT20_RH)] < 100.0
    # Raw channels must be physically sane too, since calibration depends on them.
    assert 900.0 < values[int(Ch.PT1000_R)] < 1300.0


async def test_timestamps_are_monotonic_and_evenly_spaced(device):
    """The whole point of fitting the timebase rather than stamping on arrival."""
    await device.start_streaming(50.0)
    await asyncio.sleep(1.5)

    times, _ = device.live.view()
    assert times.size > 20
    deltas = np.diff(times)
    assert np.all(deltas > 0), "timestamps must be strictly increasing"
    # Nominal 20 ms spacing; jitter should be far below one interval.
    assert np.std(deltas) < 0.002, f"spacing jitter {np.std(deltas) * 1e3:.2f} ms is too high"


async def test_sequence_numbers_have_no_holes(device):
    await device.start_streaming(50.0)
    await asyncio.sleep(1.5)
    health = device.aggregator.health()
    assert health["rows"] > 20
    assert health["lost"] == 0
    assert health["completeness"] == 1.0


# ------------------------------------------------------------------- control

async def test_setting_the_rtd_wire_mode(device):
    await device.set_wire_mode(3)
    assert device.config["rtd"]["wires"] == 3
    with pytest.raises(ValueError, match="2, 3 or 4"):
        await device.set_wire_mode(5)


async def test_display_control(device):
    await device.set_display(page="graph", source=int(Ch.TYPEK), backlight=55)
    await asyncio.sleep(0.1)
    config = await device.refresh_config()
    assert config["display"]["page"] == "graph"
    assert config["display"]["backlight"] == 55


async def test_the_host_copy_follows_a_display_change(device, runtime):
    """Without it, the next sync from the host's copy put old values back."""
    await device.set_display(rotation=180, backlight=30)
    assert device.config["display"]["rotation"] == 180   # at once, before any answer
    await asyncio.sleep(0.1)
    assert device.config["display"] == runtime.board.config["display"]


async def test_an_unsolicited_config_is_adopted(device):
    """DISPLAY_SET is fire-and-forget, so the board's CONFIG answer to it
    arrives unsolicited. It used to be dropped."""
    from tjiptemp.protocol import messages as M
    from tjiptemp.protocol.framing import Frame

    events = []
    device.subscribe(lambda event: events.append(event.kind))
    config = {**device.config, "display": {**device.config["display"], "page": "blank"}}
    device._on_unsolicited(Frame(M.Msg.CONFIG, M.json_payload(config)), None)
    assert device.config["display"]["page"] == "blank"
    assert "config" in events


async def test_self_test_reports_per_check_results(device):
    result = await device.run_self_test()
    assert "checks" in result
    names = {check["name"] for check in result["checks"]}
    assert {"max31865_spi", "max31856_spi", "aht20_i2c", "nvs"} <= names


async def test_writing_calibration_bumps_the_revision(device):
    from tjiptemp.calibration.models import ChannelCal

    cal = device.calibration
    before = cal.rev
    cal.set("pt1000", ChannelCal("cvd", {"r0": 1000.42, "a": 3.9083e-3,
                                         "b": -5.775e-7, "c": -4.183e-12}))
    written = await device.write_calibration(cal)
    assert written.rev == before + 1
    assert written.get("pt1000").params["r0"] == pytest.approx(1000.42)


async def test_implausible_calibration_is_refused_before_it_reaches_nvs(device):
    from tjiptemp.calibration.models import ChannelCal

    cal = device.calibration
    cal.set("pt1000", ChannelCal("cvd", {"r0": 3.0}))
    with pytest.raises(ValueError, match="outside the plausible range"):
        await device.write_calibration(cal)


async def test_sensor_faults_surface_as_nan_and_are_decoded(runtime):
    runtime.board.inject_fault("max31856", reg=0x01)
    device = Device()
    await device.add_transport(LoopbackTransport(runtime))
    try:
        await device.start_streaming(20.0)
        await asyncio.sleep(1.2)

        values = device.latest_values()
        assert np.isnan(values[int(Ch.TYPEK)]), "a faulted channel must read NaN"
        assert not np.isnan(values[int(Ch.PT1000)]), "other channels keep working"

        faults = device.faults()
        assert "max31856" in faults
        assert any(f["name"] == "open" for f in faults["max31856"])
    finally:
        await device.close()


# ------------------------------------------------------------------ backfill

async def test_get_range_returns_historical_rows(device):
    await device.start_streaming(50.0)
    await asyncio.sleep(1.0)

    link = device.primary_link
    first, last = device.aggregator.next_seq - 20, device.aggregator.next_seq - 10
    blocks, summary = await link.get_range(first, last)

    recovered = sum(b.n_samples for b in blocks)
    assert recovered == last - first + 1
    assert summary["sent"] == recovered
    assert summary["missing"] == []


async def test_range_request_beyond_the_ring_reports_what_is_missing(device):
    await device.start_streaming(20.0)
    await asyncio.sleep(0.5)
    link = device.primary_link
    blocks, summary = await link.get_range(10_000_000, 10_000_010)
    assert blocks == []
    assert summary["sent"] == 0


# --------------------------------------------------------------- aggregation

def _block(first_seq: int, n: int, value: float = 1.0) -> SampleBlock:
    return SampleBlock(
        first_seq=first_seq,
        t0_us=first_seq * 10_000,
        dt_us=10_000,
        channel_ids=(0, 1),
        data=np.full((n, 2), value, dtype=np.float32),
    )


def test_aggregator_deduplicates_identical_blocks_from_two_links():
    """The redundancy story: the same rows over USB and WiFi must count once."""
    agg = SampleAggregator()
    assert sum(b.n_samples for b in agg.feed(_block(0, 10), link="usb")) == 10
    assert agg.feed(_block(0, 10), link="wifi") == []
    assert agg.stats.rows_accepted == 10
    assert agg.stats.rows_duplicate == 10


def test_aggregator_merges_partial_overlap():
    agg = SampleAggregator()
    agg.feed(_block(0, 10), link="usb")
    delivered = agg.feed(_block(5, 10), link="wifi")   # rows 5..14, half already seen
    assert sum(b.n_samples for b in delivered) == 5
    assert agg.next_seq == 15


def test_aggregator_holds_out_of_order_blocks_until_the_hole_fills():
    agg = SampleAggregator()
    agg.feed(_block(0, 10))
    assert agg.feed(_block(20, 10)) == [], "must not deliver past a hole"
    assert agg.held_rows == 10
    assert agg.missing_rows == 10

    delivered = agg.feed(_block(10, 10))   # the missing middle arrives
    assert sum(b.n_samples for b in delivered) == 20, "hole plus the held block"
    assert agg.next_seq == 30
    assert agg.missing_rows == 0
    assert agg.open_gaps == []


def test_aggregator_offers_gaps_for_backfill_after_a_grace_period():
    import time as time_mod

    agg = SampleAggregator()
    agg.feed(_block(0, 10))
    agg.feed(_block(50, 10))

    assert agg.due_gaps(now=time_mod.monotonic()) == [], "must wait out the grace period"
    due = agg.due_gaps(now=time_mod.monotonic() + 2.0)
    assert len(due) == 1
    assert (due[0].first_seq, due[0].last_seq) == (10, 49)


def test_a_gap_stays_due_while_the_stream_carries_on_past_it():
    """One lost block must not freeze the live view.

    A hole is followed by a steady stream of blocks. Each one used to re-note the
    gap from next_seq, which replaced it with a fresh one: its grace period
    restarted every block, so it was never requested, and every later block was
    held behind it. On a real board that froze the display after a single frame
    lost at connect.
    """
    import time as time_mod

    agg = SampleAggregator()
    agg.feed(_block(0, 10))
    noticed = time_mod.monotonic()
    agg.feed(_block(20, 10))                 # rows 10..19 lost
    for first in range(30, 230, 10):         # the stream carries on
        assert agg.feed(_block(first, 10)) == []

    assert agg.missing_rows == 10, "only the hole is missing, not the rows held behind it"
    due = agg.due_gaps(now=noticed + 2.0)
    assert [(g.first_seq, g.last_seq) for g in due] == [(10, 19)]

    delivered = agg.feed(_block(10, 10), backfill=True)
    assert sum(b.n_samples for b in delivered) == 220
    assert agg.held_rows == 0 and agg.open_gaps == []


def test_a_second_hole_behind_held_rows_is_its_own_gap():
    agg = SampleAggregator()
    agg.feed(_block(0, 10))
    agg.feed(_block(20, 10))    # hole 10..19
    agg.feed(_block(40, 10))    # hole 30..39
    assert [(g.first_seq, g.last_seq) for g in agg.open_gaps] == [(10, 19), (30, 39)]
    assert agg.missing_rows == 20


def test_aggregator_gives_up_on_a_gap_after_repeated_failures():
    import time as time_mod

    agg = SampleAggregator()
    agg.feed(_block(0, 10))
    agg.feed(_block(50, 10))
    gap = agg.due_gaps(now=time_mod.monotonic() + 2.0)[0]
    for _ in range(4):
        agg.mark_requested(gap)
    assert agg.stats.rows_lost == 40
    assert agg.due_gaps(now=time_mod.monotonic() + 1000.0) == []


def test_aggregator_reports_evicted_rows_as_lost():
    agg = SampleAggregator()
    agg.feed(_block(0, 10))
    agg.feed(_block(50, 10))
    agg.mark_unrecoverable(10, 49)
    assert agg.stats.rows_lost == 40
    assert agg.stats.completeness < 1.0


# ------------------------------------------------------------------- manager

async def test_manager_keys_devices_by_serial_not_by_port(runtime):
    """Reconnecting on a different transport must not create a second device."""
    manager = DeviceManager()
    try:
        first = await manager.connect(LoopbackTransport(runtime), start_stream=False)
        second = await manager.connect(LoopbackTransport(runtime), start_stream=False)
        assert first is second, "same board reached twice is still one device"
        assert len(manager.devices) == 1
        assert len(first.links) == 2
        assert set(first.link_kinds) == {"sim"}
    finally:
        await manager.close()


async def test_two_links_to_one_board_do_not_double_count_rows(runtime):
    manager = DeviceManager()
    try:
        device = await manager.connect(LoopbackTransport(runtime), start_stream=False)
        await manager.connect(LoopbackTransport(runtime), start_stream=False)
        await device.start_streaming(20.0)
        await asyncio.sleep(1.5)

        health = device.aggregator.health()
        assert health["rows"] > 10
        assert health["duplicates"] > 0, "both links should have delivered the same rows"
        assert health["completeness"] == 1.0
        # Every stored row is unique: no sequence appears twice in the live buffer.
        times, _ = device.live.view()
        assert np.all(np.diff(times) > 0)
    finally:
        await manager.close()


async def test_reboot_resets_the_stream(runtime):
    device = Device()
    await device.add_transport(LoopbackTransport(runtime))
    try:
        await device.start_streaming(20.0)
        await asyncio.sleep(0.8)
        assert device.live.size > 0

        runtime.board.reboot()
        await asyncio.sleep(1.5)

        # The board restarts its sequence numbers; the host must notice rather than
        # treating row 0 as a gigantic backwards gap.
        assert device.aggregator.stats.rows_lost == 0
    finally:
        await device.close()


# ------------------------------------------------- connecting must not reset

async def test_a_slow_handshake_does_not_reopen_the_port(runtime, monkeypatch):
    """Retrying HELLO must not reopen the transport.

    Opening a USB CDC port moves DTR and RTS, and on an ESP32-S3 that sequence
    is what the USB-Serial-JTAG unit resets the chip on. Reopening once per
    retry therefore reset the board once per retry: a board that was merely slow
    to answer was reset before it could, and the connect attempt fed itself for
    the whole window. The retry belongs to the handshake, not to the port.
    """
    from tjiptemp.device import device as D
    from tjiptemp.device.link import Link

    transport = LoopbackTransport(runtime)
    opens = 0
    real_open = transport.open

    async def counting_open() -> None:
        nonlocal opens
        opens += 1
        await real_open()

    transport.open = counting_open

    calls = 0
    real_hello = Link.hello

    async def slow_hello(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise TimeoutError("board still booting")
        return await real_hello(self, *args, **kwargs)

    monkeypatch.setattr(Link, "hello", slow_hello)
    monkeypatch.setattr(D, "HANDSHAKE_RETRY_S", 0.01)

    dev = Device()
    try:
        await dev.add_transport(transport, start_stream=False)
        assert dev.state is DeviceState.ONLINE
        assert calls == 3, "the handshake should have been retried twice"
        assert opens == 1, "the port was reopened between handshake attempts"
    finally:
        await dev.close()


async def test_a_hello_lost_to_a_boot_is_sent_again_on_the_open_port(runtime, monkeypatch):
    """A board still booting when the port opens never hears the first HELLO.

    That HELLO used to wait the link's 12 s default -- the whole handshake
    window -- so one lost HELLO meant a 12 s hang and then a failure, and the
    retry never got a turn. It is sent again instead, on the port already open.
    """
    import time

    from tjiptemp.device import device as D

    monkeypatch.setattr(D, "HELLO_TIMEOUT_S", 0.2)
    real = runtime.handle_bytes
    booted_at = time.monotonic() + 0.5

    def booting(session_id, data, reader):
        if time.monotonic() >= booted_at:
            real(session_id, data, reader)

    runtime.handle_bytes = booting
    transport = LoopbackTransport(runtime)
    opens = 0
    real_open = transport.open

    async def counting_open() -> None:
        nonlocal opens
        opens += 1
        await real_open()

    transport.open = counting_open

    dev = Device()
    started = time.monotonic()
    try:
        await dev.add_transport(transport, start_stream=False)
        assert dev.state is DeviceState.ONLINE
        assert time.monotonic() - started < 2.0
        assert opens == 1, "the port was reopened to send HELLO again"
    finally:
        await dev.close()


async def test_a_late_answer_to_an_earlier_hello_still_counts(runtime, monkeypatch):
    """Sending HELLO again must not abandon the ones already out.

    A busy board answers late. If each resend threw away the HELLO before it,
    a board slower than the resend interval would never connect at all.
    """
    import time

    from tjiptemp.device import device as D
    from tjiptemp.device.link import Link

    monkeypatch.setattr(D, "HELLO_TIMEOUT_S", 0.1)
    calls = 0
    real_hello = Link.hello

    async def slow_hello(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.35)
        return await real_hello(self, *args, **kwargs)

    monkeypatch.setattr(Link, "hello", slow_hello)
    dev = Device()
    started = time.monotonic()
    try:
        await dev.add_transport(LoopbackTransport(runtime), start_stream=False)
        assert dev.state is DeviceState.ONLINE
        assert time.monotonic() - started < 2.0
        assert 2 <= calls <= 6, "HELLO should have been re-sent while the first was out"
    finally:
        await dev.close()


async def test_find_usb_boards_does_not_wait_for_the_network(monkeypatch, tmp_path):
    """It used to run the mDNS browse as well: three seconds of waiting for
    results it then threw away."""
    from tjiptemp.core import application as A
    from tjiptemp.transport import discovery

    browsed = []

    async def browse(*_args, **_kwargs):
        browsed.append(True)
        return []

    monkeypatch.setattr(discovery, "discover_mdns", browse)
    monkeypatch.setattr(A, "discover_serial", lambda **_kwargs: [])
    app = A.Application(A.Settings(), db_path=tmp_path / "recordings.tjip")
    try:
        assert await app.auto_connect() == []
        assert not browsed
    finally:
        await app.close()


async def test_a_board_that_never_sends_its_simulator_table_connects_anyway(runtime, monkeypatch):
    """The DIN-6 dropped its SIM_CAL reply (too big for a frame), and waiting
    for it held every connect for the whole 15 s timeout. It is fetched after
    going online now, not before."""
    import time

    from tjiptemp.device.link import Link

    async def never(self, *args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(Device, "has_simulator", property(lambda self: True))
    monkeypatch.setattr(Link, "get_sim_cal", never)
    dev = Device()
    started = time.monotonic()
    try:
        await dev.add_transport(LoopbackTransport(runtime), start_stream=False)
        assert dev.state is DeviceState.ONLINE
        assert time.monotonic() - started < 3.0
    finally:
        await dev.close()


async def test_a_second_connect_to_the_same_port_joins_the_first(runtime, monkeypatch, tmp_path):
    """Clicking again during a slow connect opened the port a second time, and
    that failed as "already open in another program" -- the other program being
    this one, which was connecting fine."""
    from tjiptemp.core import application as A
    from tjiptemp.transport.discovery import Candidate

    built = []

    def build(candidate):
        transport = LoopbackTransport(runtime)
        transport.info.kind, transport.info.address = candidate.kind, candidate.address
        built.append(transport)
        return transport

    monkeypatch.setattr(A, "build_transport", build)
    app = A.Application(A.Settings(), db_path=tmp_path / "recordings.tjip")
    port = Candidate("usb", "/dev/ttyACM9", "test port")
    try:
        first, second = await asyncio.gather(app.connect(port), app.connect(port))
        assert first is second
        assert len(built) == 1
        assert await app.connect(port) is first     # already connected: no new port
        assert len(built) == 1
    finally:
        await app.close()


# --------------------------------------------- the board's settings win

async def test_connecting_adopts_the_boards_own_sample_rate(runtime):
    """The board's stored rate wins over whatever the desktop remembered.

    A board applies its NVS configuration the moment it powers up, host or no
    host. If connecting pushed the desktop's remembered value over the top, the
    board would be reconfigured by a program that just walked in.
    """
    board = runtime.board
    board.config["acquire"]["rate_hz"] = 25

    manager = DeviceManager()
    manager.default_rate_hz = 4.0          # what the desktop remembered
    try:
        dev = await manager.connect(LoopbackTransport(runtime), start_stream=False)
        assert dev.stream_rate_hz == 25.0
    finally:
        await manager.close()


async def test_a_board_without_a_rate_keeps_the_desktop_default(runtime):
    board = runtime.board
    board.config["acquire"].pop("rate_hz", None)

    manager = DeviceManager()
    manager.default_rate_hz = 7.0
    try:
        dev = await manager.connect(LoopbackTransport(runtime), start_stream=False)
        assert dev.stream_rate_hz == 7.0
    finally:
        await manager.close()


async def test_connecting_reads_the_simulator_table_the_board_already_has(
    runtime, monkeypatch
):
    """A stored sweep belongs to the board, so connecting has to ask for it.

    SIM_CAL is only pushed unsolicited when a sweep finishes. A board calibrated
    last week therefore showed the panel an empty table and an unknown state
    until someone ran another sweep -- the board knew, and nothing asked.
    """
    from tjiptemp.device.link import Link

    monkeypatch.setattr(Device, "has_simulator", property(lambda self: True))
    asked = 0

    async def fake_get_sim_cal(self):
        nonlocal asked
        asked += 1
        return {"generation": 3, "last_sweep": {"ok": True, "verify": []}}

    monkeypatch.setattr(Link, "get_sim_cal", fake_get_sim_cal)

    dev = Device()
    try:
        await dev.add_transport(LoopbackTransport(runtime), start_stream=False)
        # Asked right after going online rather than before, so a board that
        # never answers cannot hold the connect for the whole timeout.
        await asyncio.sleep(0.05)
        assert asked == 1
        assert dev.sim_cal["generation"] == 3
    finally:
        await dev.close()


async def test_a_board_without_a_simulator_is_not_asked_for_a_table(runtime, monkeypatch):
    from tjiptemp.device.link import Link

    monkeypatch.setattr(Device, "has_simulator", property(lambda self: False))
    asked = 0

    async def fake_get_sim_cal(self):
        nonlocal asked
        asked += 1
        return {}

    monkeypatch.setattr(Link, "get_sim_cal", fake_get_sim_cal)

    dev = Device()
    try:
        await dev.add_transport(LoopbackTransport(runtime), start_stream=False)
        assert asked == 0
    finally:
        await dev.close()


# ---------------------------------------------------------------- reindexing

def test_reindex_maps_columns_and_nans_the_missing_ones():
    """A board that reports a subset, or a different order, must still land in
    the canonical columns — with absent channels NaN rather than zero."""
    import numpy as np

    from tjiptemp.device.device import Device
    from tjiptemp.protocol.messages import SampleBlock

    device = Device(serial="TJIP-TEST")
    device.channel_order = (1, 2, 3, 4)

    # Out of order, and channel 3 missing entirely.
    block = SampleBlock(
        first_seq=0, t0_us=0, dt_us=1000, channel_ids=(4, 1, 2),
        data=np.array([[40.0, 10.0, 20.0], [41.0, 11.0, 21.0]], dtype=np.float32),
    )
    out = device._reindex(block)
    assert out.shape == (2, 4)
    assert np.allclose(out[:, 0], [10.0, 11.0])       # channel 1
    assert np.allclose(out[:, 1], [20.0, 21.0])       # channel 2
    assert np.all(np.isnan(out[:, 2]))                # channel 3 absent
    assert np.allclose(out[:, 3], [40.0, 41.0])       # channel 4

    # The plan is cached, and the cached path must give the same answer.
    assert np.allclose(device._reindex(block), out, equal_nan=True)

    # An exactly-matching block is passed through untouched, no copy.
    same = SampleBlock(
        first_seq=0, t0_us=0, dt_us=1000, channel_ids=(1, 2, 3, 4),
        data=np.zeros((2, 4), dtype=np.float32),
    )
    assert device._reindex(same) is same.data


def test_reindex_plan_is_rebuilt_when_the_channel_order_changes():
    """The cache is keyed on the source ids, so a new canonical order has to
    invalidate it or every subsequent block lands in the wrong columns."""
    import numpy as np

    from tjiptemp.device.device import Device
    from tjiptemp.protocol.messages import SampleBlock

    device = Device(serial="TJIP-TEST")
    device.channel_order = (1, 2)
    block = SampleBlock(
        first_seq=0, t0_us=0, dt_us=1000, channel_ids=(2, 1),
        data=np.array([[20.0, 10.0]], dtype=np.float32),
    )
    assert np.allclose(device._reindex(block), [[10.0, 20.0]])

    device.channel_order = (2, 1)
    device._reindex_cache.clear()          # what _apply_info does on a new set
    assert np.allclose(device._reindex(block), [[20.0, 10.0]])
