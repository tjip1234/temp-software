"""Canonical channel identities for the TjipTemp board.

Channel *ids* are the stable wire identity (``docs/protocol.md`` §4); everything
else here is host-side presentation. A device announces its own channel list in
DEVICE_INFO and the host must honour that — this table is the fallback for a
device that reports nothing, and the source of nice names and colours.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Ch(IntEnum):
    """Wire channel ids. Do not renumber: these live in stored recordings."""

    PT1000 = 0
    TYPEK = 1
    TYPEK_CJ = 2
    NTC_EXT1 = 3
    NTC_EXT2 = 4
    NTC_EXT3 = 5
    NTC_BRD_RTD = 6
    NTC_BRD_TC = 7
    NTC_BRD_CHG = 8
    V_BAT = 9
    V_CC = 10
    AHT20_T = 11
    AHT20_RH = 12
    PT1000_R = 13
    TYPEK_UV = 14


class Kind:
    RTD = "rtd"
    TC = "tc"
    CJ = "cj"
    NTC = "ntc"
    VOLT = "volt"
    HYGRO = "hygro"
    RAW = "raw"


# --------------------------------------------------------------------- palette
#
# Eight categorical slots, validated for colour-vision deficiency and for contrast
# against both a light and a dark chart surface (adjacent-pair CVD ΔE >= 8.4,
# normal-vision ΔE >= 19.3 in both modes). The dark column is the same eight hues
# re-stepped for a dark surface, not an automatic inversion.
#
# Colour is bound to the *channel*, never to its position in whatever selection the
# user has made -- so hiding a trace never repaints the others.
#
# There are more channels than slots, which is deliberate rather than an oversight:
#
# * The eight slots go to the eight channels people actually plot together.
# * Two board-internal NTCs are diagnostics, not subjects. They get recessive grey
#   and are drawn dashed, so their identity never rests on hue alone.
# * Channels in other units (volts, %RH, ohms, microvolts) never share an axis with
#   a temperature -- they are separate plots -- so they reuse the slot colours
#   without ambiguity.
#
# If more than eight temperature traces are ever selected onto one axis, the plot
# facets into probes and board internals instead of inventing a ninth hue.

_SLOTS_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                "#e87ba4", "#008300", "#4a3aa7", "#e34948")
_SLOTS_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500",
               "#d55181", "#008300", "#9085e9", "#e66767")

#: Deliberately low-chroma: these recede, and carry a dashed stroke as secondary
#: encoding so they remain identifiable without relying on colour.
_MUTED_LIGHT = ("#6b6a66", "#8a8984")
_MUTED_DARK = ("#a3a29a", "#7d7c76")

_SLOT_OF = {
    # temperature group -- these eight can appear on one axis together
    Ch.PT1000: 0,
    Ch.TYPEK: 1,
    Ch.NTC_EXT1: 2,
    Ch.NTC_EXT2: 3,
    Ch.NTC_EXT3: 4,
    Ch.AHT20_T: 5,
    Ch.TYPEK_CJ: 6,
    Ch.NTC_BRD_CHG: 7,
    # other units -- own plots, so slot reuse is unambiguous
    Ch.V_BAT: 0,
    Ch.V_CC: 1,
    Ch.AHT20_RH: 2,
    Ch.PT1000_R: 0,
    Ch.TYPEK_UV: 1,
}

#: Diagnostics: grey, dashed, and never competing with a probe for attention.
_MUTED_OF = {Ch.NTC_BRD_RTD: 0, Ch.NTC_BRD_TC: 1}


def color_for(channel_id: int, dark: bool = False) -> str:
    """The stable colour of a channel, for the requested chart surface."""
    if channel_id in _MUTED_OF:
        table = _MUTED_DARK if dark else _MUTED_LIGHT
        return table[_MUTED_OF[channel_id]]
    slot = _SLOT_OF.get(channel_id)
    if slot is None:
        return "#a3a29a" if dark else "#6b6a66"
    return (_SLOTS_DARK if dark else _SLOTS_LIGHT)[slot]


def dash_for(channel_id: int) -> str:
    """Secondary encoding, so identity never rests on hue alone."""
    return "dashed" if channel_id in _MUTED_OF else "solid"


_COLORS = {ch: color_for(int(ch)) for ch in Ch}


@dataclass(slots=True)
class ChannelSpec:
    id: int
    key: str
    name: str
    unit: str
    kind: str
    color: str = "#6b6a66"
    color_dark: str = "#a3a29a"
    #: "solid" or "dashed" — secondary encoding so identity is never colour-alone.
    dash: str = "solid"
    decimals: int = 3
    #: Whether this channel is a temperature that participates in the "all temps" view.
    is_temperature: bool = False
    #: Hidden from the default live view (raw/diagnostic channels).
    secondary: bool = False
    #: Which calibration model this channel accepts, or None if uncalibratable.
    cal_model: str | None = None
    #: Probe channels are what the user actually measures with.
    probe: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.name} [{self.unit}]"


def _spec(
    ch: Ch,
    key: str,
    name: str,
    unit: str,
    kind: str,
    *,
    decimals: int = 3,
    temp: bool = False,
    secondary: bool = False,
    cal: str | None = None,
    probe: bool = False,
) -> ChannelSpec:
    return ChannelSpec(
        id=int(ch),
        key=key,
        name=name,
        unit=unit,
        kind=kind,
        color=color_for(int(ch), dark=False),
        color_dark=color_for(int(ch), dark=True),
        dash=dash_for(int(ch)),
        decimals=decimals,
        is_temperature=temp,
        secondary=secondary,
        cal_model=cal,
        probe=probe,
    )


DEFAULT_CHANNELS: tuple[ChannelSpec, ...] = (
    _spec(Ch.PT1000, "pt1000", "PT1000", "degC", Kind.RTD, temp=True, cal="cvd", probe=True),
    _spec(Ch.TYPEK, "typek", "Type K", "degC", Kind.TC, decimals=2, temp=True,
          cal="nist_typek", probe=True),
    _spec(Ch.TYPEK_CJ, "typek_cj", "Type K cold junction", "degC", Kind.CJ, decimals=2,
          temp=True, secondary=True, cal="linear"),
    _spec(Ch.NTC_EXT1, "ntc_ext1", "NTC CN1", "degC", Kind.NTC, decimals=2, temp=True,
          cal="steinhart", probe=True),
    _spec(Ch.NTC_EXT2, "ntc_ext2", "NTC CN2", "degC", Kind.NTC, decimals=2, temp=True,
          cal="steinhart", probe=True),
    _spec(Ch.NTC_EXT3, "ntc_ext3", "NTC CN3", "degC", Kind.NTC, decimals=2, temp=True,
          cal="steinhart", probe=True),
    _spec(Ch.NTC_BRD_RTD, "ntc_brd_rtd", "Board: RTD area", "degC", Kind.NTC, decimals=2,
          temp=True, cal="steinhart"),
    _spec(Ch.NTC_BRD_TC, "ntc_brd_tc", "Board: TC area", "degC", Kind.NTC, decimals=2,
          temp=True, cal="steinhart"),
    _spec(Ch.NTC_BRD_CHG, "ntc_brd_chg", "Board: charger", "degC", Kind.NTC, decimals=2,
          temp=True, cal="steinhart"),
    _spec(Ch.V_BAT, "v_bat", "Battery", "V", Kind.VOLT, cal="linear"),
    _spec(Ch.V_CC, "v_cc", "VCC", "V", Kind.VOLT, cal="linear"),
    _spec(Ch.AHT20_T, "aht20_t", "AHT20 temp", "degC", Kind.HYGRO, decimals=2, temp=True,
          cal="linear"),
    _spec(Ch.AHT20_RH, "aht20_rh", "AHT20 RH", "%RH", Kind.HYGRO, decimals=2, cal="linear"),
    _spec(Ch.PT1000_R, "pt1000_r", "PT1000 raw", "ohm", Kind.RAW, decimals=4, secondary=True),
    _spec(Ch.TYPEK_UV, "typek_uv", "Type K raw", "uV", Kind.RAW, decimals=2, secondary=True),
)

BY_ID: dict[int, ChannelSpec] = {c.id: c for c in DEFAULT_CHANNELS}
BY_KEY: dict[str, ChannelSpec] = {c.key: c for c in DEFAULT_CHANNELS}

#: Channels the user is most likely to plot first.
PROBE_CHANNELS: tuple[int, ...] = tuple(c.id for c in DEFAULT_CHANNELS if c.probe)
TEMPERATURE_CHANNELS: tuple[int, ...] = tuple(c.id for c in DEFAULT_CHANNELS if c.is_temperature)


def spec_for(channel_id: int) -> ChannelSpec:
    """Look up a channel, inventing a placeholder for ids a future firmware adds."""
    known = BY_ID.get(channel_id)
    if known is not None:
        return known
    return ChannelSpec(
        id=channel_id,
        key=f"ch{channel_id}",
        name=f"Channel {channel_id}",
        unit="",
        kind="unknown",
        color=color_for(channel_id, dark=False),
        color_dark=color_for(channel_id, dark=True),
        secondary=True,
    )


def specs_from_device_info(info: dict) -> dict[int, ChannelSpec]:
    """Build the channel table a connected device declares.

    The device is authoritative for id/key/name/unit; we keep our own colours and
    presentation hints for channels we recognise, because a firmware author should
    not have to care about UI colour choices.
    """
    out: dict[int, ChannelSpec] = {}
    for entry in info.get("channels", []) or []:
        cid = int(entry["id"])
        base = BY_ID.get(cid)
        out[cid] = ChannelSpec(
            id=cid,
            key=entry.get("key") or (base.key if base else f"ch{cid}"),
            name=entry.get("name") or (base.name if base else f"Channel {cid}"),
            unit=entry.get("unit") or (base.unit if base else ""),
            kind=entry.get("kind") or (base.kind if base else "unknown"),
            color=color_for(cid, dark=False),
            color_dark=color_for(cid, dark=True),
            dash=dash_for(cid),
            decimals=base.decimals if base else 3,
            is_temperature=(entry.get("unit") in ("degC", "degF", "K")),
            secondary=base.secondary if base else True,
            cal_model=base.cal_model if base else None,
            probe=base.probe if base else False,
        )
    return out or dict(BY_ID)
