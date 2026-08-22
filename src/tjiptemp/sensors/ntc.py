"""NTC thermistor conversion: Steinhart-Hart and the simpler Beta model.

Steinhart-Hart:

    1/T = A + B*ln(R) + C*ln(R)^3        (T in kelvin, R in ohms)

Three coefficients fitted from three (R, T) points give roughly +/-0.01 C over a
100 C span, which is why it is the default. The Beta model:

    1/T = 1/T0 + (1/beta) * ln(R/R0)

needs only a datasheet beta and a nominal resistance, and is typically good to a
few tenths of a degree away from its reference point. It is offered because
sometimes that is all the information you have about a probe.

The board reads its NTCs through a divider into the ESP32-S3 ADC, so this module
also carries the divider maths: the ADC gives a voltage, the divider gives a
resistance, and only then does the thermistor model apply.
"""

from __future__ import annotations

import numpy as np

KELVIN = 273.15

#: A common 10k NTC (B57861S / NTCLE100E3 class, beta ~3435). Reasonable starting
#: point for an uncalibrated probe -- good enough to see something sensible on the
#: screen, not good enough to trust to better than a degree.
DEFAULT_A = 1.129241e-3
DEFAULT_B = 2.341077e-4
DEFAULT_C = 8.775468e-8
DEFAULT_R0 = 10000.0
DEFAULT_T0_C = 25.0
DEFAULT_BETA = 3435.0

#: Outside this window the divider is saturated and the reading means nothing.
_R_MIN = 1.0
_R_MAX = 1e8


def temperature_steinhart(r_ohm, a: float = DEFAULT_A, b: float = DEFAULT_B, c: float = DEFAULT_C):
    """Steinhart-Hart: resistance in ohms -> temperature in C."""
    r = np.asarray(r_ohm, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        valid = np.isfinite(r) & (r > _R_MIN) & (r < _R_MAX)
        ln_r = np.log(np.where(valid, r, np.nan))
        inv_t = a + b * ln_r + c * ln_r**3
        t = np.where(np.abs(inv_t) > 1e-12, 1.0 / inv_t - KELVIN, np.nan)
    return t if np.ndim(r_ohm) else float(t)


def resistance_steinhart(
    t_c, a: float = DEFAULT_A, b: float = DEFAULT_B, c: float = DEFAULT_C
):
    """Inverse Steinhart-Hart: temperature in C -> resistance in ohms.

    Solves the depressed cubic ``C*x^3 + B*x + (A - 1/T) = 0`` for ``x = ln(R)``
    with Cardano's formula. There is exactly one real root for physically sensible
    coefficients (B, C > 0), so no root selection is needed.
    """
    t = np.asarray(t_c, dtype=float) + KELVIN
    with np.errstate(invalid="ignore", divide="ignore"):
        d = a - 1.0 / t
        if abs(c) < 1e-20:  # degenerates to the two-coefficient form
            x = -d / b
        else:
            p = b / c
            q = d / c
            disc = (q / 2.0) ** 2 + (p / 3.0) ** 3
            sqrt_disc = np.sqrt(np.maximum(disc, 0.0))
            u = np.cbrt(-q / 2.0 + sqrt_disc)
            v = np.cbrt(-q / 2.0 - sqrt_disc)
            x = u + v
        r = np.exp(x)
    return r if np.ndim(t_c) else float(r)


def temperature_beta(
    r_ohm, beta: float = DEFAULT_BETA, r0: float = DEFAULT_R0, t0_c: float = DEFAULT_T0_C
):
    """Beta model: resistance in ohms -> temperature in C."""
    r = np.asarray(r_ohm, dtype=float)
    t0 = t0_c + KELVIN
    with np.errstate(invalid="ignore", divide="ignore"):
        valid = np.isfinite(r) & (r > _R_MIN) & (r < _R_MAX)
        inv_t = 1.0 / t0 + np.log(np.where(valid, r, np.nan) / r0) / beta
        t = np.where(np.abs(inv_t) > 1e-12, 1.0 / inv_t - KELVIN, np.nan)
    return t if np.ndim(r_ohm) else float(t)


def resistance_beta(
    t_c, beta: float = DEFAULT_BETA, r0: float = DEFAULT_R0, t0_c: float = DEFAULT_T0_C
):
    t = np.asarray(t_c, dtype=float) + KELVIN
    t0 = t0_c + KELVIN
    r = r0 * np.exp(beta * (1.0 / t - 1.0 / t0))
    return r if np.ndim(t_c) else float(r)


def beta_to_steinhart(
    beta: float = DEFAULT_BETA, r0: float = DEFAULT_R0, t0_c: float = DEFAULT_T0_C
) -> tuple[float, float, float]:
    """Convert a Beta specification into equivalent Steinhart-Hart coefficients (C = 0).

    Lets the calibration store exactly one model while still accepting a datasheet
    beta as input.
    """
    t0 = t0_c + KELVIN
    b = 1.0 / beta
    a = 1.0 / t0 - b * np.log(r0)
    return float(a), float(b), 0.0


# ------------------------------------------------------------------ front-end

def divider_resistance(
    v_adc, v_ref: float = 3.3, r_series: float = 10000.0, pullup_to: str = "vcc"
):
    """Convert a divider node voltage into the thermistor's resistance.

    ``pullup_to="vcc"``: series resistor to the rail, thermistor to ground, ADC on
    the junction -- so ``R_ntc = R_series * V / (Vref - V)``.

    ``pullup_to="gnd"``: the thermistor is the top leg instead, giving
    ``R_ntc = R_series * (Vref - V) / V``.
    """
    v = np.asarray(v_adc, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        if pullup_to == "gnd":
            r = r_series * (v_ref - v) / v
        else:
            r = r_series * v / (v_ref - v)
        r = np.where(np.isfinite(r) & (r > 0), r, np.nan)
    return r if np.ndim(v_adc) else float(r)


def divider_voltage(
    r_ntc, v_ref: float = 3.3, r_series: float = 10000.0, pullup_to: str = "vcc"
):
    """Inverse of :func:`divider_resistance`, used by the simulator."""
    r = np.asarray(r_ntc, dtype=float)
    if pullup_to == "gnd":
        return v_ref * r_series / (r + r_series)
    return v_ref * r / (r + r_series)


def temperature_from_voltage(
    v_adc,
    *,
    a: float = DEFAULT_A,
    b: float = DEFAULT_B,
    c: float = DEFAULT_C,
    v_ref: float = 3.3,
    r_series: float = 10000.0,
    pullup_to: str = "vcc",
):
    """ADC voltage straight through to temperature in C."""
    return temperature_steinhart(
        divider_resistance(v_adc, v_ref=v_ref, r_series=r_series, pullup_to=pullup_to), a, b, c
    )


def optimal_series_resistor(
    t_min_c: float, t_max_c: float, a: float = DEFAULT_A, b: float = DEFAULT_B, c: float = DEFAULT_C
) -> float:
    """Series resistor that maximises divider sensitivity across a temperature span.

    The geometric mean of the endpoint resistances -- the classic result, and worth
    surfacing in the UI when someone is choosing a probe for a narrow range.
    """
    r_hot = float(resistance_steinhart(t_max_c, a, b, c))
    r_cold = float(resistance_steinhart(t_min_c, a, b, c))
    return float(np.sqrt(r_hot * r_cold))


def self_heating_c(r_ohm: float, v_ref: float, r_series: float, dissipation_mw_per_c: float = 2.0) -> float:
    """Estimate self-heating error from the divider current, in C.

    A 10k NTC at 3.3 V through a 10k series resistor dissipates about 0.27 mW at
    25 C; at a typical 2 mW/C dissipation constant in still air that is ~0.14 C of
    self-heating. Small but not nothing, and it is systematically *warm*, so it is
    worth showing rather than hiding.
    """
    if r_ohm <= 0 or dissipation_mw_per_c <= 0:
        return 0.0
    current = v_ref / (r_series + r_ohm)
    power_mw = current * current * r_ohm * 1000.0
    return float(power_mw / dissipation_mw_per_c)
