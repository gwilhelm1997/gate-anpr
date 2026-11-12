"""Persistent state in the app's private /data volume (included in Home Assistant backups)."""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, fields
from pathlib import Path

from .plates import is_valid_nz, normalise


@dataclass
class Settings:
    """Runtime settings exposed as native Home Assistant entities. Safe defaults."""

    recognition_enabled: bool = True
    auto_open: bool = False  # stays off until the owner has finished testing
    min_confidence: int = 90  # percent
    min_consistent: int = 2
    cooldown: int = 60  # seconds
    min_plate_width: int = 60  # pixels in the full-resolution frame
    region_left: int = 0  # percent of frame
    region_top: int = 0
    region_right: int = 100
    region_bottom: int = 100

    LIMITS = {
        "min_confidence": (50, 99),
        "min_consistent": (2, 5),
        "cooldown": (10, 900),
        "min_plate_width": (20, 800),
        "region_left": (0, 100),
        "region_top": (0, 100),
        "region_right": (0, 100),
        "region_bottom": (0, 100),
    }

    @classmethod
    def coerce(cls, key: str, value):
        """Validate a value from MQTT or the web UI. Raises ValueError when out of range."""
        names = {f.name: f.type for f in fields(cls)}
        if key not in names:
            raise ValueError(f"unknown setting {key}")
        if names[key] in (bool, "bool"):
            if isinstance(value, str):
                if value.upper() not in ("ON", "OFF", "TRUE", "FALSE"):
                    raise ValueError(f"{key} must be ON or OFF")
                return value.upper() in ("ON", "TRUE")
            return bool(value)
        number = int(round(float(value)))
        low, high = cls.LIMITS[key]
        if not low <= number <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        return number


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.images = self.data_dir / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.data_dir / "gate_anpr.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS plates (
                    plate TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, plate TEXT,
                    result TEXT NOT NULL, reason TEXT NOT NULL, granted INTEGER NOT NULL,
                    confidence REAL, detail TEXT NOT NULL, image TEXT, plate_image TEXT);
                CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
                """
            )

    # settings -------------------------------------------------------------
    def load_settings(self) -> Settings:
        settings = Settings()
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM settings").fetchall()
        for row in rows:
            try:
                setattr(settings, row["key"], Settings.coerce(row["key"], json.loads(row["value"])))
            except ValueError:
                pass  # unknown or invalid stored value: keep the safe default
        return settings

    def save_setting(self, key: str, value) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    # approved registrations ----------------------------------------------
    def plates(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM plates ORDER BY plate")]

    def is_approved(self, plate: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM plates WHERE plate = ? AND enabled = 1", (plate,)).fetchone()
        return dict(row) if row else None

    def upsert_plate(self, plate: str, label: str = "", enabled: bool = True) -> str:
        plate = normalise(plate)
        if not is_valid_nz(plate):
            raise ValueError("registration must be 1-6 letters or digits")
        label = (label or "").strip()[:60]
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO plates (plate, label, enabled, created) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(plate) DO UPDATE SET label = excluded.label, enabled = excluded.enabled",
                (plate, label, int(enabled), time.time()),
            )
        return plate

    def delete_plate(self, plate: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM plates WHERE plate = ?", (normalise(plate),)).rowcount > 0

    def approved_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM plates WHERE enabled = 1").fetchone()[0]

    # access history -------------------------------------------------------
    def add_event(self, event: dict) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO events (ts, plate, result, reason, granted, confidence, detail, image, plate_image) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event["ts"], event.get("plate"), event["result"], event["reason"], int(event["granted"]),
                 event.get("confidence"), json.dumps(event.get("detail", {})), event.get("image"),
                 event.get("plate_image")),
            )
            return cur.lastrowid

    def set_event_images(self, event_id: int, image: str | None, plate_image: str | None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE events SET image = ?, plate_image = ? WHERE id = ?",
                             (image, plate_image, event_id))

    def events(self, limit: int = 50, granted: bool | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events", []
        if granted is not None:
            sql += " WHERE granted = ?"
            args.append(int(granted))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    def prune(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        with self._lock, self._db:
            old = self._db.execute(
                "SELECT image, plate_image FROM events WHERE ts < ?", (cutoff,)).fetchall()
            self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        for row in old:
            for name in (row["image"], row["plate_image"]):
                if name:
                    (self.images / name).unlink(missing_ok=True)
        return len(old)

    def close(self) -> None:
        self._db.close()

