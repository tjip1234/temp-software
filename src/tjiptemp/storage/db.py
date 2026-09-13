"""The recording database.

SQLite, with samples stored as float32 BLOBs rather than one row per reading. At
100 Hz across 15 channels a row-per-sample schema would write 5.4 million rows an
hour and spend most of its time on per-row overhead; storing the same data as
contiguous blocks makes writes cheap, keeps the file compact (about 60 bytes per
sample-row including indexes), and means reads come back as a numpy array with a
single ``frombuffer`` and no parsing at all.

The trade is that you cannot ``SELECT`` an individual sample in SQL. In practice
nothing wants to: every query is "give me this channel over this time range",
which the ``(session_id, first_seq)`` index answers by returning a handful of
blocks that are then sliced in numpy.

Each session snapshots the calibration and config that were in force, so a
recording remains interpretable years later even after the board is recalibrated.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    serial      TEXT PRIMARY KEY,
    name        TEXT,
    model       TEXT,
    hw_rev      TEXT,
    fw_ver      TEXT,
    first_seen  REAL,
    last_seen   REAL,
    info_json   TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_serial  TEXT NOT NULL REFERENCES devices(serial) ON DELETE CASCADE,
    name           TEXT NOT NULL DEFAULT '',
    notes          TEXT NOT NULL DEFAULT '',
    started_utc    REAL NOT NULL,
    ended_utc      REAL,
    rate_hz        REAL,
    channels_json  TEXT NOT NULL,
    cal_json       TEXT,
    config_json    TEXT,
    timebase_json  TEXT,
    rows           INTEGER NOT NULL DEFAULT 0,
    gaps_json      TEXT,
    state          TEXT NOT NULL DEFAULT 'recording'
);

CREATE TABLE IF NOT EXISTS blocks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    first_seq   INTEGER NOT NULL,
    n_rows      INTEGER NOT NULL,
    n_ch        INTEGER NOT NULL,
    t0_utc      REAL NOT NULL,
    dt_s        REAL NOT NULL,
    fault_mask  INTEGER NOT NULL DEFAULT 0,
    backfilled  INTEGER NOT NULL DEFAULT 0,
    data        BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_blocks_session_seq  ON blocks(session_id, first_seq);
CREATE INDEX IF NOT EXISTS idx_blocks_session_time ON blocks(session_id, t0_utc);

CREATE TABLE IF NOT EXISTS markers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    t_utc       REAL NOT NULL,
    label       TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_markers_session ON markers(session_id, t_utc);

CREATE TABLE IF NOT EXISTS cal_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_serial  TEXT NOT NULL,
    applied_utc    REAL NOT NULL,
    rev            INTEGER,
    by             TEXT,
    reference      TEXT,
    cal_json       TEXT NOT NULL,
    fit_json       TEXT
);

CREATE INDEX IF NOT EXISTS idx_cal_device ON cal_history(device_serial, applied_utc);
"""


@dataclass(slots=True)
class SessionInfo:
    id: int
    device_serial: str
    name: str
    started_utc: float
    ended_utc: float | None
    rate_hz: float
    channels: list[int]
    rows: int
    state: str
    notes: str = ""
    cal: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    timebase: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        end = self.ended_utc if self.ended_utc else time.time()
        return max(0.0, end - self.started_utc)

    @property
    def is_open(self) -> bool:
        return self.state == "recording"

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "device": self.device_serial,
            "name": self.name,
            "started_utc": self.started_utc,
            "ended_utc": self.ended_utc,
            "duration_s": round(self.duration_s, 3),
            "rate_hz": self.rate_hz,
            "channels": self.channels,
            "rows": self.rows,
            "state": self.state,
            "notes": self.notes,
        }


class Database:
    """Thread-safe SQLite wrapper. One connection per thread, created on demand."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Every connection ever handed out, so close() can actually close them.
        # Writes run on the default executor's threads, so by the end of a
        # session most connections belong to threads that are not the one
        # shutting down -- and each holds a WAL read mark until it is closed.
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()
        # executescript() commits any open transaction, and PRAGMA journal_mode
        # cannot run inside one at all, so schema setup deliberately stays outside
        # the transaction helper.
        conn = self._conn
        conn.executescript(SCHEMA)
        with self.connect() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        found = int(row[0]) if row else SCHEMA_VERSION
        if found > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} was written by a newer version of TjipTemp "
                f"(schema {found}, this build understands {SCHEMA_VERSION}). "
                "Refusing to open it rather than risk corrupting your recordings."
            )
        # Future migrations land here, stepping found -> SCHEMA_VERSION.

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
            with self._all_lock:
                self._all.append(conn)
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside one transaction, rolling back on any exception.

        Nesting is tolerated: an inner call joins the outer transaction rather
        than trying to open a second one, which SQLite does not allow.
        """
        conn = self._conn
        if conn.in_transaction:
            yield conn  # already inside a transaction; let the outermost commit
            return
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        else:
            if conn.in_transaction:
                conn.execute("COMMIT")

    def close(self) -> None:
        """Close every connection, from whichever thread opened it.

        sqlite3 objects may only be used on their creating thread, but closing
        is the one operation that is safe to do from another one, and leaving
        them open leaves the -wal and -shm files uncheckpointed on exit.
        """
        with self._all_lock:
            conns, self._all = self._all, []
        for conn in conns:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self._local.conn = None

    # ---------------------------------------------------------------- devices

    def upsert_device(self, serial: str, info: dict, name: str = "") -> None:
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO devices(serial, name, model, hw_rev, fw_ver, first_seen,
                                    last_seen, info_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(serial) DO UPDATE SET
                    name = COALESCE(NULLIF(excluded.name, ''), devices.name),
                    model = excluded.model,
                    hw_rev = excluded.hw_rev,
                    fw_ver = excluded.fw_ver,
                    last_seen = excluded.last_seen,
                    info_json = excluded.info_json
                """,
                (serial, name or info.get("model", ""), info.get("model", ""),
                 info.get("hw_rev", ""), info.get("fw_ver", ""), now, now, json.dumps(info)),
            )

    def device_info(self, serial: str) -> dict:
        """The DEVICE_INFO last stored for a board, or {} if it was never seen."""
        row = self._conn.execute(
            "SELECT info_json FROM devices WHERE serial = ?", (serial,)
        ).fetchone()
        if row is None or not row["info_json"]:
            return {}
        try:
            return json.loads(row["info_json"])
        except ValueError:
            return {}

    def list_devices(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM devices ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def rename_device(self, serial: str, name: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE devices SET name = ? WHERE serial = ?", (name, serial))

    # ---------------------------------------------------------------- sessions

    def create_session(
        self,
        device_serial: str,
        channels: list[int],
        *,
        name: str = "",
        rate_hz: float = 0.0,
        cal: dict | None = None,
        config: dict | None = None,
        notes: str = "",
    ) -> int:
        started = time.time()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO sessions(device_serial, name, notes, started_utc, rate_hz,
                                     channels_json, cal_json, config_json, state)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'recording')
                """,
                (device_serial, name or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
                 notes, started, rate_hz, json.dumps(channels),
                 json.dumps(cal or {}), json.dumps(config or {})),
            )
            return int(cursor.lastrowid)

    def close_session(self, session_id: int, *, timebase: dict | None = None,
                      gaps: list | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                   SET ended_utc = ?, state = 'complete',
                       timebase_json = COALESCE(?, timebase_json),
                       gaps_json = COALESCE(?, gaps_json)
                 WHERE id = ?
                """,
                (time.time(),
                 json.dumps(timebase) if timebase is not None else None,
                 json.dumps(gaps) if gaps is not None else None,
                 session_id),
            )

    def update_session(self, session_id: int, **fields) -> None:
        allowed = {"name", "notes", "rows", "state", "gaps_json", "timebase_json"}
        sets, values = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                values.append(value)
        if not sets:
            return
        values.append(session_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", values)

    def get_session(self, session_id: int) -> SessionInfo | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._session_from_row(row) if row else None

    def list_sessions(self, device_serial: str | None = None, limit: int = 500) -> list[SessionInfo]:
        if device_serial:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE device_serial = ? ORDER BY started_utc DESC LIMIT ?",
                (device_serial, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY started_utc DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._session_from_row(r) for r in rows]

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionInfo:
        return SessionInfo(
            id=row["id"],
            device_serial=row["device_serial"],
            name=row["name"],
            started_utc=row["started_utc"],
            ended_utc=row["ended_utc"],
            rate_hz=row["rate_hz"] or 0.0,
            channels=json.loads(row["channels_json"]),
            rows=row["rows"] or 0,
            state=row["state"],
            notes=row["notes"] or "",
            cal=json.loads(row["cal_json"] or "{}"),
            config=json.loads(row["config_json"] or "{}"),
            timebase=json.loads(row["timebase_json"] or "{}"),
        )

    def delete_session(self, session_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    # ------------------------------------------------------------------ blocks

    def append_blocks(self, session_id: int, blocks: list[dict]) -> int:
        """Insert blocks in one transaction. Each dict is the output of ``pack_block``."""
        if not blocks:
            return 0
        rows = 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO blocks(session_id, first_seq, n_rows, n_ch, t0_utc, dt_s,
                                   fault_mask, backfilled, data)
                VALUES(:session_id, :first_seq, :n_rows, :n_ch, :t0_utc, :dt_s,
                       :fault_mask, :backfilled, :data)
                """,
                [{**b, "session_id": session_id} for b in blocks],
            )
            rows = sum(b["n_rows"] for b in blocks)
            conn.execute("UPDATE sessions SET rows = rows + ? WHERE id = ?", (rows, session_id))
        return rows

    def read_session(
        self,
        session_id: int,
        *,
        t_from: float | None = None,
        t_to: float | None = None,
        max_rows: int | None = None,
    ) -> tuple[np.ndarray, np.ndarray, list[int]]:
        """Return ``(times_utc, values, channel_ids)`` for a session.

        Blocks are stored in arrival order, which for backfilled data is not
        sequence order, so the result is sorted by sequence before returning. The
        timestamps come from each block's own ``t0_utc``/``dt_s``, so a gap in the
        recording stays a gap in the timeline rather than being silently closed up.
        """
        info = self.get_session(session_id)
        if info is None:
            raise KeyError(f"no session {session_id}")
        channels = info.channels

        query = "SELECT * FROM blocks WHERE session_id = ?"
        params: list = [session_id]
        if t_from is not None:
            # A block starting before the window may still overlap it, so widen the
            # lower bound by the block's own duration.
            query += " AND (t0_utc + n_rows * dt_s) >= ?"
            params.append(t_from)
        if t_to is not None:
            query += " AND t0_utc <= ?"
            params.append(t_to)
        query += " ORDER BY first_seq ASC"

        chunks_t: list[np.ndarray] = []
        chunks_v: list[np.ndarray] = []
        total = 0
        for row in self._conn.execute(query, params):
            values = np.frombuffer(row["data"], dtype="<f4").reshape(row["n_rows"], row["n_ch"])
            times = row["t0_utc"] + np.arange(row["n_rows"], dtype=np.float64) * row["dt_s"]
            if t_from is not None or t_to is not None:
                mask = np.ones(len(times), dtype=bool)
                if t_from is not None:
                    mask &= times >= t_from
                if t_to is not None:
                    mask &= times <= t_to
                times, values = times[mask], values[mask]
            if times.size == 0:
                continue
            chunks_t.append(times)
            chunks_v.append(values)
            total += len(times)
            if max_rows is not None and total >= max_rows:
                break

        if not chunks_t:
            return (np.zeros(0), np.zeros((0, len(channels)), dtype=np.float32), channels)

        times = np.concatenate(chunks_t)
        values = np.concatenate(chunks_v)
        # Backfill can insert older sequences after newer ones; sort so exports and
        # plots are always monotonic in time.
        if not np.all(np.diff(times) >= 0):
            order = np.argsort(times, kind="stable")
            times, values = times[order], values[order]
        if max_rows is not None and len(times) > max_rows:
            times, values = times[:max_rows], values[:max_rows]
        return times, values, channels

    def session_bounds(self, session_id: int) -> tuple[float, float]:
        row = self._conn.execute(
            "SELECT MIN(t0_utc) AS lo, MAX(t0_utc + n_rows * dt_s) AS hi "
            "FROM blocks WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row or row["lo"] is None:
            return (0.0, 0.0)
        return (float(row["lo"]), float(row["hi"]))

    def count_rows(self, session_id: int) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(n_rows), 0) AS n FROM blocks WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["n"])

    # ----------------------------------------------------------------- markers

    def add_marker(self, session_id: int, t_utc: float, label: str, notes: str = "") -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO markers(session_id, t_utc, label, notes) VALUES(?, ?, ?, ?)",
                (session_id, t_utc, label, notes),
            )
            return int(cursor.lastrowid)

    def list_markers(self, session_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM markers WHERE session_id = ? ORDER BY t_utc", (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_marker(self, marker_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM markers WHERE id = ?", (marker_id,))

    # ------------------------------------------------------------ calibrations

    def record_calibration(self, device_serial: str, cal: dict, fit: dict | None = None) -> int:
        """Keep every calibration ever applied, so a recording stays interpretable.

        The board only stores the current one. Without this table, re-deriving a
        temperature from an old recording's raw channels after a recalibration
        would be guesswork.
        """
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO cal_history(device_serial, applied_utc, rev, by, reference,
                                        cal_json, fit_json)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (device_serial, time.time(), cal.get("rev"), cal.get("by", ""),
                 cal.get("reference", ""), json.dumps(cal),
                 json.dumps(fit) if fit else None),
            )
            return int(cursor.lastrowid)

    def calibration_history(self, device_serial: str, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM cal_history WHERE device_serial = ? ORDER BY applied_utc DESC LIMIT ?",
            (device_serial, limit),
        ).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            entry["cal"] = json.loads(entry.pop("cal_json"))
            entry["fit"] = json.loads(entry.pop("fit_json") or "null")
            out.append(entry)
        return out

    # ---------------------------------------------------------------- upkeep

    def stats(self) -> dict:
        conn = self._conn
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path": str(self.path),
            "size_bytes": size,
            "devices": conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0],
            "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "rows": conn.execute("SELECT COALESCE(SUM(n_rows), 0) FROM blocks").fetchone()[0],
        }

    def vacuum(self) -> None:
        self._conn.execute("VACUUM")


def pack_block(
    first_seq: int,
    values: np.ndarray,
    t0_utc: float,
    dt_s: float,
    *,
    fault_mask: int = 0,
    backfilled: bool = False,
) -> dict:
    """Prepare one block for insertion. ``values`` is ``(n_rows, n_ch)`` float32."""
    array = np.ascontiguousarray(values, dtype="<f4")
    return {
        "first_seq": int(first_seq),
        "n_rows": int(array.shape[0]),
        "n_ch": int(array.shape[1]),
        "t0_utc": float(t0_utc),
        "dt_s": float(dt_s),
        "fault_mask": int(fault_mask),
        "backfilled": 1 if backfilled else 0,
        "data": array.tobytes(),
    }
