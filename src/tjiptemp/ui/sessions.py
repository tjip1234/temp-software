"""Browsing and exporting recordings."""

from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.ringbuffer import DecimatingView
from ..protocol.channels import spec_for
from ..storage.db import Database, SessionInfo
from ..storage.export import EXPORTERS, ExportOptions, export, summary_table
from .liveview import AXIS_GROUP, TimeAxis
from .theme import Theme
from .widgets import format_utc, humanise_duration, unit_symbol


class ExportWorker(QThread):
    """Exports off the GUI thread. A million-row xlsx is not instant."""

    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, db: Database, session_id: int, path: str,
                 options: ExportOptions, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.session_id = session_id
        self.path = path
        self.options = options

    def run(self) -> None:
        try:
            # A fresh handle: sqlite3 connections belong to the thread that made them.
            db = Database(self.db.path)
            result = export(db, self.session_id, self.path, self.options)
            db.close()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(str(result))


class ExportDialog(QDialog):
    """Choose format, range, channels and time representation."""

    def __init__(self, db: Database, info: SessionInfo, theme: Theme, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.info = info
        self.setWindowTitle(f"Export — {info.name}")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.format_box = QComboBox()
        for suffix, label in (
            ("csv", "CSV — universal, with a provenance header"),
            ("xlsx", "Excel — data, metadata and native charts"),
            ("json", "JSON — self-describing, for scripts"),
            ("parquet", "Parquet — columnar, for long recordings"),
            ("png", "PNG figure"),
            ("svg", "SVG figure — vector, for publication"),
            ("pdf", "PDF figure"),
        ):
            if suffix in EXPORTERS:
                self.format_box.addItem(label, suffix)
        form.addRow("Format", self.format_box)

        self.time_box = QComboBox()
        for label, value in (
            ("UTC timestamp", "utc"), ("Local time", "local"),
            ("Unix epoch seconds", "epoch"), ("Seconds since start", "elapsed"),
        ):
            self.time_box.addItem(label, value)
        form.addRow("Time column", self.time_box)

        self.resample_box = QCheckBox("Resample to a fixed interval")
        self.resample_input = QDoubleSpinBox()
        self.resample_input.setRange(0.001, 3600.0)
        self.resample_input.setValue(1.0)
        self.resample_input.setSuffix(" s")
        self.resample_input.setEnabled(False)
        self.resample_box.toggled.connect(self.resample_input.setEnabled)
        form.addRow(self.resample_box, self.resample_input)

        self.metadata_box = QCheckBox("Include provenance (device, calibration, clock fit)")
        self.metadata_box.setChecked(True)
        form.addRow(self.metadata_box)
        layout.addLayout(form)

        note = QLabel(
            "Gaps and faulted readings export as empty cells. They are never "
            "interpolated or forward-filled, including when resampling."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.text_muted};")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def options(self) -> ExportOptions:
        return ExportOptions(
            time_format=self.time_box.currentData(),
            resample_s=float(self.resample_input.value()) if self.resample_box.isChecked() else None,
            include_metadata=self.metadata_box.isChecked(),
        )

    def suffix(self) -> str:
        return self.format_box.currentData()


class SessionBrowser(QWidget):
    """List of recordings, with a preview plot and a statistics table."""

    def __init__(self, app, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.db = app.db
        self.theme = theme
        self._worker: ExportWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)

        header = QHBoxLayout()
        title = QLabel("Recordings")
        font = title.font()
        font.setPointSizeF(font.pointSizeF() + 3)
        font.setWeight(font.Weight.DemiBold)
        title.setFont(font)
        header.addWidget(title)
        header.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.reload)
        header.addWidget(refresh)
        self.export_button = QPushButton("Export…")
        self.export_button.setProperty("primary", True)
        self.export_button.clicked.connect(self._on_export)
        header.addWidget(self.export_button)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setProperty("destructive", True)
        self.delete_button.clicked.connect(self._on_delete)
        header.addWidget(self.delete_button)
        root.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ("Name", "Board", "Started", "Duration", "Rows", "Rate", "State")
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._on_selection)
        splitter.addWidget(self.table)

        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(0, 6, 0, 0)
        self.plot = pg.PlotWidget(axisItems={"bottom": TimeAxis(orientation="bottom")})
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.addLegend(offset=(8, 6), labelTextColor=theme.text_secondary)
        preview_layout.addWidget(self.plot, 2)

        self.stats = QTableWidget(0, 0)
        self.stats.verticalHeader().setVisible(False)
        self.stats.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stats.setMaximumHeight(190)
        preview_layout.addWidget(self.stats, 1)
        splitter.addWidget(preview)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        self._sessions: list[SessionInfo] = []
        self.reload()

    # ------------------------------------------------------------------ data

    def reload(self) -> None:
        self._sessions = self.db.list_sessions()
        self.table.setRowCount(len(self._sessions))
        for row, info in enumerate(self._sessions):
            cells = (
                info.name,
                info.device_serial[-8:],
                format_utc(info.started_utc),
                humanise_duration(info.duration_s),
                f"{info.rows:,}",
                f"{info.rate_hz:g} Hz",
                info.state,
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column >= 3:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._update_buttons()

    def selected(self) -> SessionInfo | None:
        row = self.table.currentRow()
        return self._sessions[row] if 0 <= row < len(self._sessions) else None

    def _update_buttons(self) -> None:
        info = self.selected()
        usable = info is not None and info.rows > 0
        self.export_button.setEnabled(usable)
        self.delete_button.setEnabled(info is not None)

    def _on_selection(self) -> None:
        self._update_buttons()
        info = self.selected()
        self.plot.clear()
        self.stats.clear()
        self.stats.setRowCount(0)
        self.stats.setColumnCount(0)
        if info is None or info.rows == 0:
            return

        try:
            times, values, ids = self.db.read_session(info.id, max_rows=400_000)
        except Exception as exc:
            self.plot.setTitle(f"Could not read session: {exc}")
            return
        if times.size == 0:
            return

        # Preview only the largest unit group, so the single axis stays honest.
        groups: dict[str, list[int]] = {}
        for cid in ids:
            spec = spec_for(cid)
            groups.setdefault(AXIS_GROUP.get(spec.unit, spec.unit), []).append(cid)
        group, members = max(groups.items(), key=lambda kv: len(kv[1]))
        self.plot.setTitle(f"{info.name} — {group}")
        self.plot.setLabel("left", f"{group} [{unit_symbol(spec_for(members[0]).unit)}]")

        for cid in members[:8]:
            spec = spec_for(cid)
            column = values[:, ids.index(cid)]
            t_dec, v_dec = DecimatingView.decimate(times, column, 3000)
            color = spec.color_dark if self.theme.dark else spec.color
            pen = pg.mkPen(color, width=1.5,
                           style=Qt.PenStyle.DashLine if spec.dash == "dashed"
                           else Qt.PenStyle.SolidLine)
            self.plot.plot(t_dec, v_dec, pen=pen, name=spec.name, connect="finite")

        self._fill_stats(info)

    def _fill_stats(self, info: SessionInfo) -> None:
        try:
            table = summary_table(self.db, info.id)
        except Exception:
            return
        self.stats.setColumnCount(len(table.columns))
        self.stats.setHorizontalHeaderLabels([str(c) for c in table.columns])
        self.stats.setRowCount(len(table))
        for row in range(len(table)):
            for column, name in enumerate(table.columns):
                value = table.iloc[row][name]
                text = "" if value is None or (isinstance(value, float) and np.isnan(value)) \
                    else str(value)
                item = QTableWidgetItem(text)
                if column >= 2:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.stats.setItem(row, column, item)
        self.stats.resizeColumnsToContents()

    # ---------------------------------------------------------------- actions

    def _on_export(self) -> None:
        info = self.selected()
        if info is None:
            return
        dialog = ExportDialog(self.db, info, self.theme, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        suffix = dialog.suffix()
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(info.started_utc))
        default = f"{info.name or 'recording'}-{stamp}.{suffix}".replace("/", "-")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export recording", default, f"{suffix.upper()} (*.{suffix})"
        )
        if not path:
            return

        progress = QProgressDialog("Exporting…", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(200)
        progress.show()

        worker = ExportWorker(self.db, info.id, path, dialog.options(), self)
        self._worker = worker

        def done(result: str) -> None:
            progress.close()
            QMessageBox.information(self, "Export complete", f"Written to:\n{result}")

        def failed(message: str) -> None:
            progress.close()
            QMessageBox.critical(self, "Export failed", message)

        worker.finished_ok.connect(done)
        worker.failed.connect(failed)
        worker.start()

    def _on_delete(self) -> None:
        info = self.selected()
        if info is None:
            return
        if info.is_open:
            QMessageBox.information(
                self, "Still recording",
                "Stop the recording before deleting it."
            )
            return
        answer = QMessageBox.question(
            self, "Delete recording?",
            f"Permanently delete “{info.name}” and its {info.rows:,} samples?\n\n"
            f"This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.db.delete_session(info.id)
        self.reload()

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self._on_selection()
