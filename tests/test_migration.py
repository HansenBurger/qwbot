"""One-shot migration from the legacy global model to webhook -> template -> schedule."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from qwbot import store
from qwbot.message import build_custom_notification, render_message_template
from tests.conftest import MOCK_PROD_URL, MOCK_TEST_URL, init_db, make_settings

LEGACY_TEMPLATE = "## {title_date} 组内重点工作\n进度：[点击]({progress_doc_link})"
SPARE_TEMPLATE = "备用模板内容"
NOW = "2026-07-01T09:00:00"


def seed_legacy(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO reminder_templates"
        " (name, template_content, is_active, sort_order, created_at, updated_at)"
        " VALUES (?, ?, 1, 0, ?, ?)",
        ("默认模板", LEGACY_TEMPLATE, NOW, NOW),
    )
    connection.execute(
        "INSERT INTO reminder_templates"
        " (name, template_content, is_active, sort_order, created_at, updated_at)"
        " VALUES (?, ?, 0, 1, ?, ?)",
        ("备用模板", SPARE_TEMPLATE, NOW, NOW),
    )
    for reminder_id, send_time in [
        ("morning-reminder", "09:00"),
        ("evening-reminder", "18:00"),
        ("work-reminder-extra", "12:30"),
    ]:
        connection.execute(
            "INSERT INTO scheduler_times (reminder_id, send_time, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (reminder_id, send_time, NOW, NOW),
        )
    connection.execute(
        "INSERT INTO scheduler_skip_dates (skip_date, created_at) VALUES (?, ?)",
        ("2026-10-01", NOW),
    )
    connection.execute(
        "INSERT INTO scheduler_force_dates (force_date, created_at) VALUES (?, ?)",
        ("2026-10-03", NOW),
    )
    connection.execute(
        "INSERT INTO notification_tasks"
        " (title, content, doc_url, send_time, at_all, date_rule, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "提醒更新进度",
            "请大家更新今天的执行进度",
            '[{"label": "进度统计", "url": "{progress_doc_link}"},'
            ' {"label": "加班申请", "url": "https://doc.example/agenda"}]',
            "17:30",
            "true",
            "all",
            NOW,
            NOW,
        ),
    )
    connection.execute(
        "INSERT INTO notification_tasks"
        " (title, content, doc_url, send_time, at_all, date_rule, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("晨报", "早上好", "", "08:00", "", "business_day", NOW, NOW),
    )
    connection.execute(
        "INSERT INTO notification_date_overrides"
        " (notification_task_id, target_date, mode, created_at) VALUES (?, ?, ?, ?)",
        (1, "2026-10-02", "skip", NOW),
    )
    connection.execute(
        "INSERT INTO reminder_template_vars"
        " (var_name, var_label, var_value, sort_order, created_at, updated_at)"
        " VALUES (?, ?, ?, 0, ?, ?)",
        ("progress_doc_link", "进度统计文档链接", "https://doc.example/db-progress", NOW, NOW),
    )
    connection.commit()


def migrate(db_path: Path, legacy_db: sqlite3.Connection, **overrides) -> None:
    seed_legacy(legacy_db)
    legacy_db.close()
    init_db(db_path, make_settings(db_path, **overrides))


def templates_by_name(db_path: Path) -> dict[str, dict]:
    return {item["name"]: item for item in store.list_message_templates(db_path)}


def find_template(db_path: Path, name: str) -> dict:
    tree = store.load_webhook_tree(db_path)
    template_id = int(templates_by_name(db_path)[name]["id"])
    return next(
        item
        for webhook in tree
        for item in webhook["templates"]
        if int(item["id"]) == template_id
    )


def test_webhooks_are_seeded_from_env(db_path: Path, legacy_db: sqlite3.Connection) -> None:
    migrate(db_path, legacy_db)

    webhooks = store.list_webhooks(db_path)
    assert [item["url"] for item in webhooks] == [MOCK_PROD_URL, MOCK_TEST_URL]
    default = store.get_default_webhook(db_path)
    assert default is not None and default["target"] == "test"


def test_active_template_becomes_enabled_and_spare_stays_disabled(
    db_path: Path, legacy_db: sqlite3.Connection
) -> None:
    migrate(db_path, legacy_db)

    by_name = templates_by_name(db_path)
    assert by_name[store.MIGRATED_REMINDER_TEMPLATE_NAME]["content"] == LEGACY_TEMPLATE
    assert by_name[store.MIGRATED_REMINDER_TEMPLATE_NAME]["is_enabled"] is True
    assert by_name["备用模板"]["content"] == SPARE_TEMPLATE
    assert by_name["备用模板"]["is_enabled"] is False


def test_scheduler_times_become_template_schedules(
    db_path: Path, legacy_db: sqlite3.Connection
) -> None:
    migrate(db_path, legacy_db)

    reminder_id = int(templates_by_name(db_path)[store.MIGRATED_REMINDER_TEMPLATE_NAME]["id"])
    times = [item["send_time"] for item in store.list_template_schedules(db_path, reminder_id)]
    assert times == ["09:00", "12:30", "18:00"]


def test_global_date_overrides_move_to_the_migrated_template(
    db_path: Path, legacy_db: sqlite3.Connection
) -> None:
    migrate(db_path, legacy_db)

    template = find_template(db_path, store.MIGRATED_REMINDER_TEMPLATE_NAME)
    assert template["skip_dates"] == ["2026-10-01"]
    assert template["force_dates"] == ["2026-10-03"]


def test_custom_notifications_become_templates_with_their_own_time(
    db_path: Path, legacy_db: sqlite3.Connection
) -> None:
    migrate(db_path, legacy_db)

    by_name = templates_by_name(db_path)
    assert "提醒更新进度" in by_name and "晨报" in by_name

    progress = by_name["提醒更新进度"]
    assert progress["at_all"] is True
    assert progress["date_rule"] == "all"
    assert progress["is_enabled"] is True
    assert [
        item["send_time"] for item in store.list_template_schedules(db_path, int(progress["id"]))
    ] == ["17:30"]

    morning = by_name["晨报"]
    assert morning["at_all"] is False
    assert morning["date_rule"] == "business_day"
    assert [
        item["send_time"] for item in store.list_template_schedules(db_path, int(morning["id"]))
    ] == ["08:00"]


def test_migrated_content_keeps_variable_placeholders(
    db_path: Path, legacy_db: sqlite3.Connection
) -> None:
    migrate(db_path, legacy_db)

    assert templates_by_name(db_path)["提醒更新进度"]["content"] == (
        "【提醒更新进度】\n"
        "\n"
        "请大家更新今天的执行进度\n"
        "\n"
        "相关文档：\n"
        "- [进度统计]({progress_doc_link})\n"
        "- [加班申请](https://doc.example/agenda)"
    )


def test_notification_date_overrides_are_migrated(
    db_path: Path, legacy_db: sqlite3.Connection
) -> None:
    migrate(db_path, legacy_db)

    assert find_template(db_path, "提醒更新进度")["skip_dates"] == ["2026-10-02"]


def test_migrated_custom_notification_renders_identically_to_the_legacy_builder(
    db_path: Path, legacy_db: sqlite3.Connection
) -> None:
    migrate(db_path, legacy_db)
    settings = make_settings(db_path)

    by_name = templates_by_name(db_path)
    template_vars = store.get_template_vars(db_path)
    legacy_tasks = {
        "提醒更新进度": {
            "title": "提醒更新进度",
            "content": "请大家更新今天的执行进度",
            "doc_links": [
                {"label": "进度统计", "url": "{progress_doc_link}"},
                {"label": "加班申请", "url": "https://doc.example/agenda"},
            ],
            "doc_url": "",
            "at_all": "true",
        },
        "晨报": {
            "title": "晨报",
            "content": "早上好",
            "doc_links": [],
            "doc_url": "",
            "at_all": "",
        },
    }

    for title, legacy_task in legacy_tasks.items():
        expected = build_custom_notification(legacy_task, template_vars, settings)
        actual = render_message_template(settings, by_name[title], template_vars, None)
        assert actual == expected, title

    # Sanity check: the doc-link variable really was substituted from the DB value.
    assert "https://doc.example/db-progress" in build_custom_notification(
        legacy_tasks["提醒更新进度"], template_vars, settings
    )


def test_migration_is_idempotent_and_flagged(db_path: Path, legacy_db: sqlite3.Connection) -> None:
    migrate(db_path, legacy_db)
    first_tree = store.load_webhook_tree(db_path)
    first_plan = store.load_dispatch_plan(db_path)

    init_db(db_path, make_settings(db_path))
    init_db(db_path, make_settings(db_path))

    second_tree = store.load_webhook_tree(db_path)
    assert len(second_tree) == len(first_tree)
    assert [item["template_count"] for item in second_tree] == [
        item["template_count"] for item in first_tree
    ]
    assert store.load_dispatch_plan(db_path) == first_plan

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key = 'webhook_model_migrated'"
        ).fetchone()
    assert row is not None and row[0] == "true"


def test_legacy_tables_are_preserved_for_rollback(
    db_path: Path, legacy_db: sqlite3.Connection
) -> None:
    migrate(db_path, legacy_db)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reminder_templates").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM scheduler_times").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM notification_tasks").fetchone()[0] == 2
        assert (
            connection.execute("SELECT COUNT(*) FROM notification_date_overrides").fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM scheduler_skip_dates").fetchone()[0] == 1


def test_migration_without_legacy_rows_still_creates_a_working_template(
    db_path: Path, legacy_db: sqlite3.Connection
) -> None:
    # Only the DDL exists: `_ensure_schema` seeds the legacy defaults first, then migrates.
    legacy_db.close()
    init_db(db_path, make_settings(db_path))

    templates = store.list_message_templates(db_path)
    assert len(templates) == 1
    assert templates[0]["name"] == store.MIGRATED_REMINDER_TEMPLATE_NAME
    assert templates[0]["content"] == store.DEFAULT_REMINDER_TEMPLATE
    assert [item["send_time"] for item in store.list_template_schedules(db_path)] == [
        "09:00",
        "18:00",
    ]
