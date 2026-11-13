"""Persistent state in the app's private /data volume (included in Home Assistant backups)."""

import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .plates import is_valid_nz, normalise

_ENTITY = {
    "camera_entity": re.compile(r"camera\.[a-z0-9_]+"),
    "live_camera_entity": re.compile(r"(camera\.[a-z0-9_]+)?"),
    "trigger_entity": re.compile(r"binary_sensor\.[a-z0-9_]+"),
    "gate_entity": re.compile(r"(cover|switch)\.[a-z0-9_]+"),
    "gate_position_entity": re.compile(r"((cover|binary_sensor)\.[a-z0-9_]+)?"),
    "stream_url": re.compile(r"(rtsps?://[^\s]{3,300})?"),
    # left,top,right,bottom in percent; several rectangles separated by ';'
    "exclusions": re.compile(r"(\d{1,3}(\.\d+)?,\d{1,3}(\.\d+)?,\d{1,3}(\.\d+)?,\d{1,3}(\.\d+)?"
                             r"(;\d{1,3}(\.\d+)?,\d{1,3}(\.\d+)?,\d{1,3}(\.\d+)?,\d{1,3}(\.\d+)?){0,9})?"),
}
CHOICES = {"direction": ("towards", "away"), "access": ("approved", "all")}
LIMITS = {
    "min_confidence": (50, 99),
    "min_consistent": (1, 5),
    "cooldown": (0, 900),
    "min_plate_width": (20, 800),
    "region_left": (0, 100),
    "region_top": (0, 100),
    "region_right": (0, 100),
    "region_bottom": (0, 100),
    "parked_minutes": (0, 120),
}
# Settings that are also native Home Assistant controls.
CONTROL_KEYS = ["enabled", "auto_open", *LIMITS]


@dataclass
class Lane:
    """One camera watching one gate. Safe defaults: automatic opening off."""

    id: int = 0
    name: str = "Gate"
    enabled: bool = True  # recognition on/off
    auto_open: bool = False  # stays off until the owner has finished testing
    access: str = "approved"  # approved: whitelist only; all: any consistent, approaching plate
    camera_entity: str = ""
    live_camera_entity: str = ""
    trigger_entity: str = ""
    gate_entity: str = ""
    gate_position_entity: str = ""
    stream_url: str = ""  # optional RTSP stream (contains the camera password: never returned or logged)
    direction: str = "towards"  # vehicles drive towards the camera (plate grows) or away from it
    min_confidence: int = 90
    min_consistent: int = 2
    cooldown: int = 60
    min_plate_width: int = 60
    region_left: int = 0
    region_top: int = 0
    region_right: int = 100
    region_bottom: int = 100
    exclusions: str = ""  # rectangles where plates are always ignored (parking spots, the road)
    parked_minutes: int = 2  # a plate seen at the same spot this long ago is parked; 0 = off

    @classmethod
    def coerce(cls, key: str, value):
        """Validate one value from MQTT or the web UI. Raises ValueError when invalid."""
        types = {f.name: f.type for f in fields(cls)}
        if key not in types or key == "id":
            raise ValueError(f"unknown setting {key}")
        if types[key] in (bool, "bool"):
            if isinstance(value, str):
                if value.upper() not in ("ON", "OFF", "TRUE", "FALSE"):
                    raise ValueError(f"{key} must be ON or OFF")
                return value.upper() in ("ON", "TRUE")
            return bool(value)
        if key in LIMITS:
            number = int(round(float(value)))
            low, high = LIMITS[key]
            if not low <= number <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            return number
        value = str(value or "").strip()
        if key == "name":
            if not 1 <= len(value) <= 40:
                raise ValueError("name must be 1-40 characters")
            return value
        if key in CHOICES:
            if value not in CHOICES[key]:
                raise ValueError(f"{key} must be one of {', '.join(CHOICES[key])}")
            return value
        if key == "exclusions":
            value = value.replace(" ", "")
            if not _ENTITY[key].fullmatch(value) or any(float(v) > 100 for v in re.split("[,;]", value) if v):
                raise ValueError("exclusion zones must look like 0,0,30,40;70,0,100,30 (percent)")
            return value
        if not _ENTITY[key].fullmatch(value):
            raise ValueError(f"{key}: {value!r} is not a valid entity for this field")
        return value

    @classmethod
    def from_dict(cls, data: dict, lane_id: int = 0) -> "Lane":
        lane = cls(id=lane_id)
        for key, value in data.items():
            if key != "id":
                setattr(lane, key, cls.coerce(key, value))
        for key in ("camera_entity", "trigger_entity", "gate_entity"):
            if not getattr(lane, key):
                raise ValueError(f"{key} is required")
        return lane

    def to_dict(self) -> dict:
        return asdict(self)

    def exclusion_boxes(self) -> list[tuple[float, float, float, float]]:
        """Exclusion rectangles as fractions of the frame."""
        out = []
        for part in filter(None, self.exclusions.split(";")):
            l, t, r, b = (float(v) / 100 for v in part.split(","))
            out.append((min(l, r), min(t, b), max(l, r), max(t, b)))
        return out

    def public(self) -> dict:
        """Safe to show: the stream URL is replaced by whether one is set."""
        data = self.to_dict()
        data["stream_configured"] = bool(data.pop("stream_url"))
        return data


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
                CREATE TABLE IF NOT EXISTS lanes (id INTEGER PRIMARY KEY AUTOINCREMENT, config TEXT NOT NULL);
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
            # 2.0: plates may be limited to some lanes ('' = all lanes); events record their lane.
            for table, column, ddl in (("plates", "lanes", "TEXT NOT NULL DEFAULT ''"),
                                       ("events", "lane_id", "INTEGER"), ("events", "lane", "TEXT")):
                cols = [r[1] for r in self._db.execute(f"PRAGMA table_info({table})")]
                if column not in cols:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # metadata -------------------------------------------------------------
    def meta(self, key: str, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = ?", (f"_{key}",)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (f"_{key}", json.dumps(value)))

    # lanes ----------------------------------------------------------------
    def lanes(self) -> list[Lane]:
        with self._lock:
            rows = self._db.execute("SELECT id, config FROM lanes ORDER BY id").fetchall()
        out = []
        for row in rows:
            lane = Lane(id=row["id"])
            for key, value in json.loads(row["config"]).items():
                try:
                    setattr(lane, key, Lane.coerce(key, value))
                except ValueError:
                    pass  # invalid stored value: keep the safe default
            out.append(lane)
        return out

    def save_lane(self, lane: Lane) -> Lane:
        config = json.dumps({k: v for k, v in lane.to_dict().items() if k != "id"})
        with self._lock, self._db:
            if lane.id:
                if not self._db.execute("UPDATE lanes SET config = ? WHERE id = ?", (config, lane.id)).rowcount:
                    raise ValueError(f"lane {lane.id} not found")
            else:
                lane.id = self._db.execute("INSERT INTO lanes (config) VALUES (?)", (config,)).lastrowid
        return lane

    def delete_lane(self, lane_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM lanes WHERE id = ?", (lane_id,)).rowcount > 0

    def migrate(self, options: dict) -> None:
        """First 2.0 start: turn the single-gate 0.1 options and settings into lane 1."""
        if self.lanes() or not options.get("camera_entity"):
            return
        old = {"enabled" if k == "recognition_enabled" else k: json.loads(v) for k, v in
               ((r["key"], r["value"]) for r in self._db.execute("SELECT key, value FROM settings"))
               if not k.startswith("_")}
        data = {k: options.get(k, "") for k in ("camera_entity", "live_camera_entity", "trigger_entity",
                                                "gate_entity", "gate_position_entity")}
        data["name"] = options.get("lane_name") or "Outside gate"
        for key, value in old.items():
            try:
                data[key] = Lane.coerce(key, value)
            except ValueError:
                pass
        self.save_lane(Lane.from_dict(data))
        with self._lock, self._db:
            self._db.execute("DELETE FROM settings WHERE key NOT LIKE '\\_%' ESCAPE '\\'")

    # approved registrations ----------------------------------------------
    @staticmethod
    def _plate_row(row) -> dict:
        d = dict(row)
        d["lanes"] = json.loads(d["lanes"]) if d.get("lanes") else []
        return d

    def plates(self) -> list[dict]:
        with self._lock:
            return [self._plate_row(r) for r in self._db.execute("SELECT * FROM plates ORDER BY plate")]

    def is_approved(self, plate: str, lane_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM plates WHERE plate = ? AND enabled = 1", (plate,)).fetchone()
        if not row:
            return None
        entry = self._plate_row(row)
        return entry if not entry["lanes"] or lane_id in entry["lanes"] else None

    def upsert_plate(self, plate: str, label: str = "", enabled: bool = True,
                     lanes: list[int] | None = None) -> str:
        plate = normalise(plate)
        if not is_valid_nz(plate):
            raise ValueError("registration must be 1-6 letters or digits")
        label = (label or "").strip()[:60]
        lane_ids = sorted({int(x) for x in (lanes or [])})
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO plates (plate, label, enabled, created, lanes) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(plate) DO UPDATE SET label = excluded.label, enabled = excluded.enabled, "
                "lanes = excluded.lanes",
                (plate, label, int(enabled), time.time(), json.dumps(lane_ids) if lane_ids else ""),
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
                "INSERT INTO events (ts, plate, result, reason, granted, confidence, detail, image, plate_image,"
                " lane_id, lane) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event["ts"], event.get("plate"), event["result"], event["reason"], int(event["granted"]),
                 event.get("confidence"), json.dumps(event.get("detail", {})), event.get("image"),
                 event.get("plate_image"), event.get("lane_id"), event.get("lane")),
            )
            return cur.lastrowid

    def set_event_images(self, event_id: int, image: str | None, plate_image: str | None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE events SET image = ?, plate_image = ? WHERE id = ?",
                             (image, plate_image, event_id))

    def events(self, limit: int = 50, granted: bool | None = None, lane_id: int | None = None) -> list[dict]:
        sql, where, args = "SELECT * FROM events", [], []
        if granted is not None:
            where.append("granted = ?")
            args.append(int(granted))
        if lane_id is not None:
            where.append("lane_id = ?")
            args.append(lane_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
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
        # Sweep files no event references (or older than retention), skipping ones still being written.
        with self._lock:
            kept = {n for r in self._db.execute("SELECT image, plate_image FROM events")
                    for n in (r["image"], r["plate_image"]) if n}
        now = time.time()
        for path in self.images.iterdir():
            age = path.stat().st_mtime
            if age < cutoff or (path.name not in kept and now - age > 3600):
                path.unlink(missing_ok=True)
        if old:
            with self._lock:
                self._db.execute("VACUUM")
        return len(old)

    def close(self) -> None:
        self._db.close()
