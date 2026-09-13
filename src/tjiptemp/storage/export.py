"""Exporting recordings: CSV, Excel, Parquet, JSON, and report figures.

Two principles run through all of it.

**Say what the numbers are.** An exported file carries the device serial, the
calibration revision in force, the timebase fit and its uncertainty, and the units
of every column. A CSV of bare numbers is not a measurement, it is a rumour.

**Never silently interpolate.** Gaps stay gaps. A missing sample exports as an
empty cell, not as the previous value carried forward, and resampling is opt-in
and labelled.

Figures follow the project's chart rules: channels keep their fixed colours, no
two different units ever share an axis (separate stacked panels instead), a legend
is always present, and grid lines stay recessive.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ..protocol.channels import ChannelSpec, spec_lookup
from .db import Database, SessionInfo

UNIT_LABELS = {
    "degC": "°C",
    "degF": "°F",
    "K": "K",
    "V": "V",
    "%RH": "%RH",
    "ohm": "Ω",
    "uV": "µV",
}

#: Units that may share a y-axis. Anything else gets its own panel.
_AXIS_GROUP = {
    "degC": "temperature", "degF": "temperature", "K": "temperature",
    "V": "voltage",
    "%RH": "humidity",
    "ohm": "resistance",
    "uV": "voltage_raw",
}


class ExportError(Exception):
    pass


@dataclass(slots=True)
class ExportOptions:
    """What to include. Defaults are the conservative, lossless choices."""

    channels: list[int] | None = None      # None = every channel in the session
    t_from: float | None = None
    t_to: float | None = None
    #: "utc" ISO-8601, "local" ISO-8601, "epoch" float seconds, "elapsed" seconds from start
    time_format: str = "utc"
    #: Resample to a fixed interval in seconds. None keeps the original samples.
    resample_s: float | None = None
    #: Include a metadata header (CSV) or sheet (Excel).
    include_metadata: bool = True
    #: Round values to each channel's meaningful decimals rather than dumping float32 noise.
    round_values: bool = True
    decimal: str = "."
    separator: str = ","


# --------------------------------------------------------------------- loading

def load_frame(
    db: Database, session_id: int, options: ExportOptions | None = None
) -> tuple[pd.DataFrame, SessionInfo, list[ChannelSpec]]:
    """Read a session into a DataFrame indexed by UTC timestamp."""
    options = options or ExportOptions()
    info = db.get_session(session_id)
    if info is None:
        raise ExportError(f"no session {session_id}")

    times, values, channel_ids = db.read_session(
        session_id, t_from=options.t_from, t_to=options.t_to
    )
    if times.size == 0:
        raise ExportError(
            f"session {session_id} ({info.name}) contains no samples in the requested range"
        )

    wanted = options.channels or channel_ids
    spec = spec_lookup(db.device_info(info.device_serial))
    specs = [spec(cid) for cid in channel_ids if cid in set(wanted)]
    columns = {}
    for spec in specs:
        col = values[:, channel_ids.index(spec.id)].astype(np.float64)
        if options.round_values:
            col = np.round(col, spec.decimals)
        columns[_column_name(spec)] = col

    frame = pd.DataFrame(columns, index=pd.to_datetime(times, unit="s", utc=True))
    frame.index.name = "timestamp_utc"

    if options.resample_s:
        # Mean within each bin, and bins with no samples stay NaN rather than being
        # filled -- resampling must not invent data across a dropout.
        rule = pd.Timedelta(seconds=options.resample_s)
        frame = frame.resample(rule).mean()

    return frame, info, specs


def _column_name(spec: ChannelSpec) -> str:
    unit = UNIT_LABELS.get(spec.unit, spec.unit)
    return f"{spec.name} [{unit}]" if unit else spec.name


def _format_index(frame: pd.DataFrame, time_format: str, start: float) -> pd.DataFrame:
    out = frame.copy()
    if time_format == "epoch":
        out.index = out.index.astype("int64") / 1e9
        out.index.name = "timestamp_epoch_s"
    elif time_format == "elapsed":
        out.index = out.index.astype("int64") / 1e9 - start
        out.index.name = "elapsed_s"
    elif time_format == "local":
        import datetime as _dt

        out.index = out.index.tz_convert(_dt.datetime.now().astimezone().tzinfo)
        out.index.name = "timestamp_local"
    return out


def _drop_timezone(frame: pd.DataFrame) -> pd.DataFrame:
    """Excel has no concept of a timezone, so make the offset explicit in the name.

    Stripping the tzinfo silently would leave a column of timestamps whose zone is
    anybody's guess; the column header carries it instead.
    """
    if getattr(frame.index, "tz", None) is None:
        return frame
    out = frame.copy()
    label = "UTC" if str(frame.index.tz) == "UTC" else str(frame.index.tz)
    out.index = out.index.tz_localize(None)
    out.index.name = f"{frame.index.name or 'timestamp'} ({label})"
    return out


def metadata_rows(info: SessionInfo, specs: list[ChannelSpec], frame: pd.DataFrame) -> list[tuple[str, str]]:
    """Provenance, as key/value pairs. Goes at the top of a CSV or on its own sheet."""
    started = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(info.started_utc))
    ended = (
        time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(info.ended_utc))
        if info.ended_utc else "(still recording)"
    )
    cal = info.cal or {}
    tb = info.timebase or {}
    rows = [
        ("Exported by", "TjipTemp"),
        ("Exported at", time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())),
        ("Session", f"{info.id} — {info.name}"),
        ("Device serial", info.device_serial),
        ("Started", started),
        ("Ended", ended),
        ("Duration", f"{info.duration_s:.1f} s"),
        ("Sample rate", f"{info.rate_hz:g} Hz nominal"),
        ("Rows exported", str(len(frame))),
        ("Calibration revision", str(cal.get("rev", "unknown"))),
        ("Calibration date", str(cal.get("updated_utc", "unknown"))),
        ("Calibration reference", str(cal.get("reference", ""))),
    ]
    if tb:
        rows += [
            # None when the session was too short to separate drift from offset.
            ("Clock drift", "not measured" if tb.get("drift_ppm") is None
             else f"{tb['drift_ppm']:+.2f} ppm"),
            ("Timestamp uncertainty", f"±{tb.get('uncertainty_us', 0):.0f} µs"),
            ("Time sync points", str(tb.get("n_points", 0))),
        ]
    if info.notes:
        rows.append(("Notes", info.notes))
    missing = int(frame.isna().sum().sum())
    if missing:
        rows.append(("Empty cells", f"{missing} (sensor faults or gaps — not interpolated)"))
    rows.append(("Channels", ", ".join(_column_name(s) for s in specs)))
    return rows


# ------------------------------------------------------------------------ CSV

def to_csv(db: Database, session_id: int, path: str | Path,
           options: ExportOptions | None = None) -> Path:
    """Write a CSV with a commented provenance header."""
    options = options or ExportOptions()
    frame, info, specs = load_frame(db, session_id, options)
    formatted = _format_index(frame, options.time_format, info.started_utc)
    path = Path(path)

    with path.open("w", encoding="utf-8", newline="") as handle:
        if options.include_metadata:
            for key, value in metadata_rows(info, specs, frame):
                handle.write(f"# {key}: {value}\n")
            handle.write("#\n")
        formatted.to_csv(
            handle, sep=options.separator, decimal=options.decimal,
            date_format="%Y-%m-%dT%H:%M:%S.%f%z",
        )
    return path


# ---------------------------------------------------------------------- Excel

def to_excel(
    db: Database,
    session_id: int,
    path: str | Path,
    options: ExportOptions | None = None,
    *,
    with_chart: bool = True,
) -> Path:
    """Write an .xlsx with a data sheet, a metadata sheet, and a native Excel chart.

    The chart is a real Excel chart object, not a picture, so it stays live when
    someone filters or extends the data.
    """
    options = options or ExportOptions()
    frame, info, specs = load_frame(db, session_id, options)
    formatted = _drop_timezone(_format_index(frame, options.time_format, info.started_utc))
    path = Path(path)

    with pd.ExcelWriter(path, engine="xlsxwriter",
                        datetime_format="yyyy-mm-dd hh:mm:ss.000") as writer:
        formatted.to_excel(writer, sheet_name="Data", index=True)
        book = writer.book
        sheet = writer.sheets["Data"]

        header = book.add_format({"bold": True, "bg_color": "#f0efec", "border": 1,
                                  "text_wrap": True, "valign": "vcenter"})
        for col, name in enumerate([formatted.index.name or "time", *formatted.columns]):
            sheet.write(0, col, name, header)
            sheet.set_column(col, col, max(12, min(28, len(str(name)) + 2)))
        sheet.freeze_panes(1, 1)

        if options.include_metadata:
            meta = book.add_worksheet("Metadata")
            key_fmt = book.add_format({"bold": True})
            meta.set_column(0, 0, 26)
            meta.set_column(1, 1, 60)
            for row, (key, value) in enumerate(metadata_rows(info, specs, frame)):
                meta.write(row, 0, key, key_fmt)
                meta.write(row, 1, value)

        if with_chart and len(formatted) > 1:
            _add_excel_charts(book, "Data", formatted, specs, info)

    return path


def _add_excel_charts(book, sheet_name: str, frame: pd.DataFrame,
                      specs: list[ChannelSpec], info: SessionInfo) -> None:
    """One chart per unit group — never two scales on one axis."""
    from ..protocol.channels import color_for

    groups: dict[str, list[tuple[int, ChannelSpec]]] = {}
    for index, spec in enumerate(specs):
        groups.setdefault(_AXIS_GROUP.get(spec.unit, spec.unit), []).append((index, spec))

    chart_sheet = book.add_worksheet("Charts")
    n_rows = len(frame)
    row_offset = 1

    for group, members in groups.items():
        chart = book.add_chart({"type": "line"})
        for index, spec in members:
            column = index + 1  # column 0 is the timestamp
            chart.add_series({
                "name": [sheet_name, 0, column],
                "categories": [sheet_name, 1, 0, n_rows, 0],
                "values": [sheet_name, 1, column, n_rows, column],
                "line": {"width": 1.5, "color": color_for(spec.id)},
                "marker": {"type": "none"},
            })
        unit = UNIT_LABELS.get(members[0][1].unit, members[0][1].unit)
        chart.set_title({"name": f"{info.name} — {group} [{unit}]"})
        chart.set_x_axis({"name": "Time", "num_font": {"rotation": -45},
                          "major_gridlines": {"visible": False}})
        chart.set_y_axis({"name": unit,
                          "major_gridlines": {"visible": True,
                                              "line": {"color": "#e5e4e0", "width": 0.75}}})
        chart.set_legend({"position": "bottom"})
        chart.set_size({"width": 900, "height": 420})
        chart_sheet.insert_chart(row_offset, 1, chart)
        row_offset += 23


# --------------------------------------------------------------- other formats

def to_parquet(db: Database, session_id: int, path: str | Path,
               options: ExportOptions | None = None) -> Path:
    """Columnar export for long recordings. Needs pyarrow or fastparquet."""
    frame, info, _ = load_frame(db, session_id, options or ExportOptions())
    path = Path(path)
    try:
        frame.to_parquet(path)
    except ImportError as exc:
        raise ExportError(
            "Parquet export needs pyarrow: pip install pyarrow"
        ) from exc
    return path


def to_json(db: Database, session_id: int, path: str | Path,
            options: ExportOptions | None = None) -> Path:
    """Self-describing JSON: metadata, channel descriptors, then columnar data."""
    options = options or ExportOptions()
    frame, info, specs = load_frame(db, session_id, options)
    path = Path(path)
    document = {
        "format": "tjiptemp-export/1",
        "session": info.to_json(),
        "calibration": info.cal,
        "timebase": info.timebase,
        "channels": [
            {"id": s.id, "key": s.key, "name": s.name, "unit": s.unit, "kind": s.kind}
            for s in specs
        ],
        "time_utc": (frame.index.astype("int64") / 1e9).tolist(),
        "data": {
            spec.key: [None if np.isnan(v) else float(v)
                       for v in frame[_column_name(spec)].to_numpy()]
            for spec in specs
        },
    }
    path.write_text(json.dumps(document, indent=1), encoding="utf-8")
    return path


# --------------------------------------------------------------------- figures

def _style():
    """Apply the project's chart styling to matplotlib. Idempotent."""
    import matplotlib as mpl

    mpl.rcParams.update({
        "figure.facecolor": "#fcfcfb",
        "axes.facecolor": "#fcfcfb",
        "axes.edgecolor": "#c9c8c2",
        "axes.labelcolor": "#52514e",
        "axes.titlesize": 12,
        "axes.titleweight": "600",
        "axes.titlecolor": "#0b0b0b",
        "axes.grid": True,
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "grid.color": "#e5e4e0",
        "grid.linewidth": 0.7,
        "text.color": "#0b0b0b",
        "xtick.color": "#52514e",
        "ytick.color": "#52514e",
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
        "legend.frameon": False,
        "legend.fontsize": 9,
        "lines.linewidth": 1.6,
        "lines.solid_capstyle": "round",
        "figure.dpi": 110,
        "savefig.bbox": "tight",
    })


def figure(
    db: Database,
    session_id: int,
    options: ExportOptions | None = None,
    *,
    title: str | None = None,
    max_points: int = 4000,
):
    """Build a publication-quality matplotlib figure of a session.

    One panel per unit group, stacked and sharing the time axis. Two different
    units never share a y-scale: a dual-axis chart makes any two series look
    correlated by the arbitrary choice of scaling, which for measurement data is
    actively misleading.
    """
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    from ..protocol.channels import color_for, dash_for

    _style()
    options = options or ExportOptions()
    frame, info, specs = load_frame(db, session_id, options)

    # Min/max decimation would be ideal here, but a static report figure is
    # rendered once and read carefully, so simple binned mean plus envelope keeps
    # both the trend and the spread honest.
    if len(frame) > max_points:
        factor = int(np.ceil(len(frame) / max_points))
        binned = frame.iloc[: len(frame) // factor * factor]
        grouped = binned.groupby(np.arange(len(binned)) // factor)
        plotted = grouped.mean()
        plotted.index = binned.index[::factor][: len(plotted)]
        envelope = (grouped.min(), grouped.max())
    else:
        plotted = frame
        envelope = None

    groups: dict[str, list[ChannelSpec]] = {}
    for spec in specs:
        groups.setdefault(_AXIS_GROUP.get(spec.unit, spec.unit), []).append(spec)

    fig, axes = plt.subplots(
        len(groups), 1, figsize=(11, 3.1 * len(groups) + 1.0),
        sharex=True, squeeze=False,
    )
    axes = axes.ravel()

    for ax, (_group, members) in zip(axes, groups.items(), strict=False):
        for spec in members:
            column = _column_name(spec)
            series = plotted[column]
            ax.plot(
                plotted.index, series.to_numpy(),
                color=color_for(spec.id),
                linestyle="--" if dash_for(spec.id) == "dashed" else "-",
                label=spec.name,
                linewidth=1.3 if dash_for(spec.id) == "dashed" else 1.6,
            )
            if envelope is not None:
                lo = envelope[0][column].to_numpy()
                hi = envelope[1][column].to_numpy()
                ax.fill_between(plotted.index, lo[: len(plotted)], hi[: len(plotted)],
                                color=color_for(spec.id), alpha=0.15, linewidth=0)
        unit = UNIT_LABELS.get(members[0].unit, members[0].unit)
        ax.set_ylabel(unit)
        ax.legend(loc="upper left", ncol=min(4, len(members)))

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    axes[-1].set_xlabel(
        time.strftime("Time (UTC) — %Y-%m-%d", time.gmtime(info.started_utc))
    )

    heading = title or f"{info.name}  ·  {info.device_serial}"
    subtitle = f"{len(frame):,} samples · {info.duration_s:.0f} s"
    if info.cal:
        subtitle += f" · calibration rev {info.cal.get('rev', '?')}"
    if envelope is not None:
        subtitle += " · shaded band shows min/max within each plotted bin"
    fig.suptitle(heading, x=0.01, ha="left", fontsize=13, fontweight="600")
    fig.text(0.01, 0.955, subtitle, ha="left", fontsize=9, color="#52514e")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    return fig


def to_figure_file(
    db: Database, session_id: int, path: str | Path,
    options: ExportOptions | None = None, *, dpi: int = 200,
) -> Path:
    """Render a session to PNG, SVG or PDF, chosen by the file extension."""
    import matplotlib.pyplot as plt

    fig = figure(db, session_id, options)
    path = Path(path)
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    return path


def summary_table(db: Database, session_id: int,
                  options: ExportOptions | None = None) -> pd.DataFrame:
    """Per-channel statistics — the table that accompanies every figure.

    A table view is not decoration: it is what makes the figure accessible to
    someone who cannot distinguish two of the trace colours.
    """
    frame, _info, specs = load_frame(db, session_id, options or ExportOptions())
    rows = []
    for spec in specs:
        column = frame[_column_name(spec)]
        valid = column.dropna()
        rows.append({
            "Channel": spec.name,
            "Unit": UNIT_LABELS.get(spec.unit, spec.unit),
            "N": int(valid.size),
            "Missing": int(column.size - valid.size),
            "Min": round(float(valid.min()), spec.decimals) if valid.size else None,
            "Mean": round(float(valid.mean()), spec.decimals) if valid.size else None,
            "Max": round(float(valid.max()), spec.decimals) if valid.size else None,
            "Std": round(float(valid.std()), spec.decimals) if valid.size > 1 else None,
            "Drift": (round(float(valid.iloc[-1] - valid.iloc[0]), spec.decimals)
                      if valid.size > 1 else None),
        })
    return pd.DataFrame(rows)


EXPORTERS = {
    "csv": to_csv,
    "xlsx": to_excel,
    "json": to_json,
    "parquet": to_parquet,
    "png": to_figure_file,
    "svg": to_figure_file,
    "pdf": to_figure_file,
}


def export(db: Database, session_id: int, path: str | Path,
           options: ExportOptions | None = None) -> Path:
    """Dispatch on the file extension."""
    path = Path(path)
    suffix = path.suffix.lower().lstrip(".")
    handler = EXPORTERS.get(suffix)
    if handler is None:
        raise ExportError(
            f"cannot export to {suffix!r}. Supported: {', '.join(sorted(EXPORTERS))}"
        )
    return handler(db, session_id, path, options)
