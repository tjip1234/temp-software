"""Type-K thermocouple conversion using the ITS-90 / NIST reference functions.

A thermocouple measures the *difference* between the hot junction and the cold
(reference) junction, so the conversion is always three steps:

    1. E_cj      = forward(T_cold_junction)      the voltage the cold junction "hides"
    2. E_total   = E_measured + E_cj             voltage referred to 0 C
    3. T_hot     = inverse(E_total)

Getting step 1 right is what separates a good thermocouple reading from a bad one.
The MAX31856 does this internally with its own cold-junction sensor, but the board
also has a dedicated NTC beside the TC connector, and the protocol lets you choose
which one to trust (``config.tc.cj_source``). Doing the maths here as well means the
host can re-derive temperature from the raw microvolts channel with a *different*
cold junction after the fact -- which is exactly what you want when you discover the
internal CJ sensor was reading 0.4 C high all afternoon.

Coefficients are the NIST ITS-90 type-K tables. Voltages are millivolts internally;
the public API takes and returns microvolts because that is what the MAX31856
reports and what the protocol carries on channel 14.
"""

from __future__ import annotations

import numpy as np

# --- forward: temperature (C) -> emf (mV) -------------------------------------

# -270 C .. 0 C
_FWD_NEG = (
    0.000000000000e00,
    0.394501280250e-01,
    0.236223735980e-04,
    -0.328589067840e-06,
    -0.499048287770e-08,
    -0.675090591730e-10,
    -0.574103274280e-12,
    -0.310888728940e-14,
    -0.104516093650e-16,
    -0.198892668780e-19,
    -0.163226974860e-22,
)

# 0 C .. 1372 C, plus the exponential term that models the type-K kink near 127 C
_FWD_POS = (
    -0.176004136860e-01,
    0.389212049750e-01,
    0.185587700320e-04,
    -0.994575928740e-07,
    0.318409457190e-09,
    -0.560728448890e-12,
    0.560750590590e-15,
    -0.320207200030e-18,
    0.971511471520e-22,
    -0.121047212750e-25,
)
_FWD_EXP = (0.118597600000e00, -0.118343200000e-03, 0.126968600000e03)

# --- inverse: emf (mV) -> temperature (C) -------------------------------------

# -5.891 .. 0 mV  (-200 .. 0 C), quoted error -0.02 / +0.04 C
_INV_LOW = (
    0.0000000e00,
    2.5173462e01,
    -1.1662878e00,
    -1.0833638e00,
    -8.9773540e-01,
    -3.7342377e-01,
    -8.6632643e-02,
    -1.0450598e-02,
    -5.1920577e-04,
)

# 0 .. 20.644 mV  (0 .. 500 C), quoted error -0.05 / +0.04 C
_INV_MID = (
    0.000000e00,
    2.508355e01,
    7.860106e-02,
    -2.503131e-01,
    8.315270e-02,
    -1.228034e-02,
    9.804036e-04,
    -4.413030e-05,
    1.057734e-06,
    -1.052755e-08,
)

# 20.644 .. 54.886 mV  (500 .. 1372 C), quoted error -0.05 / +0.06 C
_INV_HIGH = (
    -1.318058e02,
    4.830222e01,
    -1.646031e00,
    5.464731e-02,
    -9.650715e-04,
    8.802193e-06,
    -3.110810e-08,
)

T_MIN_C = -270.0
T_MAX_C = 1372.0
MV_MIN = -6.458
MV_MAX = 54.886

#: Nominal type-K sensitivity near room temperature, uV/C. Handy for error budgets.
SEEBECK_UV_PER_C = 41.276


def _polyval(coeffs: tuple[float, ...], x: np.ndarray) -> np.ndarray:
    """Horner evaluation with coefficients in ascending order."""
    out = np.zeros_like(x, dtype=float)
    for c in reversed(coeffs):
        out = out * x + c
    return out


def emf_mv(t_c) -> np.ndarray | float:
    """Forward reference function: temperature in C -> thermoelectric emf in mV."""
    t = np.asarray(t_c, dtype=float)
    neg = _polyval(_FWD_NEG, t)
    pos = _polyval(_FWD_POS, t)
    a0, a1, a2 = _FWD_EXP
    pos = pos + a0 * np.exp(a1 * (t - a2) ** 2)
    out = np.where(t < 0.0, neg, pos)
    out = np.where(np.isfinite(t) & (t >= T_MIN_C) & (t <= T_MAX_C), out, np.nan)
    return out if np.ndim(t_c) else float(out)


def emf_uv(t_c):
    """Forward reference function in microvolts, which is what the hardware speaks."""
    return np.asarray(emf_mv(t_c)) * 1000.0 if np.ndim(t_c) else emf_mv(t_c) * 1000.0


def temperature_from_mv(e_mv) -> np.ndarray | float:
    """Inverse reference function: emf in mV (referred to 0 C) -> temperature in C."""
    e = np.asarray(e_mv, dtype=float)
    low = _polyval(_INV_LOW, e)
    mid = _polyval(_INV_MID, e)
    high = _polyval(_INV_HIGH, e)
    out = np.where(e < 0.0, low, np.where(e < 20.644, mid, high))
    out = np.where(np.isfinite(e) & (e >= MV_MIN) & (e <= MV_MAX), out, np.nan)
    return out if np.ndim(e_mv) else float(out)


def temperature(
    e_uv,
    cj_c,
    *,
    uv_offset: float = 0.0,
    uv_gain: float = 1.0,
    cj_offset_c: float = 0.0,
):
    """Full cold-junction-compensated conversion.

    Args:
        e_uv: measured thermocouple voltage in microvolts (channel 14).
        cj_c: cold junction temperature in C (channel 2, or the board NTC).
        uv_offset / uv_gain: per-probe correction of the measured voltage. Offset in
            uV covers amplifier offset and parasitic junctions in the wiring; gain
            covers a wire alloy that is not quite standard. At ~41 uV/C, 1 uV of
            offset is about 0.024 C.
        cj_offset_c: correction of the cold junction sensor itself. This is usually
            the single largest correctable error in a thermocouple channel, because
            an error here passes straight through to the result at unity gain.

    Returns:
        Hot junction temperature in C, NaN where the input is out of range.
    """
    e = (np.asarray(e_uv, dtype=float) * uv_gain + uv_offset) / 1000.0  # -> mV
    cj = np.asarray(cj_c, dtype=float) + cj_offset_c
    e_total = e + np.asarray(emf_mv(cj))
    result = temperature_from_mv(e_total)
    scalar = np.ndim(e_uv) == 0 and np.ndim(cj_c) == 0
    return float(result) if scalar else result


def sensitivity_uv_per_c(t_c) -> np.ndarray | float:
    """Local Seebeck coefficient in uV/C, by numerical derivative of the reference function."""
    t = np.asarray(t_c, dtype=float)
    h = 0.5
    d = (np.asarray(emf_mv(t + h)) - np.asarray(emf_mv(t - h))) / (2.0 * h) * 1000.0
    return d if np.ndim(t_c) else float(d)


def cj_error_contribution(cj_error_c: float, t_c: float = 25.0) -> float:
    """How much a cold-junction error costs at the hot junction, in C.

    Very nearly 1:1 near room temperature, and the ratio only drifts slowly with
    hot-junction temperature. Shown in the calibration wizard so the user can see
    why the cold junction matters as much as the probe.
    """
    return float(cj_error_c * sensitivity_uv_per_c(25.0) / sensitivity_uv_per_c(t_c))


#: MAX31856 fault register bits.
MAX31856_FAULTS = {
    0x80: ("cj_range", "Cold junction outside its normal operating range"),
    0x40: ("tc_range", "Thermocouple temperature outside the type's valid range"),
    0x20: ("cj_high", "Cold junction above the configured high threshold"),
    0x10: ("cj_low", "Cold junction below the configured low threshold"),
    0x08: ("tc_high", "Thermocouple above the configured high threshold"),
    0x04: ("tc_low", "Thermocouple below the configured low threshold"),
    0x02: ("ovuv", "Over/under voltage on a thermocouple input"),
    0x01: ("open", "Open circuit: the thermocouple is disconnected or broken"),
}


def decode_max31856_fault(reg: int) -> list[dict]:
    return [
        {"bit": bit, "name": name, "detail": detail}
        for bit, (name, detail) in MAX31856_FAULTS.items()
        if reg & bit
    ]


#: The types the MAX31856 supports. Only K is fully implemented here; the rest are
#: listed so the config UI can offer them and the firmware can use its internal
#: linearisation. Adding another type means adding its NIST coefficient set above.
SUPPORTED_TYPES = ("K",)
CHIP_TYPES = ("B", "E", "J", "K", "N", "R", "S", "T")
