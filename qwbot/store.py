from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

COLLECTIONS = {"batch_plan", "progress", "notification_tasks"}
DEFAULT_SCHEDULER_TIMES = {
    "morning-reminder": "09:00",
    "evening-reminder": "18:00",
}
WEBHOOK_TARGETS = ("prod", "test", "custom")
WEBHOOK_TARGET_LABELS = {
    "prod": "生产",
    "test": "自测",
    "custom": "自定义",
}
DATE_RULES = ("all", "business_day", "holiday_only")
DEFAULT_WEBHOOK_NAME = "默认群机器人"
MIGRATED_REMINDER_TEMPLATE_NAME = "组内重点工作提醒"
DEFAULT_REMINDER_TEMPLATE = """## {title_date} 组内重点工作
进度统计：[点击]({progress_doc_link}) 用例分工：[点击]({case_assignment_doc_link})
跑批计划：[点击]({batch_register_doc_link}) 加班申请：[点击]({agenda_doc_link})

当前交易日：<font color="info">{current_trading_date}</font>
下一交易日：<font color="info">{next_trading_date}</font>
跑批计划修改和登记：[点击]({frontend_link})"""

REMINDER_TEMPLATE_VARS = [
    ("title_date", "日期标题，如 07月22日"),
    ("current_trading_date", "当前交易日"),
    ("next_trading_date", "下一交易日"),
]

DEFAULT_TEMPLATE_VARS = {
    "progress_doc_link": ("进度统计文档链接", "progress_doc_url"),
    "case_assignment_doc_link": ("用例分工文档链接", "case_assignment_doc_url"),
    "batch_register_doc_link": ("跑批计划文档链接", "batch_register_doc_url"),
    "agenda_doc_link": ("加班申请文档链接", "agenda_doc_url"),
    "frontend_link": ("前端登记页链接", "frontend_url"),
}
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
NOTIFICATION_FIELDS = ["title", "content", "doc_url", "send_time", "at_all", "date_rule"]


def init_store(
    db_path: Path,
    migration_path: Path | None = None,
    webhook_seeds: list[dict[str, str]] | None = None,
    default_target: str = "prod",
) -> None:
    _ensure_schema(db_path, webhook_seeds=webhook_seeds, default_target=default_target)
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
        "notification_tasks": _load_notification_tasks(path),
        "scheduler_times": _load_scheduler_times(path),
        "scheduler_skip_dates": _load_scheduler_skip_dates(path),
        "scheduler_force_dates": _load_scheduler_force_dates(path),
        "template_vars": get_template_vars(path),
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
        for item in payload.get("notification_tasks", []):
            _insert_notification_task(connection, _clean_notification_task(item))


def add_item(path: Path, collection: str, item: dict[str, str]) -> int:
    _ensure_collection(collection)
    _ensure_schema(path)
    with _connect(path) as connection:
        if collection == "batch_plan":
            return _insert_batch(connection, _clean_item(item))
        if collection == "progress":
            return _insert_progress(connection, _clean_item(item))
        return _insert_notification_task(connection, _clean_notification_task(item))


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
        elif collection == "progress":
            _update_progress(connection, index, updated_item)
        else:
            _update_notification_task(connection, index, _clean_notification_task(item))


def delete_item(path: Path, collection: str, index: int) -> None:
    _ensure_collection(collection)
    _ensure_schema(path)
    table = {
        "batch_plan": "batch_plan",
        "progress": "progress",
        "notification_tasks": "notification_tasks",
    }[collection]
    with _connect(path) as connection:
        if collection == "notification_tasks":
            connection.execute("DELETE FROM notification_date_overrides WHERE notification_task_id = ?", (index,))
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


def update_scheduler_time(path: Path, reminder_id: str, send_time: str) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        cursor = connection.execute(
            """
            UPDATE scheduler_times
            SET send_time = ?, updated_at = ?
            WHERE reminder_id = ?
            """,
            (send_time.strip(), _now_iso(), reminder_id),
        )
        if cursor.rowcount == 0:
            connection.execute(
                """
                INSERT INTO scheduler_times (reminder_id, send_time, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (reminder_id, send_time.strip(), _now_iso(), _now_iso()),
            )


def add_scheduler_time(path: Path, send_time: str) -> None:
    _ensure_schema(path)
    now = _now_iso()
    reminder_id = f"work-reminder-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    with _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO scheduler_times (reminder_id, send_time, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (reminder_id, send_time.strip(), now, now),
        )


def delete_scheduler_time(path: Path, reminder_id: str) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        cursor = connection.execute(
            "DELETE FROM scheduler_times WHERE reminder_id = ?",
            (reminder_id,),
        )
        if cursor.rowcount == 0:
            raise IndexError(f"Scheduler time not found: {reminder_id}")


def add_scheduler_skip_date(path: Path, skip_date: str) -> None:
    _ensure_schema(path)
    skip_date = skip_date.strip()
    if not skip_date:
        return
    now = _now_iso()
    with _connect(path) as connection:
        connection.execute("DELETE FROM scheduler_force_dates WHERE force_date = ?", (skip_date,))
        connection.execute(
            """
            INSERT OR IGNORE INTO scheduler_skip_dates (skip_date, created_at)
            VALUES (?, ?)
            """,
            (skip_date, now),
        )


def delete_scheduler_skip_date(path: Path, skip_date: str) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        connection.execute(
            "DELETE FROM scheduler_skip_dates WHERE skip_date = ?",
            (skip_date.strip(),),
        )


def add_scheduler_force_date(path: Path, force_date: str) -> None:
    _ensure_schema(path)
    force_date = force_date.strip()
    if not force_date:
        return
    now = _now_iso()
    with _connect(path) as connection:
        connection.execute("DELETE FROM scheduler_skip_dates WHERE skip_date = ?", (force_date,))
        connection.execute(
            """
            INSERT OR IGNORE INTO scheduler_force_dates (force_date, created_at)
            VALUES (?, ?)
            """,
            (force_date, now),
        )


def delete_scheduler_force_date(path: Path, force_date: str) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        connection.execute(
            "DELETE FROM scheduler_force_dates WHERE force_date = ?",
            (force_date.strip(),),
        )


def set_notification_date_override(path: Path, notification_task_id: int, target_date: str, mode: str) -> None:
    _ensure_schema(path)
    target_date = target_date.strip()
    if not target_date or mode not in {"skip", "force"}:
        return
    now = _now_iso()
    with _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO notification_date_overrides (notification_task_id, target_date, mode, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(notification_task_id, target_date)
            DO UPDATE SET mode = excluded.mode, created_at = excluded.created_at
            """,
            (notification_task_id, target_date, mode, now),
        )


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    # PRAGMA is connection scoped: enable it here so ON DELETE CASCADE always works.
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _ensure_schema(
    path: Path,
    webhook_seeds: list[dict[str, str]] | None = None,
    default_target: str = "prod",
) -> None:
    with _connect(path) as connection:
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                doc_url TEXT NOT NULL DEFAULT '',
                send_time TEXT NOT NULL DEFAULT '',
                at_all TEXT NOT NULL DEFAULT '',
                date_rule TEXT NOT NULL DEFAULT 'business_day',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        _ensure_column(connection, "notification_tasks", "date_rule", "TEXT NOT NULL DEFAULT 'business_day'")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_times (
                reminder_id TEXT PRIMARY KEY,
                send_time TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_skip_dates (
                skip_date TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_force_dates (
                force_date TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_date_overrides (
                notification_task_id INTEGER NOT NULL,
                target_date TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (notification_task_id, target_date)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_template_vars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                var_name TEXT NOT NULL UNIQUE,
                var_label TEXT NOT NULL DEFAULT '',
                var_value TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        scheduler_seeded = connection.execute(
            "SELECT value FROM app_settings WHERE key = 'scheduler_times_seeded'"
        ).fetchone()
        scheduler_count = connection.execute("SELECT COUNT(*) FROM scheduler_times").fetchone()[0]
        if not scheduler_seeded and scheduler_count == 0:
            now = _now_iso()
            for reminder_id, send_time in DEFAULT_SCHEDULER_TIMES.items():
                connection.execute(
                    """
                    INSERT INTO scheduler_times (reminder_id, send_time, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (reminder_id, send_time, now, now),
                )
        if not scheduler_seeded:
            connection.execute(
                "INSERT INTO app_settings (key, value) VALUES ('scheduler_times_seeded', 'true')"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                template_content TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # Seed default reminder_templates if table is empty
        tpl_count = connection.execute("SELECT COUNT(*) FROM reminder_templates").fetchone()[0]
        if tpl_count == 0:
            now = _now_iso()
            # Migrate existing template from app_settings if present
            existing = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'reminder_template'"
            ).fetchone()
            tpl_content = existing["value"] if existing and existing["value"] else DEFAULT_REMINDER_TEMPLATE
            connection.execute(
                """
                INSERT INTO reminder_templates (name, template_content, is_active, sort_order, created_at, updated_at)
                VALUES (?, ?, 1, 0, ?, ?)
                """,
                ("默认模板", tpl_content, now, now),
            )
                # Seed default template vars if table is empty
        vars_count = connection.execute("SELECT COUNT(*) FROM reminder_template_vars").fetchone()[0]
        if vars_count == 0:
            now = _now_iso()
            for sort_order, (var_name, (var_label, _)) in enumerate(DEFAULT_TEMPLATE_VARS.items()):
                connection.execute(
                    """
                    INSERT INTO reminder_template_vars (var_name, var_label, var_value, sort_order, created_at, updated_at)
                    VALUES (?, ?, '', ?, ?, ?)
                    """,
                    (var_name, var_label, sort_order, now, now),
                )

        # QWB-002: webhook -> message template -> schedule model.
        _ensure_webhook_schema(connection)
        _migrate_to_webhook_model(connection, webhook_seeds, default_target)


def _is_empty(path: Path) -> bool:
    with _connect(path) as connection:
        batch_count = connection.execute("SELECT COUNT(*) FROM batch_plan").fetchone()[0]
        progress_count = connection.execute("SELECT COUNT(*) FROM progress").fetchone()[0]
    return batch_count == 0 and progress_count == 0


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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


def _load_notification_tasks(path: Path) -> list[dict[str, Any]]:
    with _connect(path) as connection:
        rows = connection.execute("SELECT * FROM notification_tasks ORDER BY send_time, id").fetchall()
        overrides_by_task = _load_notification_date_overrides(connection)
    items = []
    for row in rows:
        item = {**{field: _stringify(row[field]) for field in NOTIFICATION_FIELDS}, "id": str(row["id"])}
        item["doc_links"] = _parse_doc_links(item["doc_url"])
        overrides = overrides_by_task.get(int(row["id"]), {"skip": [], "force": []})
        item["skip_dates"] = [{"date": value} for value in overrides["skip"]]
        item["force_dates"] = [{"date": value} for value in overrides["force"]]
        items.append(item)
    return items


def _load_notification_date_overrides(connection: sqlite3.Connection) -> dict[int, dict[str, list[str]]]:
    rows = connection.execute(
        """
        SELECT notification_task_id, target_date, mode
        FROM notification_date_overrides
        ORDER BY target_date
        """
    ).fetchall()
    overrides: dict[int, dict[str, list[str]]] = {}
    for row in rows:
        task_overrides = overrides.setdefault(int(row["notification_task_id"]), {"skip": [], "force": []})
        if row["mode"] in task_overrides:
            task_overrides[row["mode"]].append(_stringify(row["target_date"]))
    return overrides


def _load_scheduler_times(path: Path) -> list[dict[str, str]]:
    with _connect(path) as connection:
        rows = connection.execute("SELECT * FROM scheduler_times ORDER BY send_time, reminder_id").fetchall()
    return [
        {
            "id": _stringify(row["reminder_id"]),
            "label": f"固定提醒 {index}",
            "send_time": _stringify(row["send_time"]),
        }
        for index, row in enumerate(rows, start=1)
    ]


def _load_scheduler_skip_dates(path: Path) -> list[dict[str, str]]:
    with _connect(path) as connection:
        rows = connection.execute("SELECT * FROM scheduler_skip_dates ORDER BY skip_date").fetchall()
    return [
        {
            "date": _stringify(row["skip_date"]),
            "created_at": _stringify(row["created_at"]),
        }
        for row in rows
    ]


def _load_scheduler_force_dates(path: Path) -> list[dict[str, str]]:
    with _connect(path) as connection:
        rows = connection.execute("SELECT * FROM scheduler_force_dates ORDER BY force_date").fetchall()
    return [
        {
            "date": _stringify(row["force_date"]),
            "created_at": _stringify(row["created_at"]),
        }
        for row in rows
    ]


def get_reminder_template(path: Path) -> str:
    _ensure_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key = 'reminder_template'"
        ).fetchone()
    if row and row["value"]:
        return row["value"]
    return DEFAULT_REMINDER_TEMPLATE


def set_reminder_template(path: Path, template: str) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO app_settings (key, value) VALUES ('reminder_template', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (template,),
        )


def get_template_vars(path: Path) -> list[dict[str, str]]:
    _ensure_schema(path)
    with _connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM reminder_template_vars ORDER BY sort_order DESC, id DESC"
        ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "var_name": _stringify(row["var_name"]),
            "var_label": _stringify(row["var_label"]),
            "var_value": _stringify(row["var_value"]),
            "sort_order": int(row["sort_order"]),
        }
        for row in rows
    ]


def add_template_var(path: Path, var_name: str, var_label: str, var_value: str) -> int:
    _ensure_schema(path)
    with _connect(path) as connection:
        max_order = connection.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM reminder_template_vars"
        ).fetchone()[0]
        now = _now_iso()
        cursor = connection.execute(
            """
            INSERT INTO reminder_template_vars (var_name, var_label, var_value, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (var_name.strip(), var_label.strip(), var_value.strip(), max_order + 1, now, now),
        )
        return int(cursor.lastrowid)


def update_template_var(path: Path, var_id: int, var_name: str, var_label: str, var_value: str) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        connection.execute(
            """
            UPDATE reminder_template_vars
            SET var_name = ?, var_label = ?, var_value = ?, updated_at = ?
            WHERE id = ?
            """,
            (var_name.strip(), var_label.strip(), var_value.strip(), _now_iso(), var_id),
        )



def check_duplicate_var_value(path: Path, var_value: str, exclude_id: int | None = None) -> str | None:
    """Check if var_value (URL) is already used by another variable.
    Returns the var_name of the duplicate, or None if not found.
    """
    if not var_value.strip():
        return None
    _ensure_schema(path)
    with _connect(path) as connection:
        if exclude_id:
            row = connection.execute(
                "SELECT var_name FROM reminder_template_vars WHERE var_value = ? AND id != ?",
                (var_value.strip(), exclude_id)
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT var_name FROM reminder_template_vars WHERE var_value = ?",
                (var_value.strip(),)
            ).fetchone()
        return row["var_name"] if row else None


def delete_template_var(path: Path, var_id: int) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        connection.execute("DELETE FROM reminder_template_vars WHERE id = ?", (var_id,))




def get_templates(path: Path) -> list[dict[str, str]]:
    _ensure_schema(path)
    with _connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM reminder_templates ORDER BY sort_order, id"
        ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "name": _stringify(row["name"]),
            "template_content": _stringify(row["template_content"]),
            "is_active": bool(row["is_active"]),
            "sort_order": int(row["sort_order"]),
        }
        for row in rows
    ]


def get_active_template(path: Path) -> str:
    _ensure_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT template_content FROM reminder_templates WHERE is_active = 1 ORDER BY id LIMIT 1"
        ).fetchone()
    if row and row["template_content"]:
        return row["template_content"]
    return get_reminder_template(path)


def add_template(path: Path, name: str, template_content: str) -> int:
    _ensure_schema(path)
    with _connect(path) as connection:
        max_order = connection.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM reminder_templates"
        ).fetchone()[0]
        now = _now_iso()
        cursor = connection.execute(
            """
            INSERT INTO reminder_templates (name, template_content, is_active, sort_order, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?, ?)
            """,
            (name.strip(), template_content.strip(), max_order + 1, now, now),
        )
        return int(cursor.lastrowid)


def update_template(path: Path, template_id: int, name: str, template_content: str) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        connection.execute(
            """
            UPDATE reminder_templates
            SET name = ?, template_content = ?, updated_at = ?
            WHERE id = ?
            """,
            (name.strip(), template_content.strip(), _now_iso(), template_id),
        )


def delete_template(path: Path, template_id: int) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT is_active FROM reminder_templates WHERE id = ?", (template_id,)
        ).fetchone()
        connection.execute("DELETE FROM reminder_templates WHERE id = ?", (template_id,))
        # If deleted template was active, activate the first remaining template
        if row and row["is_active"]:
            first = connection.execute(
                "SELECT id FROM reminder_templates ORDER BY sort_order, id LIMIT 1"
            ).fetchone()
            if first:
                connection.execute(
                    "UPDATE reminder_templates SET is_active = 1 WHERE id = ?", (first["id"],)
                )


def set_active_template(path: Path, template_id: int) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        connection.execute("UPDATE reminder_templates SET is_active = 0")
        connection.execute(
            "UPDATE reminder_templates SET is_active = 1 WHERE id = ?", (template_id,)
        )

# ---------------------------------------------------------------------------
# Webhook / message template / schedule model (QWB-002)
# ---------------------------------------------------------------------------


def _ensure_webhook_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS webhooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL DEFAULT 'custom',
            is_enabled INTEGER NOT NULL DEFAULT 1,
            is_default INTEGER NOT NULL DEFAULT 0,
            remark TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS message_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            webhook_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            at_all TEXT NOT NULL DEFAULT '',
            date_rule TEXT NOT NULL DEFAULT 'business_day',
            is_enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (webhook_id) REFERENCES webhooks(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS template_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL,
            send_time TEXT NOT NULL DEFAULT '',
            date_rule TEXT NOT NULL DEFAULT '',
            is_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (template_id) REFERENCES message_templates(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS template_date_overrides (
            template_id INTEGER NOT NULL,
            target_date TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (template_id, target_date),
            FOREIGN KEY (template_id) REFERENCES message_templates(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_templates_webhook"
        " ON message_templates(webhook_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_template_schedules_template"
        " ON template_schedules(template_id)"
    )


def _seed_webhooks(
    connection: sqlite3.Connection,
    webhook_seeds: list[dict[str, str]] | None,
    default_target: str,
) -> None:
    """Seed webhooks from `.env` the first time only.

    The seed runs only while the table is empty so a webhook deleted from the
    maintenance page is never resurrected by environment variables.
    """
    if connection.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0]:
        return
    now = _now_iso()
    seeds = [seed for seed in (webhook_seeds or []) if str(seed.get("url") or "").strip()]
    if not seeds:
        # Nothing usable in `.env`: create an editable, disabled placeholder so the
        # maintenance page has an entry instead of showing an empty board.
        seeds = [
            {
                "name": DEFAULT_WEBHOOK_NAME,
                "url": "",
                "target": "custom",
                "remark": "请在页面上填写群机器人 webhook 地址",
                "is_enabled": "0",
            }
        ]
    seen: set[str] = set()
    for order, seed in enumerate(seeds):
        url = str(seed.get("url") or "").strip()
        if url in seen:
            continue
        seen.add(url)
        connection.execute(
            """
            INSERT INTO webhooks
                (name, url, target, is_enabled, is_default, remark, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                str(seed.get("name") or "").strip() or DEFAULT_WEBHOOK_NAME,
                url,
                _clean_webhook_target(str(seed.get("target") or "custom")),
                1 if str(seed.get("is_enabled", "1")) not in {"0", "false", ""} else 0,
                str(seed.get("remark") or "").strip(),
                order,
                now,
                now,
            ),
        )
    _ensure_default_webhook(connection, default_target)
    connection.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('webhook_model_seeded', 'true')"
    )


def _ensure_default_webhook(
    connection: sqlite3.Connection,
    preferred_target: str | None = None,
) -> None:
    if connection.execute("SELECT COUNT(*) FROM webhooks WHERE is_default = 1").fetchone()[0]:
        return
    row = None
    if preferred_target:
        row = connection.execute(
            "SELECT id FROM webhooks WHERE target = ? ORDER BY sort_order, id LIMIT 1",
            (_clean_webhook_target(preferred_target),),
        ).fetchone()
    if row is None:
        row = connection.execute(
            "SELECT id FROM webhooks ORDER BY sort_order, id LIMIT 1"
        ).fetchone()
    if row is not None:
        connection.execute("UPDATE webhooks SET is_default = 1 WHERE id = ?", (row["id"],))


def _set_default_webhook(connection: sqlite3.Connection, webhook_id: int) -> None:
    connection.execute("UPDATE webhooks SET is_default = 0")
    connection.execute(
        "UPDATE webhooks SET is_default = 1, updated_at = ? WHERE id = ?",
        (_now_iso(), webhook_id),
    )


def _migrate_to_webhook_model(
    connection: sqlite3.Connection,
    webhook_seeds: list[dict[str, str]] | None,
    default_target: str,
) -> None:
    """One-shot migration from the legacy global model to webhook -> template -> schedule.

    Legacy tables are read only; nothing is deleted so the change can be rolled back
    by reverting the code.
    """
    if connection.execute(
        "SELECT value FROM app_settings WHERE key = 'webhook_model_migrated'"
    ).fetchone():
        return

    _seed_webhooks(connection, webhook_seeds, default_target)
    default_row = connection.execute(
        "SELECT id FROM webhooks WHERE is_default = 1 ORDER BY sort_order, id LIMIT 1"
    ).fetchone() or connection.execute(
        "SELECT id FROM webhooks ORDER BY sort_order, id LIMIT 1"
    ).fetchone()
    if default_row is None:
        return
    webhook_id = int(default_row["id"])
    now = _now_iso()
    order = 0

    # 1. reminder_templates -> message_templates (only the active one stays enabled).
    reminder_template_id: int | None = None
    for row in connection.execute(
        "SELECT * FROM reminder_templates ORDER BY is_active DESC, sort_order, id"
    ).fetchall():
        is_active = bool(row["is_active"])
        content = _stringify(row["template_content"]) or DEFAULT_REMINDER_TEMPLATE
        name = MIGRATED_REMINDER_TEMPLATE_NAME if is_active else (
            _stringify(row["name"]) or MIGRATED_REMINDER_TEMPLATE_NAME
        )
        cursor = connection.execute(
            """
            INSERT INTO message_templates
                (webhook_id, name, content, at_all, date_rule, is_enabled, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, '', 'business_day', ?, ?, ?, ?)
            """,
            (webhook_id, name, content, 1 if is_active else 0, order, now, now),
        )
        order += 1
        if is_active and reminder_template_id is None:
            reminder_template_id = int(cursor.lastrowid)

    if reminder_template_id is None:
        fallback = connection.execute(
            "SELECT value FROM app_settings WHERE key = 'reminder_template'"
        ).fetchone()
        content = (_stringify(fallback["value"]) if fallback else "") or DEFAULT_REMINDER_TEMPLATE
        cursor = connection.execute(
            """
            INSERT INTO message_templates
                (webhook_id, name, content, at_all, date_rule, is_enabled, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, '', 'business_day', 1, ?, ?, ?)
            """,
            (webhook_id, MIGRATED_REMINDER_TEMPLATE_NAME, content, order, now, now),
        )
        reminder_template_id = int(cursor.lastrowid)
        order += 1

    # 2. scheduler_times -> schedules of the migrated built-in reminder.
    for row in connection.execute(
        "SELECT send_time FROM scheduler_times ORDER BY send_time, reminder_id"
    ).fetchall():
        send_time = _normalize_send_time(_stringify(row["send_time"]))
        if send_time:
            _insert_schedule(connection, reminder_template_id, send_time, "", True, now)

    # 3. Global skip/force dates -> overrides of the migrated built-in reminder.
    for table, column, mode in (
        ("scheduler_skip_dates", "skip_date", "skip"),
        ("scheduler_force_dates", "force_date", "force"),
    ):
        for row in connection.execute(f"SELECT {column} AS target_date FROM {table}").fetchall():
            target_date = _stringify(row["target_date"])
            if target_date:
                _insert_date_override(connection, reminder_template_id, target_date, mode, now)

    # 4. notification_tasks -> one template (plus one schedule) each.
    for row in connection.execute(
        "SELECT * FROM notification_tasks ORDER BY send_time, id"
    ).fetchall():
        task = {field: _stringify(row[field]) for field in NOTIFICATION_FIELDS}
        task["doc_links"] = _parse_doc_links(task["doc_url"])
        template_id = _insert_template(
            connection,
            webhook_id=webhook_id,
            name=task["title"] or "自定义通知",
            content=_notification_task_to_content(task),
            at_all="true" if task["at_all"] == "true" else "",
            date_rule=_clean_date_rule(task["date_rule"]),
            is_enabled=True,
            sort_order=order,
            now=now,
        )
        order += 1
        send_time = _normalize_send_time(task["send_time"])
        if send_time:
            _insert_schedule(connection, template_id, send_time, "", True, now)
        for override in connection.execute(
            "SELECT target_date, mode FROM notification_date_overrides"
            " WHERE notification_task_id = ?",
            (int(row["id"]),),
        ).fetchall():
            target_date = _stringify(override["target_date"])
            mode = _stringify(override["mode"])
            if target_date and mode in {"skip", "force"}:
                _insert_date_override(connection, template_id, target_date, mode, now)

    connection.execute(
        "INSERT OR REPLACE INTO app_settings (key, value)"
        " VALUES ('webhook_model_migrated', 'true')"
    )


def _notification_task_to_content(task: dict[str, Any]) -> str:
    """Rebuild legacy custom-notification markdown as a template body.

    `{var_name}` references inside doc links are kept verbatim so the renderer can
    still fall back to `.env` values, matching the previous send-time behaviour.
    """
    lines = [f"【{task.get('title') or '消息通知'}】", "", task.get("content") or ""]
    doc_links = task.get("doc_links") or []
    if doc_links:
        lines.extend(["", "相关文档："])
        for link in doc_links:
            label = link.get("label") or "在线文档"
            lines.append(f"- [{label}]({link.get('url') or ''})")
    elif task.get("doc_url"):
        lines.extend(["", "相关文档：", f"- [在线文档]({task['doc_url']})"])
    return "\n".join(lines)


def _insert_template(
    connection: sqlite3.Connection,
    *,
    webhook_id: int,
    name: str,
    content: str,
    at_all: str | bool,
    date_rule: str,
    is_enabled: bool,
    sort_order: int,
    now: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO message_templates
            (webhook_id, name, content, at_all, date_rule, is_enabled, sort_order, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            webhook_id,
            name.strip(),
            content,
            _clean_at_all(at_all),
            _clean_date_rule(date_rule),
            1 if is_enabled else 0,
            sort_order,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def _insert_schedule(
    connection: sqlite3.Connection,
    template_id: int,
    send_time: str,
    date_rule: str,
    is_enabled: bool,
    now: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO template_schedules
            (template_id, send_time, date_rule, is_enabled, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            template_id,
            _normalize_send_time(send_time),
            _clean_optional_date_rule(date_rule),
            1 if is_enabled else 0,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def _insert_date_override(
    connection: sqlite3.Connection,
    template_id: int,
    target_date: str,
    mode: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO template_date_overrides (template_id, target_date, mode, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(template_id, target_date)
        DO UPDATE SET mode = excluded.mode, created_at = excluded.created_at
        """,
        (template_id, target_date.strip(), mode, now),
    )


def _clean_webhook_target(value: str) -> str:
    value = (value or "").strip().lower()
    return value if value in WEBHOOK_TARGETS else "custom"


def _clean_at_all(value: str | bool) -> str:
    """Normalize the `at_all` flag coming from forms, the DB or the API."""
    if isinstance(value, bool):
        return "true" if value else ""
    return "true" if str(value or "").strip().lower() in {"true", "1", "yes", "y", "on"} else ""


def _clean_optional_date_rule(value: str) -> str:
    value = (value or "").strip()
    return value if value in DATE_RULES else ""


def _normalize_send_time(value: str) -> str:
    parts = (value or "").strip().split(":")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        hour = max(0, min(23, int(parts[0])))
        minute = max(0, min(59, int(parts[1])))
        return f"{hour:02d}:{minute:02d}"
    return ""


def _webhook_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    target = _stringify(row["target"]) or "custom"
    return {
        "id": str(row["id"]),
        "name": _stringify(row["name"]),
        "url": _stringify(row["url"]),
        "target": target,
        "target_label": WEBHOOK_TARGET_LABELS.get(target, "自定义"),
        "is_enabled": bool(row["is_enabled"]),
        "is_default": bool(row["is_default"]),
        "remark": _stringify(row["remark"]),
        "sort_order": int(row["sort_order"] or 0),
        "created_at": _stringify(row["created_at"]),
        "updated_at": _stringify(row["updated_at"]),
    }


def _message_template_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "webhook_id": str(row["webhook_id"]),
        "name": _stringify(row["name"]),
        "content": _stringify(row["content"]),
        "at_all": _stringify(row["at_all"]) == "true",
        "date_rule": _clean_date_rule(_stringify(row["date_rule"])),
        "is_enabled": bool(row["is_enabled"]),
        "sort_order": int(row["sort_order"] or 0),
        "created_at": _stringify(row["created_at"]),
        "updated_at": _stringify(row["updated_at"]),
    }


def _schedule_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "template_id": str(row["template_id"]),
        "send_time": _stringify(row["send_time"]),
        "date_rule": _clean_optional_date_rule(_stringify(row["date_rule"])),
        "is_enabled": bool(row["is_enabled"]),
        "created_at": _stringify(row["created_at"]),
        "updated_at": _stringify(row["updated_at"]),
    }


def list_webhooks(path: Path) -> list[dict[str, Any]]:
    _ensure_schema(path)
    with _connect(path) as connection:
        rows = connection.execute("SELECT * FROM webhooks ORDER BY sort_order, id").fetchall()
    return [_webhook_row_to_dict(row) for row in rows]


def get_webhook(path: Path, webhook_id: int) -> dict[str, Any] | None:
    _ensure_schema(path)
    with _connect(path) as connection:
        row = connection.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
    return _webhook_row_to_dict(row) if row else None


def count_webhooks(path: Path) -> int:
    _ensure_schema(path)
    with _connect(path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0])


def get_default_webhook(path: Path) -> dict[str, Any] | None:
    webhooks = list_webhooks(path)
    if not webhooks:
        return None
    return next((item for item in webhooks if item["is_default"]), webhooks[0])


def resolve_webhook_url(webhook: dict[str, Any] | None, settings: Any = None) -> str:
    """Database URL wins; empty falls back to the matching `.env` webhook."""
    if not webhook:
        return ((getattr(settings, "webhook_url", "") or "") if settings else "").strip()
    url = (webhook.get("url") or "").strip()
    if url:
        return url
    if settings is None:
        return ""
    fallback = getattr(settings, "env_webhook_url", None)
    if callable(fallback):
        return (fallback(webhook.get("target") or "custom") or "").strip()
    return (getattr(settings, "webhook_url", "") or "").strip()


def resolve_default_webhook_url(path: Path, settings: Any = None) -> str:
    webhook = get_default_webhook(path)
    if webhook:
        return resolve_webhook_url(webhook, settings)
    return ((getattr(settings, "webhook_url", "") or "") if settings else "").strip()


def resolve_target_webhook(
    path: Path,
    target: str,
    settings: Any = None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve `--webhook prod|test` to a database webhook plus its URL."""
    wanted = _clean_webhook_target(target)
    webhook = next(
        (item for item in list_webhooks(path) if item["target"] == wanted),
        None,
    ) or get_default_webhook(path)
    return webhook, resolve_webhook_url(webhook, settings)


def add_webhook(
    path: Path,
    name: str,
    url: str,
    target: str = "custom",
    remark: str = "",
    is_enabled: bool = True,
    is_default: bool = False,
) -> int:
    _ensure_schema(path)
    now = _now_iso()
    with _connect(path) as connection:
        max_order = connection.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM webhooks"
        ).fetchone()[0]
        cursor = connection.execute(
            """
            INSERT INTO webhooks
                (name, url, target, is_enabled, is_default, remark, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                name.strip() or DEFAULT_WEBHOOK_NAME,
                url.strip(),
                _clean_webhook_target(target),
                1 if is_enabled else 0,
                remark.strip(),
                int(max_order) + 1,
                now,
                now,
            ),
        )
        webhook_id = int(cursor.lastrowid)
        if is_default:
            _set_default_webhook(connection, webhook_id)
        else:
            _ensure_default_webhook(connection)
        return webhook_id


def update_webhook(
    path: Path,
    webhook_id: int,
    name: str,
    url: str,
    target: str = "custom",
    remark: str = "",
    is_enabled: bool = True,
) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        cursor = connection.execute(
            """
            UPDATE webhooks
            SET name = ?, url = ?, target = ?, remark = ?, is_enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                name.strip() or DEFAULT_WEBHOOK_NAME,
                url.strip(),
                _clean_webhook_target(target),
                remark.strip(),
                1 if is_enabled else 0,
                _now_iso(),
                webhook_id,
            ),
        )
        if cursor.rowcount == 0:
            raise IndexError(f"Webhook not found: {webhook_id}")
        _ensure_default_webhook(connection)


def delete_webhook(path: Path, webhook_id: int) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT is_default FROM webhooks WHERE id = ?", (webhook_id,)
        ).fetchone()
        if row is None:
            raise IndexError(f"Webhook not found: {webhook_id}")
        if connection.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0] <= 1:
            raise ValueError("至少需要保留一个群机器人")
        connection.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        if bool(row["is_default"]):
            _ensure_default_webhook(connection)


def set_default_webhook(path: Path, webhook_id: int) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        if connection.execute(
            "SELECT id FROM webhooks WHERE id = ?", (webhook_id,)
        ).fetchone() is None:
            raise IndexError(f"Webhook not found: {webhook_id}")
        _set_default_webhook(connection, webhook_id)


def toggle_webhook(path: Path, webhook_id: int) -> bool:
    _ensure_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT is_enabled FROM webhooks WHERE id = ?", (webhook_id,)
        ).fetchone()
        if row is None:
            raise IndexError(f"Webhook not found: {webhook_id}")
        enabled = 0 if int(row["is_enabled"]) else 1
        connection.execute(
            "UPDATE webhooks SET is_enabled = ?, updated_at = ? WHERE id = ?",
            (enabled, _now_iso(), webhook_id),
        )
        return bool(enabled)


def import_env_webhooks(
    path: Path,
    webhook_seeds: list[dict[str, str]] | None,
    default_target: str = "prod",
) -> int:
    """Add `.env` webhooks that are not in the database yet. Returns rows added."""
    _ensure_schema(path)
    seeds = [seed for seed in (webhook_seeds or []) if str(seed.get("url") or "").strip()]
    if not seeds:
        return 0
    now = _now_iso()
    added = 0
    with _connect(path) as connection:
        existing_urls = {
            _stringify(row["url"])
            for row in connection.execute("SELECT url FROM webhooks").fetchall()
        }
        max_order = connection.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM webhooks"
        ).fetchone()[0]
        for seed in seeds:
            url = str(seed.get("url") or "").strip()
            if not url or url in existing_urls:
                continue
            existing_urls.add(url)
            max_order = int(max_order) + 1
            connection.execute(
                """
                INSERT INTO webhooks
                    (name, url, target, is_enabled, is_default, remark, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, 1, 0, ?, ?, ?, ?)
                """,
                (
                    str(seed.get("name") or "").strip() or DEFAULT_WEBHOOK_NAME,
                    url,
                    _clean_webhook_target(str(seed.get("target") or "custom")),
                    str(seed.get("remark") or "从 .env 导入").strip(),
                    max_order,
                    now,
                    now,
                ),
            )
            added += 1
        _ensure_default_webhook(connection, default_target)
    return added


def list_message_templates(path: Path, webhook_id: int | None = None) -> list[dict[str, Any]]:
    _ensure_schema(path)
    with _connect(path) as connection:
        if webhook_id is None:
            rows = connection.execute(
                "SELECT * FROM message_templates ORDER BY webhook_id, sort_order, id"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM message_templates WHERE webhook_id = ? ORDER BY sort_order, id",
                (webhook_id,),
            ).fetchall()
    return [_message_template_row_to_dict(row) for row in rows]


def get_message_template(path: Path, template_id: int) -> dict[str, Any] | None:
    _ensure_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM message_templates WHERE id = ?", (template_id,)
        ).fetchone()
    return _message_template_row_to_dict(row) if row else None


def add_message_template(
    path: Path,
    webhook_id: int,
    name: str,
    content: str,
    at_all: str | bool = "",
    date_rule: str = "business_day",
    is_enabled: bool = True,
    send_times: list[str] | None = None,
) -> int:
    _ensure_schema(path)
    now = _now_iso()
    with _connect(path) as connection:
        if connection.execute(
            "SELECT id FROM webhooks WHERE id = ?", (webhook_id,)
        ).fetchone() is None:
            raise IndexError(f"Webhook not found: {webhook_id}")
        max_order = connection.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM message_templates WHERE webhook_id = ?",
            (webhook_id,),
        ).fetchone()[0]
        template_id = _insert_template(
            connection,
            webhook_id=webhook_id,
            name=name,
            content=content or DEFAULT_REMINDER_TEMPLATE,
            at_all=at_all,
            date_rule=date_rule,
            is_enabled=is_enabled,
            sort_order=int(max_order) + 1,
            now=now,
        )
        for send_time in send_times or []:
            normalized = _normalize_send_time(send_time)
            if normalized:
                _insert_schedule(connection, template_id, normalized, "", True, now)
        return template_id


def update_message_template(
    path: Path,
    template_id: int,
    name: str,
    content: str,
    at_all: str | bool = "",
    date_rule: str = "business_day",
    is_enabled: bool | None = None,
) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT is_enabled FROM message_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if row is None:
            raise IndexError(f"Message template not found: {template_id}")
        enabled = bool(row["is_enabled"]) if is_enabled is None else bool(is_enabled)
        connection.execute(
            """
            UPDATE message_templates
            SET name = ?, content = ?, at_all = ?, date_rule = ?, is_enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                name.strip() or "未命名模板",
                content,
                _clean_at_all(at_all),
                _clean_date_rule(date_rule),
                1 if enabled else 0,
                _now_iso(),
                template_id,
            ),
        )


def delete_message_template(path: Path, template_id: int) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        cursor = connection.execute(
            "DELETE FROM message_templates WHERE id = ?", (template_id,)
        )
        if cursor.rowcount == 0:
            raise IndexError(f"Message template not found: {template_id}")


def toggle_message_template(path: Path, template_id: int) -> bool:
    _ensure_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT is_enabled FROM message_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if row is None:
            raise IndexError(f"Message template not found: {template_id}")
        enabled = 0 if int(row["is_enabled"]) else 1
        connection.execute(
            "UPDATE message_templates SET is_enabled = ?, updated_at = ? WHERE id = ?",
            (enabled, _now_iso(), template_id),
        )
        return bool(enabled)


def duplicate_message_template(path: Path, template_id: int) -> int:
    _ensure_schema(path)
    now = _now_iso()
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM message_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if row is None:
            raise IndexError(f"Message template not found: {template_id}")
        source = _message_template_row_to_dict(row)
        new_id = _insert_template(
            connection,
            webhook_id=int(row["webhook_id"]),
            name=f"{source['name']} 副本",
            content=source["content"],
            at_all="true" if source["at_all"] else "",
            date_rule=source["date_rule"],
            is_enabled=False,
            sort_order=int(row["sort_order"] or 0) + 1,
            now=now,
        )
        for schedule in connection.execute(
            "SELECT send_time, date_rule, is_enabled FROM template_schedules"
            " WHERE template_id = ? ORDER BY send_time, id",
            (template_id,),
        ).fetchall():
            connection.execute(
                """
                INSERT INTO template_schedules
                    (template_id, send_time, date_rule, is_enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    _stringify(schedule["send_time"]),
                    _clean_optional_date_rule(_stringify(schedule["date_rule"])),
                    0,
                    now,
                    now,
                ),
            )
        return new_id


def list_template_schedules(path: Path, template_id: int | None = None) -> list[dict[str, Any]]:
    _ensure_schema(path)
    with _connect(path) as connection:
        if template_id is None:
            rows = connection.execute(
                "SELECT * FROM template_schedules ORDER BY send_time, id"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM template_schedules WHERE template_id = ? ORDER BY send_time, id",
                (template_id,),
            ).fetchall()
    return [_schedule_row_to_dict(row) for row in rows]


def add_template_schedule(
    path: Path,
    template_id: int,
    send_time: str,
    date_rule: str = "",
    is_enabled: bool = True,
) -> int:
    _ensure_schema(path)
    normalized = _normalize_send_time(send_time)
    if not normalized:
        raise ValueError("发送时间格式必须为 HH:MM")
    now = _now_iso()
    with _connect(path) as connection:
        if connection.execute(
            "SELECT id FROM message_templates WHERE id = ?", (template_id,)
        ).fetchone() is None:
            raise IndexError(f"Message template not found: {template_id}")
        return _insert_schedule(connection, template_id, normalized, date_rule, is_enabled, now)


def update_template_schedule(
    path: Path,
    schedule_id: int,
    send_time: str,
    date_rule: str | None = None,
    is_enabled: bool | None = None,
) -> None:
    """Update one notification time.

    `date_rule=None` and `is_enabled=None` keep the stored values, so callers that
    only change the time do not silently reset the rule or the enabled flag.
    """
    _ensure_schema(path)
    normalized = _normalize_send_time(send_time)
    if not normalized:
        raise ValueError("发送时间格式必须为 HH:MM")
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT is_enabled, date_rule FROM template_schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
        if row is None:
            raise IndexError(f"Template schedule not found: {schedule_id}")
        enabled = bool(row["is_enabled"]) if is_enabled is None else bool(is_enabled)
        rule = (
            _clean_optional_date_rule(_stringify(row["date_rule"]))
            if date_rule is None
            else _clean_optional_date_rule(date_rule)
        )
        connection.execute(
            """
            UPDATE template_schedules
            SET send_time = ?, date_rule = ?, is_enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (normalized, rule, 1 if enabled else 0, _now_iso(), schedule_id),
        )


def delete_template_schedule(path: Path, schedule_id: int) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        cursor = connection.execute(
            "DELETE FROM template_schedules WHERE id = ?", (schedule_id,)
        )
        if cursor.rowcount == 0:
            raise IndexError(f"Template schedule not found: {schedule_id}")


def toggle_template_schedule(path: Path, schedule_id: int) -> bool:
    _ensure_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT is_enabled FROM template_schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
        if row is None:
            raise IndexError(f"Template schedule not found: {schedule_id}")
        enabled = 0 if int(row["is_enabled"]) else 1
        connection.execute(
            "UPDATE template_schedules SET is_enabled = ?, updated_at = ? WHERE id = ?",
            (enabled, _now_iso(), schedule_id),
        )
        return bool(enabled)


def set_template_date_override(
    path: Path,
    template_id: int,
    target_date: str,
    mode: str,
) -> None:
    _ensure_schema(path)
    target_date = target_date.strip()
    if not target_date or mode not in {"skip", "force"}:
        return
    with _connect(path) as connection:
        _insert_date_override(connection, template_id, target_date, mode, _now_iso())


def delete_template_date_override(path: Path, template_id: int, target_date: str) -> None:
    _ensure_schema(path)
    with _connect(path) as connection:
        connection.execute(
            "DELETE FROM template_date_overrides WHERE template_id = ? AND target_date = ?",
            (template_id, target_date.strip()),
        )


def _load_template_date_overrides(
    connection: sqlite3.Connection,
) -> dict[int, dict[str, list[str]]]:
    rows = connection.execute(
        "SELECT template_id, target_date, mode FROM template_date_overrides ORDER BY target_date"
    ).fetchall()
    overrides: dict[int, dict[str, list[str]]] = {}
    for row in rows:
        entry = overrides.setdefault(int(row["template_id"]), {"skip": [], "force": []})
        mode = _stringify(row["mode"])
        if mode in entry:
            entry[mode].append(_stringify(row["target_date"]))
    return overrides


def load_webhook_tree(path: Path) -> list[dict[str, Any]]:
    """Webhooks with nested templates and schedules, for the maintenance page."""
    _ensure_schema(path)
    with _connect(path) as connection:
        webhook_rows = connection.execute(
            "SELECT * FROM webhooks ORDER BY sort_order, id"
        ).fetchall()
        template_rows = connection.execute(
            "SELECT * FROM message_templates ORDER BY sort_order, id"
        ).fetchall()
        schedule_rows = connection.execute(
            "SELECT * FROM template_schedules ORDER BY send_time, id"
        ).fetchall()
        overrides = _load_template_date_overrides(connection)

    schedules_by_template: dict[int, list[dict[str, Any]]] = {}
    for row in schedule_rows:
        schedule = _schedule_row_to_dict(row)
        schedules_by_template.setdefault(int(row["template_id"]), []).append(schedule)

    templates_by_webhook: dict[int, list[dict[str, Any]]] = {}
    for row in template_rows:
        template = _message_template_row_to_dict(row)
        template_id = int(row["id"])
        template["schedules"] = schedules_by_template.get(template_id, [])
        entry = overrides.get(template_id, {"skip": [], "force": []})
        template["skip_dates"] = entry["skip"]
        template["force_dates"] = entry["force"]
        templates_by_webhook.setdefault(int(row["webhook_id"]), []).append(template)

    tree: list[dict[str, Any]] = []
    for row in webhook_rows:
        webhook = _webhook_row_to_dict(row)
        templates = templates_by_webhook.get(int(row["id"]), [])
        webhook["templates"] = templates
        webhook["template_count"] = len(templates)
        webhook["schedule_count"] = sum(len(item["schedules"]) for item in templates)
        webhook["enabled_schedule_count"] = sum(
            1
            for item in templates
            for schedule in item["schedules"]
            if schedule["is_enabled"] and item["is_enabled"]
        )
        tree.append(webhook)
    return tree


def load_dispatch_plan(path: Path) -> list[dict[str, Any]]:
    """Flatten enabled webhook x template x schedule combinations for the scheduler."""
    _ensure_schema(path)
    with _connect(path) as connection:
        rows = connection.execute(
            """
            SELECT w.id AS webhook_id, w.name AS webhook_name, w.url AS webhook_url,
                   w.target AS webhook_target,
                   t.id AS template_id, t.name AS template_name, t.content AS template_content,
                   t.at_all AS template_at_all, t.date_rule AS template_date_rule,
                   s.id AS schedule_id, s.send_time AS send_time, s.date_rule AS schedule_date_rule
            FROM template_schedules s
            JOIN message_templates t ON t.id = s.template_id
            JOIN webhooks w ON w.id = t.webhook_id
            WHERE w.is_enabled = 1 AND t.is_enabled = 1 AND s.is_enabled = 1
            ORDER BY s.send_time, w.sort_order, w.id, t.sort_order, t.id, s.id
            """
        ).fetchall()
        overrides = _load_template_date_overrides(connection)
        global_skip = {
            _stringify(row["skip_date"])
            for row in connection.execute("SELECT skip_date FROM scheduler_skip_dates").fetchall()
        }
        global_force = {
            _stringify(row["force_date"])
            for row in connection.execute("SELECT force_date FROM scheduler_force_dates").fetchall()
        }

    plan: list[dict[str, Any]] = []
    for row in rows:
        template_id = int(row["template_id"])
        entry = overrides.get(template_id, {"skip": [], "force": []})
        plan.append(
            {
                "webhook_id": int(row["webhook_id"]),
                "webhook_name": _stringify(row["webhook_name"]),
                "webhook_url": _stringify(row["webhook_url"]),
                "webhook_target": _stringify(row["webhook_target"]) or "custom",
                "template_id": template_id,
                "template_name": _stringify(row["template_name"]),
                "content": _stringify(row["template_content"]),
                "at_all": _stringify(row["template_at_all"]) == "true",
                "template_date_rule": _clean_date_rule(_stringify(row["template_date_rule"])),
                "schedule_id": int(row["schedule_id"]),
                "send_time": _stringify(row["send_time"]),
                "schedule_date_rule": _clean_optional_date_rule(
                    _stringify(row["schedule_date_rule"])
                ),
                "skip_dates": entry["skip"],
                "force_dates": entry["force"],
                "global_skip_dates": sorted(global_skip),
                "global_force_dates": sorted(global_force),
            }
        )
    return plan


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


def _insert_notification_task(connection: sqlite3.Connection, item: dict[str, str]) -> int:
    values = {field: _coerce_field(item, field) for field in NOTIFICATION_FIELDS}
    now = _now_iso()
    fields = [*NOTIFICATION_FIELDS, "created_at", "updated_at"]
    cursor = connection.execute(
        f"INSERT INTO notification_tasks ({', '.join(fields)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [values[field] for field in NOTIFICATION_FIELDS] + [now, now],
    )
    return int(cursor.lastrowid)


def _update_notification_task(connection: sqlite3.Connection, index: int, item: dict[str, str]) -> None:
    values = {field: _coerce_field(item, field) for field in NOTIFICATION_FIELDS}
    values["updated_at"] = _now_iso()
    cursor = connection.execute(
        """
        UPDATE notification_tasks
        SET title = ?, content = ?, doc_url = ?, send_time = ?, at_all = ?, date_rule = ?, updated_at = ?
        WHERE id = ?
        """,
        [values[field] for field in NOTIFICATION_FIELDS] + [values["updated_at"], index],
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


def _clean_notification_task(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        item = {}
    return {
        "title": _value(item, "title", "标题"),
        "content": _value(item, "content", "消息内容", "内容"),
        "doc_url": _clean_doc_links_value(item),
        "send_time": _value(item, "send_time", "发送时间"),
        "at_all": "true" if _value(item, "at_all", "是否at全体") in {"true", "1", "yes", "Y", "on"} else "",
        "date_rule": _clean_date_rule(_value(item, "date_rule", "日期规则")),
    }


def _clean_date_rule(value: str) -> str:
    return value if value in {"all", "business_day", "holiday_only"} else "business_day"


def _clean_doc_links_value(item: dict[str, Any]) -> str:
    doc_links = item.get("doc_links")
    if isinstance(doc_links, list):
        cleaned = [
            {
                "label": str(link.get("label") or "").strip(),
                "url": str(link.get("url") or "").strip(),
            }
            for link in doc_links
            if isinstance(link, dict) and str(link.get("url") or "").strip()
        ]
        return json.dumps(cleaned, ensure_ascii=False) if cleaned else ""
    return _value(item, "doc_url", "在线文档链接", "文档链接")


def _parse_doc_links(value: str) -> list[dict[str, str]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return [{"label": "在线文档", "url": value}]
    if not isinstance(parsed, list):
        return []
    return [
        {
            "label": str(link.get("label") or "在线文档").strip(),
            "url": str(link.get("url") or "").strip(),
        }
        for link in parsed
        if isinstance(link, dict) and str(link.get("url") or "").strip()
    ]


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
