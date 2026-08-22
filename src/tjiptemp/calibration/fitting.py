"""Fitting calibration coefficients from reference points.

Each fitter takes measured/reference pairs and returns a :class:`FitResult` that
carries the coefficients, the per-point residuals, and an honest summary of how
well it actually fitted. The residuals matter more than the coefficients: a
Steinhart fit through three points always passes exactly through those three
points, so a zero residual there proves nothing. The wizard therefore shows both
the residuals *and* the number of degrees of freedom, and says so when there are
none.

Everything here is host-side. The device only ever evaluates the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import least_squares

from ..sensors import ntc as ntc_mod
from ..sensors import rtd as rtd_mod
from ..sensors import thermocouple as tc_mod
from .models import ChannelCal, PostTrim


class FitError(ValueError):
    """Not enough points, degenerate points, or a fit that failed to converge."""


@dataclass(slots=True)
class FitResult:
    model: str
    params: dict[str, float]
    #: Reference temperatures (or values) supplied by the user.
    reference: np.ndarray = field(default_factory=lambda: np.array([]))
    #: What the fitted model predicts at each reference point.
    predicted: np.ndarray = field(default_factory=lambda: np.array([]))
    dof: int = 0
    converged: bool = True
    message: str = ""

    @property
    def residuals(self) -> np.ndarray:
        if self.reference.size == 0:
            return np.array([])
        return self.predicted - self.reference

    @property
    def rms(self) -> float:
        r = self.residuals
        return float(np.sqrt(np.mean(r**2))) if r.size else 0.0

    @property
    def max_abs(self) -> float:
        r = self.residuals
        return float(np.max(np.abs(r))) if r.size else 0.0

    @property
    def exactly_determined(self) -> bool:
        """True when there are no spare points, so residuals are meaningless."""
        return self.dof <= 0

    def quality_note(self) -> str:
        """The sentence the wizard shows under the residual plot."""
        if self.exactly_determined:
            return (
                f"{len(self.reference)} points fit {len(self.params)} coefficients exactly — "
                "the residuals are zero by construction and tell you nothing about accuracy. "
                "Add at least one more point to get a real error estimate."
            )
        return (
            f"RMS residual {self.rms * 1000:.1f} mK, worst point {self.max_abs * 1000:.1f} mK, "
            f"{self.dof} degree(s) of freedom."
        )

    def to_channel_cal(self, post: PostTrim | None = None, extra_fit: dict | None = None) -> ChannelCal:
        fit_meta = {
            "rms": round(self.rms, 6),
            "max_abs": round(self.max_abs, 6),
            "n_points": int(self.reference.size),
            "dof": self.dof,
        }
        if extra_fit:
            fit_meta.update(extra_fit)
        return ChannelCal(
            model=self.model,
            params=dict(self.params),
            post=post or PostTrim(),
            fit=fit_meta,
        )


def _prepare(measured, reference, min_points: int, what: str) -> tuple[np.ndarray, np.ndarray]:
    m = np.asarray(measured, dtype=float).ravel()
    r = np.asarray(reference, dtype=float).ravel()
    if m.size != r.size:
        raise FitError(f"{m.size} measured values but {r.size} reference values")
    good = np.isfinite(m) & np.isfinite(r)
    m, r = m[good], r[good]
    if m.size < min_points:
        raise FitError(f"{what} needs at least {min_points} usable points, got {m.size}")
    if np.ptp(r) < 1e-9 and min_points > 1:
        raise FitError(
            "all reference points are at the same value — a fit needs a spread of "
            "temperatures to have anything to fit to"
        )
    return m, r


# ------------------------------------------------------------------- RTD / CVD

def fit_cvd(
    r_ohm,
    t_ref_c,
    *,
    fit_ab: bool | None = None,
    fit_c: bool = False,
    lead_ohm: float = 0.0,
    r0_guess: float = rtd_mod.R0_PT1000,
) -> FitResult:
    """Fit Callendar-Van Dusen coefficients to (resistance, reference temperature) pairs.

    How many coefficients to free depends on how many points you have:

    * 1 point  -> R0 only. This is the ice-point calibration and it is genuinely
      useful: R0 error is the dominant term for a decent platinum sensor.
    * 2-3 points -> R0 and A. Fixes the slope.
    * 4+ points -> R0, A and B. Fixes the curvature.
    * ``fit_c=True`` also frees C, which only affects sub-zero readings, so only
      do it if you have reference points below 0 C.

    ``fit_ab`` overrides the automatic choice.
    """
    r, t = _prepare(r_ohm, t_ref_c, 1, "CVD fit")
    if fit_ab is None:
        fit_ab = r.size >= 2
    free = ["r0"]
    if fit_ab:
        free.append("a")
    if r.size >= 4:
        free.append("b")
    if fit_c:
        if not np.any(t < 0):
            raise FitError(
                "fitting the C coefficient needs at least one reference point below 0 °C — "
                "C has no effect at or above zero"
            )
        free.append("c")

    fixed = {"r0": r0_guess, "a": rtd_mod.A_NOMINAL, "b": rtd_mod.B_NOMINAL, "c": rtd_mod.C_NOMINAL}
    x0 = np.array([fixed[name] for name in free], dtype=float)
    # Coefficients span twelve orders of magnitude; without per-parameter scaling the
    # solver effectively never moves C.
    _SCALE = {"r0": 1e3, "a": 1e-3, "b": 1e-7, "c": 1e-12}
    scale = np.array([_SCALE[name] for name in free])

    def unpack(x):
        p = dict(fixed)
        p.update(dict(zip(free, x, strict=True)))
        return p

    def resid(x):
        p = unpack(x)
        model_t = rtd_mod.temperature(
            r, r0=p["r0"], a=p["a"], b=p["b"], c=p["c"], lead_ohm=lead_ohm
        )
        return np.nan_to_num(np.asarray(model_t) - t, nan=1e3)

    sol = least_squares(resid, x0, x_scale=scale, method="trf", max_nfev=2000)
    params = unpack(sol.x)
    params["lead_ohm"] = float(lead_ohm)
    predicted = np.asarray(
        rtd_mod.temperature(r, r0=params["r0"], a=params["a"], b=params["b"],
                            c=params["c"], lead_ohm=lead_ohm)
    )
    return FitResult(
        model="cvd",
        params={k: float(v) for k, v in params.items()},
        reference=t,
        predicted=predicted,
        dof=int(r.size - len(free)),
        converged=bool(sol.success),
        message="" if sol.success else sol.message,
    )


def fit_lead_resistance(r_measured_ohm: float, r_expected_ohm: float) -> float:
    """Lead resistance from a shorted-probe or known-resistor measurement.

    Short the probe leads at the sensor end (or substitute a calibrated resistor)
    and the difference is the lead contribution. Only meaningful in 2-wire mode.
    """
    return float(r_measured_ohm - r_expected_ohm)


# ------------------------------------------------------------------------ NTC

def fit_steinhart(r_ohm, t_ref_c) -> FitResult:
    """Fit Steinhart-Hart A, B, C to (resistance, reference temperature) pairs.

    With exactly three points this is a linear system in (A, B, C) and is solved
    directly. With more it becomes a linear least-squares problem in the same
    basis -- still linear, because Steinhart-Hart is linear in its coefficients
    once you take ``ln(R)``. No iteration needed either way, which is why it is
    such a well-behaved model.
    """
    r, t = _prepare(r_ohm, t_ref_c, 3, "Steinhart-Hart fit")
    if np.any(r <= 0):
        raise FitError("resistances must be positive")
    ln_r = np.log(r)
    basis = np.column_stack([np.ones_like(ln_r), ln_r, ln_r**3])
    target = 1.0 / (t + ntc_mod.KELVIN)
    coeffs, *_ = np.linalg.lstsq(basis, target, rcond=None)
    a, b, c = (float(v) for v in coeffs)
    predicted = np.asarray(ntc_mod.temperature_steinhart(r, a, b, c))
    return FitResult(
        model="steinhart",
        params={"a": a, "b": b, "c": c},
        reference=t,
        predicted=predicted,
        dof=int(r.size - 3),
    )


def fit_beta(r_ohm, t_ref_c, *, r0: float | None = None, t0_c: float = 25.0) -> FitResult:
    """Fit the Beta model. Two points suffice; more are least-squared.

    If ``r0`` is not given it is fitted too, which is usually what you want -- a
    probe's nominal resistance is as likely to be off as its beta.
    """
    r, t = _prepare(r_ohm, t_ref_c, 2, "Beta fit")
    inv_t = 1.0 / (t + ntc_mod.KELVIN)
    if r0 is None:
        # ln(R) = ln(R0) + beta*(1/T - 1/T0): linear in [ln(R0'), beta]
        basis = np.column_stack([np.ones_like(inv_t), inv_t])
        coeffs, *_ = np.linalg.lstsq(basis, np.log(r), rcond=None)
        beta = float(coeffs[1])
        r0_fit = float(np.exp(coeffs[0] + beta / (t0_c + ntc_mod.KELVIN)))
        n_free = 2
    else:
        beta = float(
            np.sum(np.log(r / r0) * (inv_t - 1.0 / (t0_c + ntc_mod.KELVIN)))
            / np.sum((inv_t - 1.0 / (t0_c + ntc_mod.KELVIN)) ** 2)
        )
        r0_fit = float(r0)
        n_free = 1
    predicted = np.asarray(ntc_mod.temperature_beta(r, beta, r0_fit, t0_c))
    return FitResult(
        model="beta",
        params={"beta": beta, "r0": r0_fit, "t0_c": float(t0_c)},
        reference=t,
        predicted=predicted,
        dof=int(r.size - n_free),
    )


# ---------------------------------------------------------------- thermocouple

def fit_typek(
    uv_measured,
    cj_c,
    t_ref_c,
    *,
    cj_offset_c: float = 0.0,
    fit_gain: bool | None = None,
) -> FitResult:
    """Fit the thermocouple channel's voltage offset and, optionally, its gain.

    **The cold-junction offset is not fitted here, and cannot be.** It is taken as
    a known input, from :func:`fit_cj_offset` or from a datasheet.

    The reason is worth spelling out, because the naive three-parameter fit
    converges beautifully to a wrong answer. Adding ``d`` degrees to the cold
    junction changes the compensating voltage by ``S(cj) * d``, where S is the
    Seebeck coefficient. Over any cold junction range a bench calibration can
    plausibly cover -- say 15 C to 45 C -- S moves from about 40.6 to 41.1 uV/C, a
    little over one percent. So a cold-junction offset is, to within that one
    percent, *exactly* a constant voltage offset. No amount of varying the hot
    junction separates them; the two parameters trade off freely and the solver
    happily reports whichever combination it wandered into.

    So the two are calibrated separately and by different means:

    * the cold junction against a reference thermometer touching the board
      (:func:`fit_cj_offset`), which measures it directly;
    * the voltage offset against reference *hot* junction points, here.

    ``uv_gain`` is only fitted when the reference span exceeds 100 C, because a
    gain error is invisible near the cold junction where the voltage is small.

    Args:
        uv_measured: thermocouple voltage in microvolts at each point (channel 14).
        cj_c: cold junction temperature reported by the board at each point.
        t_ref_c: the true hot junction temperature at each point.
        cj_offset_c: a previously determined cold-junction correction, held fixed.
            Left at zero, ``uv_offset`` absorbs the cold-junction error too, which
            reads correctly as long as the cold junction stays where it was.
    """
    uv = np.asarray(uv_measured, dtype=float).ravel()
    cj = np.asarray(cj_c, dtype=float).ravel()
    t = np.asarray(t_ref_c, dtype=float).ravel()
    if not (uv.size == cj.size == t.size):
        raise FitError("uv, cold junction and reference arrays must be the same length")
    good = np.isfinite(uv) & np.isfinite(cj) & np.isfinite(t)
    uv, cj, t = uv[good], cj[good], t[good]
    if uv.size < 1:
        raise FitError("type-K fit needs at least one usable point")

    span = float(np.ptp(t)) if t.size > 1 else 0.0
    if fit_gain is None:
        fit_gain = span > 100.0 and uv.size >= 3

    free: list[str] = ["uv_offset"]
    if fit_gain:
        free.append("uv_gain")
    free = free[: max(1, uv.size)]  # never free more parameters than there are points
    defaults = {"cj_offset_c": float(cj_offset_c), "uv_offset": 0.0, "uv_gain": 1.0}
    x0 = np.array([defaults[n] for n in free], dtype=float)
    scale = np.array([{"cj_offset_c": 1.0, "uv_offset": 10.0, "uv_gain": 1e-3}[n] for n in free])

    def unpack(x):
        p = dict(defaults)
        p.update(dict(zip(free, x, strict=True)))
        return p

    def resid(x):
        p = unpack(x)
        model_t = tc_mod.temperature(
            uv, cj, uv_offset=p["uv_offset"], uv_gain=p["uv_gain"],
            cj_offset_c=p["cj_offset_c"],
        )
        return np.nan_to_num(np.asarray(model_t) - t, nan=1e3)

    sol = least_squares(resid, x0, x_scale=scale, method="trf", max_nfev=2000)
    params = unpack(sol.x)
    predicted = np.asarray(
        tc_mod.temperature(uv, cj, uv_offset=params["uv_offset"], uv_gain=params["uv_gain"],
                           cj_offset_c=params["cj_offset_c"])
    )
    result = FitResult(
        model="nist_typek",
        params={k: float(v) for k, v in params.items()},
        reference=t,
        predicted=predicted,
        dof=int(uv.size - len(free)),
        converged=bool(sol.success),
        message="" if sol.success else sol.message,
    )
    if cj_offset_c == 0.0:
        result.message = (
            "No cold-junction correction was supplied, so uv_offset ({:+.1f} µV) has "
            "absorbed the cold-junction error as well. Readings are correct while the "
            "cold junction stays near {:.1f} °C. Calibrate the cold junction against a "
            "reference and re-run this fit to separate the two."
        ).format(result.params["uv_offset"], float(np.mean(cj)))
    elif span < 30.0:
        result.message = (
            f"Reference points span only {span:.1f} °C. The coefficients are poorly "
            "constrained over such a narrow range even though the fit looks good."
        )
    return result


def fit_cj_offset(cj_measured_c, cj_ref_c) -> FitResult:
    """Fit just the cold-junction sensor against a reference. The single best-value fix.

    A cold-junction error passes through to the hot-junction reading at very nearly
    1:1, so a 0.5 C error in the MAX31856's internal sensor is a 0.5 C error in every
    thermocouple reading. Correcting this alone often buys more than everything else.
    """
    m, r = _prepare(cj_measured_c, cj_ref_c, 1, "cold junction fit")
    offset = float(np.mean(r - m))
    return FitResult(
        model="nist_typek",
        params={"cj_offset_c": offset, "uv_offset": 0.0, "uv_gain": 1.0},
        reference=r,
        predicted=m + offset,
        dof=int(m.size - 1),
    )


# --------------------------------------------------------------- linear / poly

def fit_linear(measured, reference) -> FitResult:
    """Least-squares ``y = gain*x + offset``. One point fits offset only."""
    m, r = _prepare(measured, reference, 1, "linear fit")
    if m.size == 1:
        return FitResult(
            model="linear",
            params={"gain": 1.0, "offset": float(r[0] - m[0])},
            reference=r, predicted=np.array([r[0]]), dof=0,
        )
    gain, offset = np.polyfit(m, r, 1)
    return FitResult(
        model="linear",
        params={"gain": float(gain), "offset": float(offset)},
        reference=r,
        predicted=m * gain + offset,
        dof=int(m.size - 2),
    )


def fit_poly(measured, reference, degree: int = 2) -> FitResult:
    """Least-squares polynomial correction, for a sensor that fits nothing else.

    Use sparingly: a high-degree polynomial through few points will interpolate
    beautifully and extrapolate catastrophically.
    """
    m, r = _prepare(measured, reference, degree + 1, f"degree-{degree} polynomial fit")
    coeffs_desc = np.polyfit(m, r, degree)
    predicted = np.polyval(coeffs_desc, m)
    return FitResult(
        model="poly",
        params={"coeffs": [float(c) for c in coeffs_desc[::-1]]},
        reference=r,
        predicted=predicted,
        dof=int(m.size - (degree + 1)),
    )


# ------------------------------------------------------------------- reporting

def residual_table(result: FitResult, raw=None, unit: str = "°C") -> list[dict]:
    """Per-point breakdown for the wizard's table and the calibration certificate."""
    rows = []
    raw_arr = np.asarray(raw, dtype=float).ravel() if raw is not None else None
    for i, (ref, pred) in enumerate(
        zip(result.reference, result.predicted, strict=True)
    ):
        row = {
            "n": i + 1,
            "reference": float(ref),
            "predicted": float(pred),
            "residual": float(pred - ref),
            "unit": unit,
        }
        if raw_arr is not None and i < raw_arr.size:
            row["raw"] = float(raw_arr[i])
        rows.append(row)
    return rows


def compare_models(r_ohm, t_ref_c) -> list[FitResult]:
    """Fit every NTC model that the data supports, best first.

    Shown side by side in the wizard so the choice between Steinhart-Hart and Beta
    is made on evidence rather than on which one the user has heard of.
    """
    out: list[FitResult] = []
    for fitter in (fit_steinhart, fit_beta):
        try:
            out.append(fitter(r_ohm, t_ref_c))
        except (FitError, np.linalg.LinAlgError):
            continue
    return sorted(out, key=lambda f: (f.exactly_determined, f.rms))
