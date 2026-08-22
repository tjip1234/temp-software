"""Calibration models: the objects that live in NVS and turn raw readings into truth.

The division of labour, per ``docs/protocol.md`` §7, is that the *host* fits and the
*device* evaluates. That matters: it means the 240x320 screen shows exactly the same
number the desktop does, with no host in the loop. This module implements both sides
-- fitting lives in ``fitting.py``, evaluation lives here, and the same evaluation is
mirrored in the firmware.

Every model ends with an optional ``post`` linear trim so a user can nudge a probe by
a known offset without disturbing a carefully fitted sensor curve.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from ..sensors import ntc as ntc_mod
from ..sensors import rtd as rtd_mod
from ..sensors import thermocouple as tc_mod

CAL_SCHEMA_VERSION = 1


class CalibrationError(ValueError):
    """A calibration object is malformed or physically implausible."""


@dataclass(slots=True)
class PostTrim:
    """Final linear touch-up applied after the physical model: ``y = gain*x + offset``."""

    offset_c: float = 0.0
    gain: float = 1.0

    def apply(self, value):
        if self.gain == 1.0 and self.offset_c == 0.0:
            return value
        return value * self.gain + self.offset_c

    def to_json(self) -> dict:
        return {"offset_c": self.offset_c, "gain": self.gain}

    @classmethod
    def from_json(cls, obj: dict | None) -> PostTrim:
        obj = obj or {}
        return cls(
            offset_c=float(obj.get("offset_c", obj.get("offset", 0.0))),
            gain=float(obj.get("gain", 1.0)),
        )

    @property
    def is_identity(self) -> bool:
        return self.gain == 1.0 and self.offset_c == 0.0


@dataclass(slots=True)
class ChannelCal:
    """Calibration for one channel. ``model`` selects which of the params matter."""

    model: str = "identity"
    params: dict[str, float] = field(default_factory=dict)
    post: PostTrim = field(default_factory=PostTrim)
    #: Free-form provenance: what it was fitted against and how well it fitted.
    fit: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- evaluation

    def apply(self, raw, *, cj_c=None):
        """Convert a raw measurement to a calibrated value.

        What "raw" means depends on the model: ohms for ``cvd`` and ``steinhart``,
        microvolts for ``nist_typek``, and the already-physical value for ``linear``,
        ``poly`` and ``identity``.
        """
        p = self.params
        if self.model == "identity":
            out = raw
        elif self.model == "cvd":
            out = rtd_mod.temperature(
                raw,
                r0=p.get("r0", rtd_mod.R0_PT1000),
                a=p.get("a", rtd_mod.A_NOMINAL),
                b=p.get("b", rtd_mod.B_NOMINAL),
                c=p.get("c", rtd_mod.C_NOMINAL),
                lead_ohm=p.get("lead_ohm", 0.0),
            )
        elif self.model == "steinhart":
            out = ntc_mod.temperature_steinhart(
                raw, p.get("a", ntc_mod.DEFAULT_A), p.get("b", ntc_mod.DEFAULT_B),
                p.get("c", ntc_mod.DEFAULT_C),
            )
        elif self.model == "beta":
            out = ntc_mod.temperature_beta(
                raw, p.get("beta", ntc_mod.DEFAULT_BETA), p.get("r0", ntc_mod.DEFAULT_R0),
                p.get("t0_c", ntc_mod.DEFAULT_T0_C),
            )
        elif self.model == "nist_typek":
            if cj_c is None:
                raise CalibrationError("nist_typek needs a cold junction temperature")
            out = tc_mod.temperature(
                raw, cj_c,
                uv_offset=p.get("uv_offset", 0.0),
                uv_gain=p.get("uv_gain", 1.0),
                cj_offset_c=p.get("cj_offset_c", 0.0),
            )
        elif self.model == "linear":
            out = np.asarray(raw, dtype=float) * p.get("gain", 1.0) + p.get("offset", 0.0)
            if np.ndim(raw) == 0:
                out = float(out)
        elif self.model == "poly":
            coeffs = self.coeffs
            x = np.asarray(raw, dtype=float)
            acc = np.zeros_like(x, dtype=float)
            for c in reversed(coeffs):
                acc = acc * x + c
            out = acc if np.ndim(raw) else float(acc)
        else:
            raise CalibrationError(f"unknown calibration model {self.model!r}")
        return self.post.apply(out)

    @property
    def coeffs(self) -> list[float]:
        """Polynomial coefficients, ascending order, for the ``poly`` model."""
        raw = self.params.get("coeffs")
        if isinstance(raw, (list, tuple)):
            return [float(c) for c in raw]
        # tolerate c0/c1/c2... spellings, which are easier to hand-edit in JSON
        out: list[float] = []
        i = 0
        while f"c{i}" in self.params:
            out.append(float(self.params[f"c{i}"]))
            i += 1
        return out or [0.0, 1.0]

    @property
    def is_default(self) -> bool:
        """True when this channel has never actually been calibrated."""
        return self.model == "identity" and self.post.is_identity and not self.fit

    def describe(self) -> str:
        """One-line human summary, for tables and tooltips."""
        p = self.params
        if self.model == "cvd":
            s = f"CVD R0={p.get('r0', 1000.0):.4f}Ω"
            if p.get("lead_ohm"):
                s += f", lead {p['lead_ohm']:.4f}Ω"
        elif self.model == "steinhart":
            s = (f"Steinhart A={p.get('a', 0):.6e} B={p.get('b', 0):.6e} "
                 f"C={p.get('c', 0):.6e}")
        elif self.model == "beta":
            s = f"Beta β={p.get('beta', 0):.1f} R0={p.get('r0', 0):.0f}Ω"
        elif self.model == "nist_typek":
            s = (f"Type K, CJ {p.get('cj_offset_c', 0.0):+.3f}°C, "
                 f"{p.get('uv_offset', 0.0):+.2f}µV")
        elif self.model == "linear":
            s = f"gain {p.get('gain', 1.0):.6f}, offset {p.get('offset', 0.0):+.6f}"
        elif self.model == "poly":
            s = f"poly deg {len(self.coeffs) - 1}"
        else:
            s = "uncalibrated"
        if not self.post.is_identity:
            s += f"  (trim {self.post.gain:.5f}x {self.post.offset_c:+.4f})"
        return s

    # ------------------------------------------------------------------- json

    def to_json(self) -> dict:
        obj: dict[str, Any] = {"model": self.model}
        obj.update({k: v for k, v in self.params.items()})
        if not self.post.is_identity:
            obj["post"] = self.post.to_json()
        if self.fit:
            obj["fit"] = self.fit
        return obj

    @classmethod
    def from_json(cls, obj: dict) -> ChannelCal:
        obj = dict(obj or {})
        model = str(obj.pop("model", "identity"))
        post = PostTrim.from_json(obj.pop("post", None))
        fit = obj.pop("fit", None) or {}
        params = {}
        for key, value in obj.items():
            if isinstance(value, (int, float)):
                params[key] = float(value)
            elif key == "coeffs" and isinstance(value, list):
                params[key] = [float(v) for v in value]
        return cls(model=model, params=params, post=post, fit=fit)


@dataclass(slots=True)
class CalibrationSet:
    """The full calibration for a board, keyed by channel key (not id).

    Keyed by key rather than id because a human reads this file: ``"pt1000"`` says
    what it is, ``"0"`` does not. The id mapping is in DEVICE_INFO where it belongs.
    """

    rev: int = 0
    updated_utc: str = ""
    by: str = ""
    reference: str = ""
    channels: dict[str, ChannelCal] = field(default_factory=dict)
    notes: str = ""

    def get(self, key: str) -> ChannelCal:
        return self.channels.get(key) or ChannelCal()

    def set(self, key: str, cal: ChannelCal) -> None:
        self.channels[key] = cal

    def touched(self, by: str = "", reference: str = "") -> CalibrationSet:
        """Return a copy stamped as freshly edited. The device owns ``rev``."""
        return replace(
            self,
            updated_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            by=by or self.by,
            reference=reference or self.reference,
        )

    @property
    def calibrated_channels(self) -> list[str]:
        return sorted(k for k, c in self.channels.items() if not c.is_default)

    def to_json(self) -> dict:
        return {
            "schema": CAL_SCHEMA_VERSION,
            "rev": self.rev,
            "updated_utc": self.updated_utc,
            "by": self.by,
            "reference": self.reference,
            "notes": self.notes,
            "channels": {k: c.to_json() for k, c in self.channels.items()},
        }

    @classmethod
    def from_json(cls, obj: dict) -> CalibrationSet:
        obj = obj or {}
        return cls(
            rev=int(obj.get("rev", 0)),
            updated_utc=str(obj.get("updated_utc", "")),
            by=str(obj.get("by", "")),
            reference=str(obj.get("reference", "")),
            notes=str(obj.get("notes", "")),
            channels={
                str(k): ChannelCal.from_json(v)
                for k, v in (obj.get("channels") or {}).items()
            },
        )


# --------------------------------------------------------------------- factory

def default_calibration() -> CalibrationSet:
    """The nominal calibration a freshly flashed board reports.

    These are datasheet values, not measurements. A board running this is perfectly
    usable -- it just should not be believed to better than a degree or so, and the
    UI says as much.
    """
    cal = CalibrationSet(rev=0, by="factory-nominal")
    cal.set("pt1000", ChannelCal("cvd", {
        "r0": rtd_mod.R0_PT1000,
        "a": rtd_mod.A_NOMINAL,
        "b": rtd_mod.B_NOMINAL,
        "c": rtd_mod.C_NOMINAL,
        "lead_ohm": 0.0,
    }))
    cal.set("typek", ChannelCal("nist_typek", {
        "cj_offset_c": 0.0, "uv_offset": 0.0, "uv_gain": 1.0,
    }))
    for key in ("ntc_ext1", "ntc_ext2", "ntc_ext3", "ntc_brd_rtd", "ntc_brd_tc", "ntc_brd_chg"):
        cal.set(key, ChannelCal("steinhart", {
            "a": ntc_mod.DEFAULT_A, "b": ntc_mod.DEFAULT_B, "c": ntc_mod.DEFAULT_C,
            "r_series_ohm": 10000.0,
        }))
    for key in ("v_bat", "v_cc", "aht20_t", "aht20_rh", "typek_cj"):
        cal.set(key, ChannelCal("linear", {"gain": 1.0, "offset": 0.0}))
    return cal


# ------------------------------------------------------------------ validation

#: Plausibility bounds. These exist to stop a fat-fingered coefficient from being
#: written to NVS, not to enforce physics -- an unusual but deliberate value should
#: still be accepted, so the ranges are wide.
_BOUNDS: dict[tuple[str, str], tuple[float, float]] = {
    ("cvd", "r0"): (50.0, 5000.0),
    ("cvd", "a"): (1e-3, 1e-2),
    ("cvd", "b"): (-1e-5, 1e-5),
    ("cvd", "c"): (-1e-10, 1e-10),
    ("cvd", "lead_ohm"): (-10.0, 100.0),
    ("steinhart", "a"): (-1e-2, 1e-2),
    ("steinhart", "b"): (-1e-2, 1e-2),
    ("steinhart", "c"): (-1e-4, 1e-4),
    ("beta", "beta"): (500.0, 10000.0),
    ("beta", "r0"): (10.0, 1e7),
    ("nist_typek", "cj_offset_c"): (-25.0, 25.0),
    ("nist_typek", "uv_offset"): (-5000.0, 5000.0),
    ("nist_typek", "uv_gain"): (0.5, 2.0),
    ("linear", "gain"): (0.1, 10.0),
}


def validate(cal: CalibrationSet) -> list[str]:
    """Return a list of human-readable problems. Empty list means it is safe to write."""
    problems: list[str] = []
    for key, ch in cal.channels.items():
        if ch.model not in ("identity", "cvd", "steinhart", "beta", "nist_typek", "linear", "poly"):
            problems.append(f"{key}: unknown model {ch.model!r}")
            continue
        for pname, value in ch.params.items():
            if isinstance(value, list):
                continue
            bounds = _BOUNDS.get((ch.model, pname))
            if bounds and not (bounds[0] <= value <= bounds[1]):
                problems.append(
                    f"{key}.{pname} = {value:g} is outside the plausible range "
                    f"[{bounds[0]:g}, {bounds[1]:g}]"
                )
            if not np.isfinite(value):
                problems.append(f"{key}.{pname} is not a finite number")
        if not (0.1 <= ch.post.gain <= 10.0):
            problems.append(f"{key}: post-trim gain {ch.post.gain:g} is implausible")
        if abs(ch.post.offset_c) > 100.0:
            problems.append(f"{key}: post-trim offset {ch.post.offset_c:g} is implausible")
        if ch.model == "steinhart":
            # A monotonic decreasing R(T) is the defining property of an NTC; a bad
            # fit can silently produce coefficients that violate it.
            probe = ntc_mod.temperature_steinhart(
                np.array([1000.0, 10000.0, 100000.0]),
                ch.params.get("a", ntc_mod.DEFAULT_A),
                ch.params.get("b", ntc_mod.DEFAULT_B),
                ch.params.get("c", ntc_mod.DEFAULT_C),
            )
            if not np.all(np.diff(probe) < 0):
                problems.append(
                    f"{key}: Steinhart coefficients are not monotonic -- the fit is bad, "
                    "check that your reference points are not degenerate"
                )
    return problems
