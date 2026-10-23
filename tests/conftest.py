"""Shared fixtures for the QWBot test suite."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from qwbot import store
from qwbot.config import Settings

# `mock.invalid` is the shared stand-in host for these suites. SEC-03 keeps it out of the
# production default whitelist, so tests opt in explicitly through this variable.
MOCK_WEBHOOK_HOST = "mock.invalid"
MOCK_PROD_URL = f"http://{MOCK_WEBHOOK_HOST}/cgi-bin/webhook/send?key=MOCKPRODKEY0001"
MOCK_TEST_URL = f"http://{MOCK_WEBHOOK_HOST}/cgi-bin/webhook/send?key=MOCKTESTKEY0001"
MOCK_CUSTOM_URL = f"http://{MOCK_WEBHOOK_HOST}/cgi-bin/webhook/send?key=MOCKCUSTOMKEY01"

# The panel password the fixtures log in with (SEC-01). Tests that want the gate closed
# build their own `Settings` with a different value or use `anon_client`.
TEST_WEB_PASSWORD = "unit-test-panel-password"


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
        # The panel is guarded in production. Tests that exercise routes log in through
        # `login()` below, so the default fixture carries a password rather than leaving
        # the gate open.
        "web_username": "admin",
        "web_password": TEST_WEB_PASSWORD,
        "secret_key": None,
        "allowed_webhook_hosts": (MOCK_WEBHOOK_HOST,),
        "allow_insecure_local_webhooks": False,
        "secure_cookies": False,
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


def login(client: Any, password: str = TEST_WEB_PASSWORD, username: str = "admin") -> bool:
    """Authenticate a Flask test client so it can pass the SEC-01 gate.

    Returns True when the panel accepted the credentials. The CSRF token is read back from
    the rendered form rather than the session cookie because the session is signed.
    """
    page = client.get("/login")
    token = _csrf_from_html(page.get_data(as_text=True))
    if not token:
        return False
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
    )
    return response.status_code == 302


class PanelClient:
    """A test client that behaves like the real panel for a logged-in operator.

    `post()` adds the current CSRF token unless the caller already supplied one (or opted
    out with `csrf=False`), and reads it through the same channel the browser uses — the
    hidden field of a rendered page. That keeps the SEC-01 gate enforced in production and
    still lets route tests exercise the handlers.
    """

    def __init__(self, client: Any, db_path: Path | None = None) -> None:
        self._client = client
        # Route tests reach for the database behind the app; keep it on the wrapper so a
        # test can assert on stored rows without another fixture argument.
        self.db_path = db_path
        self._token_source = "/notifications"

    def post(self, url: str, data: Any = None, csrf: bool = True, **kwargs: Any):
        form = dict(data or {})
        if csrf and "csrf_token" not in form and "csrf_token" not in kwargs:
            form["csrf_token"] = self.csrf_token()
        return self._client.post(url, data=form, **kwargs)

    def get(self, *args: Any, **kwargs: Any):
        return self._client.get(*args, **kwargs)

    def raw(self) -> Any:
        """The underlying Flask test client, for tests that must dodge the wrapper."""
        return self._client

    def csrf_token(self, path: str | None = None) -> str:
        html = self._client.get(path or self._token_source).get_data(as_text=True)
        token = _csrf_from_html(html)
        if not token:
            raise AssertionError(f"no CSRF token found on {path or self._token_source}")
        return token

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def panel_client(client: Any) -> PanelClient:
    """Wrap an authenticated Flask test client."""
    return PanelClient(client)


def _csrf_from_html(html: str) -> str:
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return match.group(1) if match else ""


def csrf_token_for(client: Any, path: str = "/notifications") -> str:
    """Read the CSRF token embedded in a page the client is allowed to see."""
    return _csrf_from_html(client.get(path).get_data(as_text=True))


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
def app_client(logged_in_client):
    """Flask test client bound to an isolated database, already signed in."""
    return logged_in_client


@pytest.fixture()
def logged_in_client(db_path: Path, settings: Settings):
    """Flask test client that passed the SEC-01 login (and therefore holds a CSRF token)."""
    from qwbot.web import create_app

    init_db(db_path, settings)
    app = create_app(settings)
    app.config["TESTING"] = True
    with app.test_client() as client:
        assert login(client), "the test login should succeed"
        yield PanelClient(client, db_path)


@pytest.fixture()
def auth_client(logged_in_client):
    """Alias for a signed-in client, for tests that want to be explicit about it."""
    return logged_in_client


@pytest.fixture()
def anon_client(db_path: Path, settings: Settings):
    """Flask test client with no session at all — what an attacker or a bot sees."""
    from qwbot.web import create_app

    init_db(db_path, settings)
    app = create_app(settings)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client
