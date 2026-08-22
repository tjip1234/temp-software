"""Sensor conversions and calibration fitting.

These tests are the reason to trust a number the software prints. Each conversion
is checked against its own inverse, against published reference values, and
against the deliberate errors the simulator injects.
"""

from __future__ import annotations

import numpy as np
import pytest

from tjiptemp.calibration import fitting
from tjiptemp.sensors import ntc, rtd
from tjiptemp.sensors import thermocouple as tc

# ------------------------------------------------------------------------ RTD

@pytest.mark.parametrize("t", [-200.0, -100.0, -0.5, 0.0, 25.0, 100.0, 420.0, 660.0])
def test_cvd_roundtrip(t):
    r = rtd.resistance(t)
    back = rtd.temperature(r)
    assert abs(back - t) < 1e-6, f"CVD failed to invert at {t} °C"


def test_pt1000_known_values():
    """IEC 60751 reference points, scaled x10 from the PT100 table."""
    assert rtd.resistance(0.0) == pytest.approx(1000.0, abs=1e-9)
    assert rtd.resistance(100.0) == pytest.approx(1385.055, abs=0.005)
    assert rtd.resistance(-100.0) == pytest.approx(602.56, abs=0.02)


def test_cvd_vectorises_and_preserves_nan():
    out = rtd.temperature(np.array([1000.0, np.nan, 1385.055]))
    assert out[0] == pytest.approx(0.0, abs=1e-6)
    assert np.isnan(out[1])
    assert out[2] == pytest.approx(100.0, abs=0.01)


def test_lead_resistance_shifts_the_reading():
    """1 Ω of lead on a PT1000 is roughly a quarter of a degree."""
    clean = rtd.temperature(1097.3)
    with_lead = rtd.temperature(1097.3 + 1.0, lead_ohm=0.0)
    assert with_lead - clean == pytest.approx(0.26, abs=0.02)
    # Declaring the lead removes the error again.
    assert rtd.temperature(1097.3 + 1.0, lead_ohm=1.0) == pytest.approx(clean, abs=1e-9)


def test_max31865_resolution_is_reported_honestly():
    res = rtd.resolution_c(rtd.R0_PT1000, 4000.0, 25.0)
    assert 0.02 < res < 0.05  # ~31 mK per LSB


# --------------------------------------------------------------- thermocouple

def test_typek_forward_reference_points():
    """NIST ITS-90 type-K table values."""
    assert tc.emf_mv(0.0) == pytest.approx(0.0, abs=1e-6)
    assert tc.emf_mv(100.0) == pytest.approx(4.096, abs=0.001)
    assert tc.emf_mv(500.0) == pytest.approx(20.644, abs=0.002)
    assert tc.emf_mv(1000.0) == pytest.approx(41.276, abs=0.005)
    assert tc.emf_mv(-100.0) == pytest.approx(-3.554, abs=0.002)


@pytest.mark.parametrize("t", [-150.0, -20.0, 0.0, 55.0, 250.0, 700.0, 1200.0])
def test_typek_inverse_matches_forward(t):
    """The inverse polynomial's quoted error is under 0.06 °C across its range."""
    assert tc.temperature_from_mv(tc.emf_mv(t)) == pytest.approx(t, abs=0.07)


def test_cold_junction_compensation():
    """A TC at 300 °C with its cold junction at 25 °C must still read 300 °C."""
    uv = tc.emf_uv(300.0) - tc.emf_uv(25.0)
    assert tc.temperature(uv, 25.0) == pytest.approx(300.0, abs=0.1)


def test_cold_junction_error_passes_through_at_unity():
    """Why the CJ sensor matters as much as the probe."""
    uv = tc.emf_uv(300.0) - tc.emf_uv(25.0)
    correct = tc.temperature(uv, 25.0)
    with_error = tc.temperature(uv, 25.5)  # CJ reads half a degree high
    assert with_error - correct == pytest.approx(0.5, abs=0.05)


# ------------------------------------------------------------------------ NTC

@pytest.mark.parametrize("t", [-40.0, 0.0, 25.0, 85.0, 125.0])
def test_steinhart_roundtrip(t):
    r = ntc.resistance_steinhart(t)
    assert ntc.temperature_steinhart(r) == pytest.approx(t, abs=1e-6)


def test_default_ntc_is_a_10k_at_25c():
    assert ntc.resistance_steinhart(25.0) == pytest.approx(10000.0, rel=0.01)


@pytest.mark.parametrize("t", [-20.0, 25.0, 80.0])
def test_beta_roundtrip(t):
    r = ntc.resistance_beta(t)
    assert ntc.temperature_beta(r) == pytest.approx(t, abs=1e-9)


def test_divider_roundtrip():
    r = 8200.0
    v = ntc.divider_voltage(r, 3.3, 10000.0)
    assert ntc.divider_resistance(v, 3.3, 10000.0) == pytest.approx(r, rel=1e-9)


def test_beta_converts_to_equivalent_steinhart():
    a, b, c = ntc.beta_to_steinhart(3435.0, 10000.0, 25.0)
    for t in (0.0, 25.0, 60.0):
        r = ntc.resistance_beta(t, 3435.0, 10000.0, 25.0)
        assert ntc.temperature_steinhart(r, a, b, c) == pytest.approx(t, abs=1e-6)


# -------------------------------------------------------------------- fitting

def test_fit_cvd_recovers_a_shifted_r0():
    true_r0 = 1000.42
    temps = np.array([0.0, 25.0, 60.0, 100.0])
    resistances = rtd.resistance(temps, r0=true_r0)

    result = fitting.fit_cvd(resistances, temps)
    assert result.params["r0"] == pytest.approx(true_r0, abs=0.01)
    assert result.max_abs < 1e-3
    assert result.dof > 0


def test_fit_cvd_single_point_is_the_ice_point_calibration():
    result = fitting.fit_cvd([1000.42], [0.0])
    assert result.params["r0"] == pytest.approx(1000.42, abs=0.01)
    assert result.exactly_determined
    assert "tell you nothing" in result.quality_note()


def test_fit_steinhart_recovers_known_coefficients():
    a, b, c = 1.129241e-3, 2.341077e-4, 8.775468e-8
    temps = np.array([0.0, 25.0, 50.0, 80.0])
    resistances = ntc.resistance_steinhart(temps, a, b, c)

    result = fitting.fit_steinhart(resistances, temps)
    assert result.params["a"] == pytest.approx(a, rel=1e-4)
    assert result.params["b"] == pytest.approx(b, rel=1e-4)
    assert result.max_abs < 1e-3


def test_fit_steinhart_needs_three_points():
    with pytest.raises(fitting.FitError, match="at least 3"):
        fitting.fit_steinhart([10000.0, 4000.0], [25.0, 60.0])


def test_fit_rejects_degenerate_points():
    with pytest.raises(fitting.FitError, match="spread of temperatures"):
        fitting.fit_steinhart([10000.0, 10001.0, 9999.0], [25.0, 25.0, 25.0])


def _typek_points(true_temps, cj_true, cj_offset, uv_offset):
    """Simulate a board with a known CJ error and a known amplifier offset."""
    uv_measured = (
        np.asarray(tc.emf_uv(true_temps)) - np.asarray(tc.emf_uv(cj_true)) + uv_offset
    )
    return uv_measured, cj_true + cj_offset


def test_fit_typek_with_a_known_cold_junction_recovers_the_voltage_offset():
    """Calibrate the cold junction first, then the voltage offset comes out clean."""
    cj_offset, uv_offset = 0.37, 4.1
    true_temps = np.array([25.0, 100.0, 200.0, 400.0, 600.0])
    cj_true = np.full_like(true_temps, 22.0)
    uv_measured, cj_reported = _typek_points(true_temps, cj_true, cj_offset, uv_offset)

    result = fitting.fit_typek(
        uv_measured, cj_reported, true_temps, cj_offset_c=-cj_offset, fit_gain=False
    )

    assert result.params["cj_offset_c"] == pytest.approx(-cj_offset)
    # The recovered offset lands within about a microvolt of truth. It cannot do
    # better: the NIST inverse polynomial is only accurate to ~0.05 °C, so the
    # solver trades a fraction of a microvolt to balance that inversion error
    # across the reference points. One microvolt is 0.024 °C.
    assert result.params["uv_offset"] == pytest.approx(-uv_offset, abs=1.0)
    assert result.max_abs < 0.05


def test_fit_typek_gain_absorbs_part_of_the_offset():
    """Freeing the gain trades against the offset — small in °C, but real in µV.

    Worth pinning down: a user comparing two fits of the same data should not be
    surprised that the reported offset moved by a microvolt when the gain was freed.
    """
    cj_offset, uv_offset = 0.37, 4.1
    true_temps = np.array([25.0, 100.0, 200.0, 400.0, 600.0])
    cj_true = np.full_like(true_temps, 22.0)
    uv_measured, cj_reported = _typek_points(true_temps, cj_true, cj_offset, uv_offset)

    fixed = fitting.fit_typek(uv_measured, cj_reported, true_temps,
                              cj_offset_c=-cj_offset, fit_gain=False)
    freed = fitting.fit_typek(uv_measured, cj_reported, true_temps,
                              cj_offset_c=-cj_offset, fit_gain=True)

    assert freed.params["uv_gain"] != 1.0
    assert abs(freed.params["uv_offset"] - fixed.params["uv_offset"]) < 2.0
    # Both describe the data equally well; the difference is under 0.05 °C.
    assert freed.max_abs < 0.05 and fixed.max_abs < 0.05


def test_fit_typek_folds_error_into_uv_offset_when_the_cold_junction_is_unknown():
    """The normal bench case: nobody calibrated the cold junction first.

    A cold-junction offset is, to about one percent, indistinguishable from a
    constant voltage offset — the Seebeck coefficient barely moves across any
    plausible cold-junction range. The fit must not report a confident CJ offset it
    cannot possibly have measured. It folds everything into the voltage offset,
    which reads correctly under the same conditions, and says so.
    """
    cj_offset, uv_offset = 0.37, 4.1
    true_temps = np.array([25.0, 100.0, 200.0, 400.0, 600.0])
    cj_true = np.full_like(true_temps, 22.0)
    uv_measured, cj_reported = _typek_points(true_temps, cj_true, cj_offset, uv_offset)

    result = fitting.fit_typek(uv_measured, cj_reported, true_temps)

    assert result.params["cj_offset_c"] == 0.0, "must not invent a CJ offset"
    # The combined error is the CJ error expressed in volts, plus the raw offset.
    combined = -(cj_offset * tc.SEEBECK_UV_PER_C + uv_offset)
    assert result.params["uv_offset"] == pytest.approx(combined, abs=1.5)
    assert result.max_abs < 0.05, "readings are still correct under these conditions"
    assert "absorbed the cold-junction error" in result.message


def test_fit_typek_is_not_fooled_by_a_moving_cold_junction():
    """Varying the cold junction does not rescue the degeneracy, and must not appear to.

    It is tempting to think that warming the board during calibration separates the
    two parameters. It does not: over 18-40 C the Seebeck coefficient changes by
    about one percent, so the effect remains a constant offset to within that.
    """
    cj_offset, uv_offset = 0.37, 4.1
    true_temps = np.array([25.0, 100.0, 200.0, 400.0, 600.0, 300.0])
    cj_true = np.array([18.0, 22.0, 26.0, 31.0, 36.0, 40.0])
    uv_measured, cj_reported = _typek_points(true_temps, cj_true, cj_offset, uv_offset)

    result = fitting.fit_typek(uv_measured, cj_reported, true_temps)
    assert result.params["cj_offset_c"] == 0.0
    assert result.max_abs < 0.1


def test_fit_cj_offset_directly():
    """The only way to pin down the cold junction: measure it against a reference."""
    result = fitting.fit_cj_offset([22.37, 30.41, 40.38], [22.0, 30.0, 40.0])
    assert result.params["cj_offset_c"] == pytest.approx(-0.387, abs=0.01)


def test_fit_linear():
    raw = np.array([1.0, 2.0, 3.0, 4.0])
    ref = raw * 0.998 + 0.011
    result = fitting.fit_linear(raw, ref)
    assert result.params["gain"] == pytest.approx(0.998, rel=1e-6)
    assert result.params["offset"] == pytest.approx(0.011, abs=1e-6)


def test_compare_models_ranks_steinhart_above_beta():
    temps = np.array([0.0, 20.0, 40.0, 60.0, 80.0])
    resistances = ntc.resistance_steinhart(temps)
    ranked = fitting.compare_models(resistances, temps)
    assert ranked[0].model == "steinhart"
    assert ranked[0].rms < ranked[1].rms


def test_validate_catches_implausible_calibration():
    from tjiptemp.calibration.models import CalibrationSet, ChannelCal, validate

    cal = CalibrationSet()
    cal.set("pt1000", ChannelCal("cvd", {"r0": 42.0}))  # not a PT1000
    problems = validate(cal)
    assert any("r0" in p for p in problems)


def test_calibration_json_roundtrip():
    from tjiptemp.calibration.models import CalibrationSet, default_calibration

    original = default_calibration()
    restored = CalibrationSet.from_json(original.to_json())
    assert restored.get("pt1000").model == "cvd"
    assert restored.get("pt1000").params["r0"] == pytest.approx(1000.0)
