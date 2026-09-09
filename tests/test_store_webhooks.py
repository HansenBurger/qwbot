"""QWB-002 webhook model: seeding, CRUD, cascade and dispatch plan."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwbot import store
from tests.conftest import MOCK_CUSTOM_URL, MOCK_PROD_URL, MOCK_TEST_URL, init_db, make_settings


def test_env_seeds_create_one_webhook_per_url(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)

    webhooks = store.list_webhooks(db_path)
    assert [item["name"] for item in webhooks] == ["生产群机器人", "自测群机器人"]
    assert [item["target"] for item in webhooks] == ["prod", "test"]
    assert [item["url"] for item in webhooks] == [MOCK_PROD_URL, MOCK_TEST_URL]


def test_default_webhook_follows_env_target(db_path: Path) -> None:
    settings = make_settings(db_path, webhook_target="prod")
    init_db(db_path, settings)

    default = store.get_default_webhook(db_path)
    assert default is not None
    assert default["target"] == "prod"
    assert store.resolve_default_webhook_url(db_path, settings) == MOCK_PROD_URL


def test_seeding_without_env_creates_placeholder(db_path: Path) -> None:
    settings = make_settings(
        db_path,
        webhook_url="",
        webhook_url_prod=None,
        webhook_url_test=None,
    )
    init_db(db_path, settings)

    webhooks = store.list_webhooks(db_path)
    assert len(webhooks) == 1
    assert webhooks[0]["url"] == ""
    assert webhooks[0]["name"] == store.DEFAULT_WEBHOOK_NAME
    assert webhooks[0]["is_default"] is True
    # A webhook without an address must not take part in dispatching.
    assert webhooks[0]["is_enabled"] is False
    assert store.load_dispatch_plan(db_path) == []
    assert store.resolve_default_webhook_url(db_path, settings) == ""


def test_seeding_runs_only_once(db_path: Path) -> None:
    settings = make_settings(db_path, webhook_url_test=None)
    init_db(db_path, settings)
    assert store.count_webhooks(db_path) == 1

    webhook_id = int(store.list_webhooks(db_path)[0]["id"])
    store.add_webhook(db_path, "临时群", MOCK_CUSTOM_URL)
    store.delete_webhook(db_path, webhook_id)

    # Re-initialising must not resurrect deleted webhooks from `.env`.
    init_db(db_path, settings)
    init_db(db_path, settings)
    remaining = store.list_webhooks(db_path)
    assert [item["name"] for item in remaining] == ["临时群"]


def test_database_url_wins_over_env_and_empty_falls_back(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook = store.list_webhooks(db_path)[0]
    webhook_id = int(webhook["id"])

    assert store.resolve_webhook_url(webhook, settings) == MOCK_PROD_URL

    store.update_webhook(db_path, webhook_id, webhook["name"], "", "prod")
    emptied = store.get_webhook(db_path, webhook_id)
    assert emptied is not None
    assert store.resolve_webhook_url(emptied, settings) == MOCK_PROD_URL

    store.update_webhook(db_path, webhook_id, webhook["name"], MOCK_CUSTOM_URL, "prod")
    updated = store.get_webhook(db_path, webhook_id)
    assert updated is not None
    assert store.resolve_webhook_url(updated, settings) == MOCK_CUSTOM_URL


def test_resolve_target_webhook_prefers_matching_target(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)

    webhook, url = store.resolve_target_webhook(db_path, "test", settings)
    assert webhook is not None and webhook["target"] == "test"
    assert url == MOCK_TEST_URL

    # No `custom` webhook exists, so the default is used instead of raising.
    webhook, _ = store.resolve_target_webhook(db_path, "custom", settings)
    assert webhook is not None
    assert webhook["is_default"]


def test_add_update_toggle_and_default_webhook(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)

    webhook_id = store.add_webhook(db_path, "核算二群", MOCK_CUSTOM_URL, target="custom")
    webhook = store.get_webhook(db_path, webhook_id)
    assert webhook is not None
    assert webhook["is_enabled"] is True
    assert webhook["is_default"] is False
    assert webhook["target_label"] == "自定义"

    store.update_webhook(
        db_path, webhook_id, "核算二群（改）", MOCK_CUSTOM_URL, "custom", "备注", False
    )
    webhook = store.get_webhook(db_path, webhook_id)
    assert webhook is not None
    assert webhook["name"] == "核算二群（改）"
    assert webhook["remark"] == "备注"
    assert webhook["is_enabled"] is False

    assert store.toggle_webhook(db_path, webhook_id) is True
    assert store.toggle_webhook(db_path, webhook_id) is False

    store.set_default_webhook(db_path, webhook_id)
    default = store.get_default_webhook(db_path)
    assert default is not None and int(default["id"]) == webhook_id
    # Exactly one default at any time.
    assert sum(1 for item in store.list_webhooks(db_path) if item["is_default"]) == 1


def test_add_webhook_with_is_default_moves_the_flag(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)

    webhook_id = store.add_webhook(db_path, "新默认", MOCK_CUSTOM_URL, is_default=True)
    default = store.get_default_webhook(db_path)
    assert default is not None and int(default["id"]) == webhook_id
    assert sum(1 for item in store.list_webhooks(db_path) if item["is_default"]) == 1


def test_delete_webhook_cascades_to_templates_and_schedules(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "待删除", MOCK_CUSTOM_URL)
    template_id = store.add_message_template(
        db_path, webhook_id, "模板A", "内容A", send_times=["09:00", "18:00"]
    )
    store.set_template_date_override(db_path, template_id, "2026-10-01", "skip")

    store.delete_webhook(db_path, webhook_id)

    assert store.get_webhook(db_path, webhook_id) is None
    assert store.list_message_templates(db_path, webhook_id) == []
    assert store.list_template_schedules(db_path, template_id) == []
    assert all(entry["webhook_id"] != webhook_id for entry in store.load_dispatch_plan(db_path))


def test_delete_default_webhook_reassigns_default(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    default = store.get_default_webhook(db_path)
    assert default is not None

    store.add_webhook(db_path, "另一个群", MOCK_CUSTOM_URL)
    store.delete_webhook(db_path, int(default["id"]))

    # The first remaining webhook (by sort_order) becomes the new default.
    new_default = store.get_default_webhook(db_path)
    assert new_default is not None
    assert new_default["name"] == "生产群机器人"
    assert new_default["is_default"] is True
    assert sum(1 for item in store.list_webhooks(db_path) if item["is_default"]) == 1


def test_last_webhook_cannot_be_deleted(db_path: Path) -> None:
    settings = make_settings(db_path, webhook_url_prod=MOCK_PROD_URL, webhook_url_test=None)
    init_db(db_path, settings)
    only = store.list_webhooks(db_path)[0]

    with pytest.raises(ValueError):
        store.delete_webhook(db_path, int(only["id"]))
    assert store.count_webhooks(db_path) == 1


def test_templates_and_schedules_crud(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = int(store.list_webhooks(db_path)[0]["id"])

    template_id = store.add_message_template(
        db_path,
        webhook_id,
        "日报",
        "## {title_date} 日报",
        at_all="true",
        date_rule="all",
        send_times=["09:00", "9:5"],
    )
    template = store.get_message_template(db_path, template_id)
    assert template is not None
    assert template["at_all"] is True
    assert template["date_rule"] == "all"
    # "9:5" is normalized to HH:MM and both distinct times are kept.
    assert [
        item["send_time"] for item in store.list_template_schedules(db_path, template_id)
    ] == ["09:00", "09:05"]

    store.update_message_template(
        db_path, template_id, "日报v2", "内容v2", "", "business_day", False
    )
    template = store.get_message_template(db_path, template_id)
    assert template is not None
    assert template["name"] == "日报v2"
    assert template["at_all"] is False
    assert template["is_enabled"] is False

    assert store.toggle_message_template(db_path, template_id) is True

    schedule_id = store.add_template_schedule(
        db_path, template_id, "12:30", date_rule="holiday_only"
    )
    schedule = next(
        item
        for item in store.list_template_schedules(db_path, template_id)
        if int(item["id"]) == schedule_id
    )
    assert schedule["send_time"] == "12:30"
    assert schedule["date_rule"] == "holiday_only"

    store.update_template_schedule(db_path, schedule_id, "13:45")
    schedule = next(
        item
        for item in store.list_template_schedules(db_path, template_id)
        if int(item["id"]) == schedule_id
    )
    assert schedule["send_time"] == "13:45"
    # date_rule is preserved when only the time changes.
    assert schedule["date_rule"] == "holiday_only"

    assert store.toggle_template_schedule(db_path, schedule_id) is False
    store.delete_template_schedule(db_path, schedule_id)
    assert all(
        int(item["id"]) != schedule_id
        for item in store.list_template_schedules(db_path, template_id)
    )


def test_invalid_send_time_is_rejected(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = int(store.list_webhooks(db_path)[0]["id"])
    template_id = store.add_message_template(db_path, webhook_id, "模板", "内容")

    with pytest.raises(ValueError):
        store.add_template_schedule(db_path, template_id, "not-a-time")


def test_unknown_date_rule_falls_back(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = int(store.list_webhooks(db_path)[0]["id"])

    template_id = store.add_message_template(
        db_path, webhook_id, "模板", "内容", date_rule="bogus"
    )
    template = store.get_message_template(db_path, template_id)
    assert template is not None and template["date_rule"] == "business_day"

    schedule_id = store.add_template_schedule(db_path, template_id, "09:00", date_rule="bogus")
    schedule = next(
        item
        for item in store.list_template_schedules(db_path, template_id)
        if int(item["id"]) == schedule_id
    )
    # An unknown schedule rule becomes empty, i.e. "inherit from the template".
    assert schedule["date_rule"] == ""


def test_duplicate_template_is_disabled_with_schedules_copied(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = int(store.list_webhooks(db_path)[0]["id"])
    template_id = store.add_message_template(
        db_path, webhook_id, "原版", "内容", at_all="true", send_times=["09:00"]
    )

    copy_id = store.duplicate_message_template(db_path, template_id)
    copy = store.get_message_template(db_path, copy_id)
    assert copy is not None
    assert copy["name"] == "原版 副本"
    assert copy["content"] == "内容"
    assert copy["at_all"] is True
    assert copy["is_enabled"] is False
    schedules = store.list_template_schedules(db_path, copy_id)
    assert [item["send_time"] for item in schedules] == ["09:00"]
    assert all(item["is_enabled"] is False for item in schedules)
    # A disabled copy must not enter the dispatch plan.
    assert all(entry["template_id"] != copy_id for entry in store.load_dispatch_plan(db_path))


def test_delete_template_cascades_schedules_and_overrides(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = int(store.list_webhooks(db_path)[0]["id"])
    template_id = store.add_message_template(
        db_path, webhook_id, "模板", "内容", send_times=["09:00"]
    )
    store.set_template_date_override(db_path, template_id, "2026-10-01", "skip")

    store.delete_message_template(db_path, template_id)

    assert store.get_message_template(db_path, template_id) is None
    assert store.list_template_schedules(db_path, template_id) == []
    tree = store.load_webhook_tree(db_path)
    assert all(
        int(item["id"]) != template_id for webhook in tree for item in webhook["templates"]
    )


def test_load_webhook_tree_nests_templates_and_schedules(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "群A", MOCK_CUSTOM_URL)
    first = store.add_message_template(db_path, webhook_id, "模板1", "内容1", send_times=["09:00"])
    store.add_message_template(db_path, webhook_id, "模板2", "内容2", send_times=["12:00", "18:00"])
    store.set_template_date_override(db_path, first, "2026-10-01", "skip")

    tree = store.load_webhook_tree(db_path)
    entry = next(item for item in tree if int(item["id"]) == webhook_id)
    assert entry["template_count"] == 2
    assert entry["schedule_count"] == 3
    assert entry["enabled_schedule_count"] == 3
    template1 = next(item for item in entry["templates"] if int(item["id"]) == first)
    assert template1["skip_dates"] == ["2026-10-01"]
    assert [item["send_time"] for item in template1["schedules"]] == ["09:00"]


def test_dispatch_plan_only_contains_enabled_combinations(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "群A", MOCK_CUSTOM_URL)
    enabled = store.add_message_template(
        db_path, webhook_id, "启用模板", "内容", send_times=["09:00"]
    )
    disabled_template = store.add_message_template(
        db_path, webhook_id, "停用模板", "内容", is_enabled=False, send_times=["10:00"]
    )
    schedule_id = store.add_template_schedule(db_path, enabled, "11:00")
    store.toggle_template_schedule(db_path, schedule_id)

    plan = store.load_dispatch_plan(db_path)
    custom = [entry for entry in plan if entry["webhook_id"] == webhook_id]
    assert {(entry["template_id"], entry["send_time"]) for entry in custom} == {
        (enabled, "09:00")
    }
    assert all(entry["template_id"] != disabled_template for entry in plan)

    # Disabling the webhook removes every combination for it.
    store.toggle_webhook(db_path, webhook_id)
    plan = store.load_dispatch_plan(db_path)
    assert all(entry["webhook_id"] != webhook_id for entry in plan)


def test_dispatch_plan_carries_urls_rules_and_overrides(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "群A", MOCK_CUSTOM_URL)
    template_id = store.add_message_template(
        db_path, webhook_id, "模板", "内容", at_all="true", date_rule="holiday_only",
        send_times=["09:00"],
    )
    store.add_template_schedule(db_path, template_id, "21:00", date_rule="all")
    store.set_template_date_override(db_path, template_id, "2026-10-01", "skip")
    store.add_scheduler_skip_date(db_path, "2026-10-02")
    store.add_scheduler_force_date(db_path, "2026-10-03")

    plan = {entry["send_time"]: entry for entry in store.load_dispatch_plan(db_path)}
    entry = plan["09:00"]
    assert entry["webhook_url"] == MOCK_CUSTOM_URL
    assert entry["webhook_name"] == "群A"
    assert entry["at_all"] is True
    assert entry["template_date_rule"] == "holiday_only"
    assert entry["schedule_date_rule"] == ""
    assert entry["skip_dates"] == ["2026-10-01"]
    assert entry["global_skip_dates"] == ["2026-10-02"]
    assert entry["global_force_dates"] == ["2026-10-03"]
    assert plan["21:00"]["schedule_date_rule"] == "all"


def test_import_env_webhooks_skips_existing_urls(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    before = store.count_webhooks(db_path)

    assert store.import_env_webhooks(db_path, settings.webhook_seeds(), "test") == 0
    assert store.count_webhooks(db_path) == before

    store.delete_webhook(db_path, int(store.list_webhooks(db_path)[0]["id"]))
    assert store.import_env_webhooks(db_path, settings.webhook_seeds(), "test") == 1


def test_import_env_webhooks_without_seeds_is_a_noop(db_path: Path) -> None:
    settings = make_settings(db_path, webhook_url_prod=None, webhook_url_test=None)
    init_db(db_path, settings)
    before = store.count_webhooks(db_path)
    assert store.import_env_webhooks(db_path, settings.webhook_seeds()) == 0
    assert store.count_webhooks(db_path) == before


def test_date_override_upsert_keeps_single_row(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = int(store.list_webhooks(db_path)[0]["id"])
    template_id = store.add_message_template(db_path, webhook_id, "模板", "内容")

    def overrides() -> dict[str, list[str]]:
        tree = store.load_webhook_tree(db_path)
        template = next(
            item
            for webhook in tree
            for item in webhook["templates"]
            if int(item["id"]) == template_id
        )
        return {"skip": template["skip_dates"], "force": template["force_dates"]}

    store.set_template_date_override(db_path, template_id, "2026-10-01", "skip")
    assert overrides() == {"skip": ["2026-10-01"], "force": []}

    store.set_template_date_override(db_path, template_id, "2026-10-01", "force")
    assert overrides() == {"skip": [], "force": ["2026-10-01"]}

    # Invalid modes and empty dates are ignored.
    store.set_template_date_override(db_path, template_id, "2026-10-01", "bogus")
    store.set_template_date_override(db_path, template_id, "   ", "skip")
    assert overrides() == {"skip": [], "force": ["2026-10-01"]}

    store.delete_template_date_override(db_path, template_id, "2026-10-01")
    assert overrides() == {"skip": [], "force": []}
