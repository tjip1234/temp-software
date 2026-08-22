"""PT100/PT1000 resistance <-> temperature via Callendar-Van Dusen.

IEC 60751 defines, for a platinum RTD of nominal resistance R0:

    t >= 0 C:  R(t) = R0 * (1 + A*t + B*t^2)
    t <  0 C:  R(t) = R0 * (1 + A*t + B*t^2 + C*(t - 100)*t^3)

with nominal A = 3.9083e-3, B = -5.775e-7, C = -4.183e-12 for a class-standard
alpha = 0.003851 sensor. A calibration replaces R0 (and optionally A, B, C) with
values fitted to your reference points.

Above 0 C the inverse is exact via the quadratic formula. Below 0 C the C term
makes it a quartic, so we start from the quadratic root and run Newton-Raphson;
in practice that converges in two or three iterations across the whole -200 C
range because the curve is nearly linear.

All functions are numpy-vectorised: they accept scalars or arrays and preserve
NaN, which is how a faulted reading travels through the pipeline.
"""

from __future__ import annotations

import numpy as np

#: IEC 60751 nominal coefficients (alpha = 0.00385055 /C).
A_NOMINAL = 3.9083e-3
B_NOMINAL = -5.775e-7
C_NOMINAL = -4.183e-12

#: Sensible defaults for the MAX31865 front-end fitted on this board.
R0_PT1000 = 1000.0
R0_PT100 = 100.0

#: Below this resistance ratio the RTD is out of any plausible range.
_MIN_RATIO = 0.05


def resistance(
    t_c,
    r0: float = R0_PT1000,
    a: float = A_NOMINAL,
    b: float = B_NOMINAL,
    c: float = C_NOMINAL,
):
    """Forward CVD: temperature in C -> resistance in ohms."""
    t = np.asarray(t_c, dtype=float)
    poly = 1.0 + a * t + b * t * t
    below = t < 0.0
    if np.any(below):
        poly = np.where(below, poly + c * (t - 100.0) * t**3, poly)
    return r0 * poly


def temperature(
    r_ohm,
    r0: float = R0_PT1000,
    a: float = A_NOMINAL,
    b: float = B_NOMINAL,
    c: float = C_NOMINAL,
    *,
    lead_ohm: float = 0.0,
    newton_iters: int = 6,
):
    """Inverse CVD: resistance in ohms -> temperature in C.

    ``lead_ohm`` is subtracted first. That is the 2-wire lead compensation: in a
    2-wire hookup the lead resistance adds directly to the measurement, and for a
    PT1000 every 1 ohm of lead is roughly 0.26 C of error. In 3- and 4-wire modes
    the MAX31865 already cancels it and this should be left at zero.
    """
    r = np.asarray(r_ohm, dtype=float) - lead_ohm
    ratio = r / r0

    with np.errstate(invalid="ignore"):
        # Positive branch, exact: B*t^2 + A*t + (1 - R/R0) = 0
        disc = a * a - 4.0 * b * (1.0 - ratio)
        t_pos = (-a + np.sqrt(disc)) / (2.0 * b)

        # Negative branch: seed with the positive-branch solution, then Newton on
        # f(t) = R0*(1 + A t + B t^2 + C (t-100) t^3) - R
        t_neg = t_pos.copy() if np.ndim(t_pos) else np.array(t_pos, dtype=float)
        for _ in range(newton_iters):
            f = r0 * (1.0 + a * t_neg + b * t_neg**2 + c * (t_neg - 100.0) * t_neg**3) - r
            df = r0 * (a + 2.0 * b * t_neg + c * (4.0 * t_neg**3 - 300.0 * t_neg**2))
            step = np.where(np.abs(df) > 1e-12, f / df, 0.0)
            t_neg = t_neg - step

        out = np.where(ratio >= 1.0, t_pos, t_neg)
        out = np.where(np.isfinite(ratio) & (ratio > _MIN_RATIO), out, np.nan)

    return out if np.ndim(r_ohm) else float(out)


def sensitivity(t_c, r0: float = R0_PT1000, a: float = A_NOMINAL, b: float = B_NOMINAL):
    """dR/dT in ohm/C. Useful for turning an ohm uncertainty into a C uncertainty."""
    t = np.asarray(t_c, dtype=float)
    return r0 * (a + 2.0 * b * t)


def resolution_c(r0: float = R0_PT1000, rref_ohm: float = 4000.0, t_c: float = 25.0) -> float:
    """Temperature resolution of one MAX31865 LSB at a given temperature.

    The MAX31865 returns a 15-bit ratio of RTD to reference resistor, so one LSB is
    Rref/32768 ohms. At 25 C with a 4k reference and a PT1000 that is about 0.031 C
    -- worth showing in the UI so nobody chases noise below the converter floor.
    """
    lsb_ohm = rref_ohm / 32768.0
    return float(lsb_ohm / sensitivity(t_c, r0))


def adc_code_to_ohm(code: int, rref_ohm: float = 4000.0) -> float:
    """MAX31865 15-bit RTD code -> resistance. The low bit of the register is the fault flag."""
    return (code & 0x7FFF) * rref_ohm / 32768.0


def ohm_to_adc_code(r_ohm: float, rref_ohm: float = 4000.0) -> int:
    return int(round(r_ohm * 32768.0 / rref_ohm)) & 0x7FFF


#: MAX31865 fault register bit meanings, for surfacing a fault verbatim to the user.
MAX31865_FAULTS = {
    0x80: ("rtd_high_threshold", "RTD resistance above the configured high threshold"),
    0x40: ("rtd_low_threshold", "RTD resistance below the configured low threshold"),
    0x20: ("refin_low", "REFIN- below 0.85*VBIAS: reference resistor or wiring problem"),
    0x10: ("refin_high", "REFIN- above 0.85*VBIAS with FORCE- open: RTD lead open"),
    0x08: ("rtdin_low", "RTDIN- below 0.85*VBIAS with FORCE- open: RTD lead open"),
    0x04: ("voltage", "Overvoltage or undervoltage on an input pin"),
}


def decode_max31865_fault(reg: int) -> list[dict]:
    """Decode the MAX31865 fault status register into something a user can act on."""
    return [
        {"bit": bit, "name": name, "detail": detail}
        for bit, (name, detail) in MAX31865_FAULTS.items()
        if reg & bit
    ]


def wire_mode_note(wires: int) -> str:
    """What the chosen wiring actually costs you, in plain terms."""
    return {
        2: "2-wire: lead resistance adds directly to the reading (~0.26 C per ohm of lead "
           "on a PT1000). Measure your leads and set lead_ohm in the calibration.",
        3: "3-wire: cancels lead resistance assuming all three leads match. Residual error "
           "is the mismatch between them, typically a few mC.",
        4: "4-wire: lead resistance fully cancelled. Use this whenever the probe allows it.",
    }.get(wires, f"Unknown wire count: {wires}")
