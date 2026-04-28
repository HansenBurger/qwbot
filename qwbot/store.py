from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

COLLECTIONS = {"batch_plan", "progress"}
BATCH_FIELDS = [
    "content",
    "owner",
    "status",
    "category",
    "date",
    "natural_date",
    "current_accounting_date",
    "next_accounting_date",
    "holiday_flag",
    "description",
    "requester",
    "execution_status",
    "block_reason",
    "batch_start_time",
    "archived",
    "archived_on",
    "completed_on",
    "started_on",
    "started_at",
    "completed_at",
    "execution_seconds",
]
PROGRESS_FIELDS = ["content", "owner", "status", "category", "date"]


def init_store(db_path: Path, migration_path: Path | None = None) -> None:
    _ensure_schema(db_path)
    if migration_path and migration_path.exists() and _is_empty(db_path):
        with migration_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        for item in _normalize_collection(payload.get("batch_plan")):
            add_item(db_path, "batch_plan", item)
        for item in _normalize_collection(payload.get("progress")):
            add_item(db_path, "progress", item)


def load_status_file(path: Path) -> dict[str, list[dict[str, Any]]]:
    _ensure_schema(path)
    return {
        "batch_plan": _load_batch_plan(path),
        "progress": _load_progress(path),
    }


def save_status_file(path: Path, payload: dict[str, list[dict[str, Any]]]) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        connection.execute("DELETE FROM block_events")
        connection.execute("DELETE FROM batch_plan")
        connection.execute("DELETE FROM progress")
        for item in payload.get("batch_plan", []):
            _insert_batch(connection, _clean_item(item))
        for item in payload.get("progress", []):
            _insert_progress(connection, _clean_item(item))


def add_item(path: Path, collection: str, item: dict[str, str]) -> int:
    _ensure_collection(collection)
    _ensure_schema(path)
    with _connect(path) as connection:
        if collection == "batch_plan":
            return _insert_batch(connection, _clean_item(item))
        return _insert_progress(connection, _clean_item(item))


def update_item(path: Path, collection: str, index: int, item: dict[str, str]) -> None:
    _ensure_collection(collection)
    _ensure_schema(path)
    existing_item = get_item(path, collection, index)
    updated_item = _clean_item(item)
    for key in [
        "archived",
        "archived_on",
        "completed_on",
        "started_on",
        "started_at",
        "completed_at",
        "execution_seconds",
    ]:
        if key not in item and existing_item.get(key):
            updated_item[key] = existing_item[key]
    with _connect(path) as connection:
        if collection == "batch_plan":
            _update_batch(connection, index, updated_item)
        else:
            _update_progress(connection, index, updated_item)


def delete_item(path: Path, collection: str, index: int) -> None:
    _ensure_collection(collection)
    _ensure_schema(path)
    table = "batch_plan" if collection == "batch_plan" else "progress"
    with _connect(path) as connection:
        cursor = connection.execute(f"DELETE FROM {table} WHERE id = ?", (index,))
        if cursor.rowcount == 0:
            raise IndexError(f"Item index out of range: {index}")


def get_item(path: Path, collection: str, index: int) -> dict[str, Any]:
    _ensure_collection(collection)
    payload = load_status_file(path)
    for item in payload[collection]:
        if int(item["id"]) == index:
            return item
    raise IndexError(f"Item index out of range: {index}")


def add_block_event(path: Path, batch_plan_id: int, reason: str, started_at: str | None = None) -> int:
    _ensure_schema(path)
    started_at = started_at or _now_iso()
    with _connect(path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO block_events (batch_plan_id, reason, started_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (batch_plan_id, reason, started_at, _now_iso()),
        )
        return int(cursor.lastrowid)


def update_open_block_reason(path: Path, batch_plan_id: int, reason: str) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        connection.execute(
            """
            UPDATE block_events
            SET reason = ?
            WHERE batch_plan_id = ? AND ended_at = ''
            """,
            (reason, batch_plan_id),
        )


def close_open_block(path: Path, batch_plan_id: int, ended_at: str | None = None) -> None:
    _ensure_schema(path)
    ended_at = ended_at or _now_iso()
    with _connect(path) as connection:
        rows = connection.execute(
            """
            SELECT id, started_at
            FROM block_events
            WHERE batch_plan_id = ? AND ended_at = ''
            """,
            (batch_plan_id,),
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                UPDATE block_events
                SET ended_at = ?, duration_seconds = ?
                WHERE id = ?
                """,
                (ended_at, _duration_seconds(row["started_at"], ended_at), row["id"]),
            )


def has_open_block(path: Path, batch_plan_id: int) -> bool:
    _ensure_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM block_events
            WHERE batch_plan_id = ? AND ended_at = ''
            LIMIT 1
            """,
            (batch_plan_id,),
        ).fetchone()
    return row is not None


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _ensure_schema(path: Path) -> None:
    with _connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS batch_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL DEFAULT '',
                natural_date TEXT NOT NULL DEFAULT '',
                current_accounting_date TEXT NOT NULL DEFAULT '',
                next_accounting_date TEXT NOT NULL DEFAULT '',
                holiday_flag TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                requester TEXT NOT NULL DEFAULT '',
                execution_status TEXT NOT NULL DEFAULT '',
                block_reason TEXT NOT NULL DEFAULT '',
                batch_start_time TEXT NOT NULL DEFAULT '',
                archived TEXT NOT NULL DEFAULT '',
                archived_on TEXT NOT NULL DEFAULT '',
                completed_on TEXT NOT NULL DEFAULT '',
                started_on TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                execution_seconds INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS block_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_plan_id INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                ended_at TEXT NOT NULL DEFAULT '',
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (batch_plan_id) REFERENCES batch_plan(id) ON DELETE CASCADE
            )
            """
        )


def _is_empty(path: Path) -> bool:
    with _connect(path) as connection:
        batch_count = connection.execute("SELECT COUNT(*) FROM batch_plan").fetchone()[0]
        progress_count = connection.execute("SELECT COUNT(*) FROM progress").fetchone()[0]
    return batch_count == 0 and progress_count == 0


def _load_batch_plan(path: Path) -> list[dict[str, Any]]:
    with _connect(path) as connection:
        rows = connection.execute("SELECT * FROM batch_plan ORDER BY id").fetchall()
        events_by_batch = _load_block_events(connection)
    items = []
    for row in rows:
        item = {field: _stringify(row[field]) for field in BATCH_FIELDS}
        item["id"] = str(row["id"])
        events = events_by_batch.get(row["id"], [])
        item["block_events"] = events
        item["active_block_event"] = next((event for event in events if not event["ended_at"]), None)
        item["block_total_seconds"] = sum(int(event["duration_seconds"] or 0) for event in events)
        items.append(item)
    return items


def _load_progress(path: Path) -> list[dict[str, Any]]:
    with _connect(path) as connection:
        rows = connection.execute("SELECT * FROM progress ORDER BY id").fetchall()
    return [{**{field: _stringify(row[field]) for field in PROGRESS_FIELDS}, "id": str(row["id"])} for row in rows]


def _load_block_events(connection: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    rows = connection.execute(
        """
        SELECT *
        FROM block_events
        ORDER BY started_at, id
        """
    ).fetchall()
    events_by_batch: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        event = {
            "id": str(row["id"]),
            "batch_plan_id": str(row["batch_plan_id"]),
            "reason": _stringify(row["reason"]),
            "started_at": _stringify(row["started_at"]),
            "ended_at": _stringify(row["ended_at"]),
            "duration_seconds": int(row["duration_seconds"] or 0),
        }
        events_by_batch.setdefault(int(row["batch_plan_id"]), []).append(event)
    return events_by_batch


def _insert_batch(connection: sqlite3.Connection, item: dict[str, str]) -> int:
    values = {field: _coerce_field(item, field) for field in BATCH_FIELDS}
    now = _now_iso()
    values["created_at"] = now
    values["updated_at"] = now
    fields = [*BATCH_FIELDS, "created_at", "updated_at"]
    placeholders = ", ".join("?" for _ in fields)
    cursor = connection.execute(
        f"INSERT INTO batch_plan ({', '.join(fields)}) VALUES ({placeholders})",
        [values[field] for field in fields],
    )
    return int(cursor.lastrowid)


def _update_batch(connection: sqlite3.Connection, index: int, item: dict[str, str]) -> None:
    values = {field: _coerce_field(item, field) for field in BATCH_FIELDS}
    values["updated_at"] = _now_iso()
    assignments = ", ".join(f"{field} = ?" for field in [*BATCH_FIELDS, "updated_at"])
    cursor = connection.execute(
        f"UPDATE batch_plan SET {assignments} WHERE id = ?",
        [values[field] for field in [*BATCH_FIELDS, "updated_at"]] + [index],
    )
    if cursor.rowcount == 0:
        raise IndexError(f"Item index out of range: {index}")


def _insert_progress(connection: sqlite3.Connection, item: dict[str, str]) -> int:
    values = {field: _coerce_field(item, field) for field in PROGRESS_FIELDS}
    now = _now_iso()
    fields = [*PROGRESS_FIELDS, "created_at", "updated_at"]
    cursor = connection.execute(
        f"INSERT INTO progress ({', '.join(fields)}) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [values[field] for field in PROGRESS_FIELDS] + [now, now],
    )
    return int(cursor.lastrowid)


def _update_progress(connection: sqlite3.Connection, index: int, item: dict[str, str]) -> None:
    values = {field: _coerce_field(item, field) for field in PROGRESS_FIELDS}
    values["updated_at"] = _now_iso()
    cursor = connection.execute(
        """
        UPDATE progress
        SET content = ?, owner = ?, status = ?, category = ?, date = ?, updated_at = ?
        WHERE id = ?
        """,
        [values[field] for field in PROGRESS_FIELDS] + [values["updated_at"], index],
    )
    if cursor.rowcount == 0:
        raise IndexError(f"Item index out of range: {index}")


def _coerce_field(item: dict[str, Any], field: str) -> str | int:
    if field == "execution_seconds":
        try:
            return int(item.get(field) or 0)
        except (TypeError, ValueError):
            return 0
    return str(item.get(field) or "").strip()


def _stringify(value: Any) -> str:
    return "" if value is None else str(value)


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _duration_seconds(started_at: str, ended_at: str) -> int:
    try:
        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at)
    except ValueError:
        return 0
    return max(0, int((ended - started).total_seconds()))


def _normalize_collection(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [_clean_item(item) for item in value if _clean_item(item).get("content")]


def _clean_item(item: Any) -> dict[str, str]:
    if isinstance(item, str):
        return _empty_item(content=item.strip())
    if not isinstance(item, dict):
        return _empty_item(content=str(item))

    natural_date = _value(item, "natural_date", "自然日历", "date", "日期")
    description = _value(item, "description", "说明", "content", "内容", "事项")
    requester = _value(item, "requester", "提出人", "owner", "负责人")
    execution_status = _value(item, "execution_status", "执行状态", "status", "状态")
    block_reason = _value(item, "block_reason", "阻塞原因")
    batch_start_time = _value(item, "batch_start_time", "跑批启动时间")
    return {
        "content": _value(item, "content", "内容", "事项") or description,
        "owner": _value(item, "owner", "负责人") or requester,
        "status": _value(item, "status", "状态") or execution_status,
        "category": str(item.get("category") or item.get("类型") or "").strip(),
        "date": _value(item, "date", "日期") or natural_date,
        "natural_date": natural_date,
        "current_accounting_date": _value(
            item,
            "current_accounting_date",
            "系统当前会计日期",
            "系统当前\n会计日期",
        ),
        "next_accounting_date": _value(
            item,
            "next_accounting_date",
            "跑批后会计日期",
            "跑批后\n会计日期",
        ),
        "holiday_flag": _value(item, "holiday_flag", "节假日标志", "节假日标志\nY-是/N-否"),
        "description": description,
        "requester": requester,
        "execution_status": execution_status,
        "block_reason": block_reason,
        "batch_start_time": batch_start_time,
        "archived": _value(item, "archived"),
        "archived_on": _value(item, "archived_on"),
        "completed_on": _value(item, "completed_on"),
        "started_on": _value(item, "started_on"),
        "started_at": _value(item, "started_at"),
        "completed_at": _value(item, "completed_at"),
        "execution_seconds": _value(item, "execution_seconds"),
    }


def _empty_item(content: str = "") -> dict[str, str]:
    return {
        "content": content,
        "owner": "",
        "status": "",
        "category": "",
        "date": "",
        "natural_date": "",
        "current_accounting_date": "",
        "next_accounting_date": "",
        "holiday_flag": "",
        "description": content,
        "requester": "",
        "execution_status": "",
        "block_reason": "",
        "batch_start_time": "",
        "archived": "",
        "archived_on": "",
        "completed_on": "",
        "started_on": "",
        "started_at": "",
        "completed_at": "",
        "execution_seconds": "",
    }


def _value(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _ensure_collection(collection: str) -> None:
    if collection not in COLLECTIONS:
        raise ValueError(f"Unsupported collection: {collection}")
