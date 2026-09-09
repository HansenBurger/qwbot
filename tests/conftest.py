"""Shared fixtures for the QWBot test suite."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from qwbot import store
from qwbot.config import Settings

MOCK_PROD_URL = "http://mock.invalid/cgi-bin/webhook/send?key=MOCKPRODKEY0001"
MOCK_TEST_URL = "http://mock.invalid/cgi-bin/webhook/send?key=MOCKTESTKEY0001"
MOCK_CUSTOM_URL = "http://mock.invalid/cgi-bin/webhook/send?key=MOCKCUSTOMKEY01"


def make_settings(db_path: Path, **overrides: Any) -> Settings:
    """Build a `Settings` instance without touching the environment."""
    defaults: dict[str, Any] = {
        "webhook_url": MOCK_TEST_URL,
        "webhook_url_prod": MOCK_PROD_URL,
        "webhook_url_test": MOCK_TEST_URL,
        "webhook_target": "test",
        "bot_title": "贷款核算测试日报提醒",
        "system_date_format": "%Y-%m-%d",
        "timezone": "Asia/Shanghai",
        "reminder_hour": 9,
        "reminder_minute": 0,
        "progress_source_url": None,
        "batch_plan_source_url": None,
        "progress_doc_url": "https://doc.example/progress",
        "batch_register_doc_url": "https://doc.example/batch",
        "case_assignment_doc_url": "https://doc.example/cases",
        "agenda_doc_url": "https://doc.example/agenda",
        "frontend_url": "http://127.0.0.1:5000",
        "local_status_file": db_path.parent / "sample_status.json",
        "db_path": db_path,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def init_db(path: Path, settings: Settings | None = None) -> None:
    """Create/upgrade the schema the same way the app and the CLI do."""
    settings = settings or make_settings(path)
    store.init_store(
        path,
        settings.local_status_file,
        webhook_seeds=settings.webhook_seeds(),
        default_target=settings.default_seed_target(),
    )


LEGACY_DDL = [
    """
    CREATE TABLE batch_plan (
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
    """,
    """
    CREATE TABLE progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL DEFAULT '',
        owner TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        date TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE notification_tasks (
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
    """,
    """
    CREATE TABLE scheduler_times (
        reminder_id TEXT PRIMARY KEY,
        send_time TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE scheduler_skip_dates (
        skip_date TEXT PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE scheduler_force_dates (
        force_date TEXT PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE notification_date_overrides (
        notification_task_id INTEGER NOT NULL,
        target_date TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (notification_task_id, target_date)
    )
    """,
    """
    CREATE TABLE reminder_template_vars (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        var_name TEXT NOT NULL UNIQUE,
        var_label TEXT NOT NULL DEFAULT '',
        var_value TEXT NOT NULL DEFAULT '',
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE reminder_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL DEFAULT '',
        template_content TEXT NOT NULL DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """,
]


def make_legacy_db(path: Path) -> sqlite3.Connection:
    """Create a database that only knows the pre-QWB-002 schema.

    Mirrors an installation that was running the previous release so the migration
    can be exercised against realistic legacy data.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    for ddl in LEGACY_DDL:
        connection.execute(ddl)
    connection.commit()
    return connection


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "qwbot.sqlite3"


@pytest.fixture()
def settings(db_path: Path) -> Settings:
    return make_settings(db_path)


@pytest.fixture()
def initialized(db_path: Path, settings: Settings) -> Settings:
    init_db(db_path, settings)
    return settings


@pytest.fixture()
def legacy_db(db_path: Path):
    connection = make_legacy_db(db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def app_client(db_path: Path, settings: Settings):
    """Flask test client bound to an isolated database."""
    from qwbot.web import create_app

    init_db(db_path, settings)
    app = create_app(settings)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client
