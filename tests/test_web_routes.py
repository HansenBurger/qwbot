"""HTTP level tests for the webhook / template / schedule maintenance routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

import pytest

from qwbot import store
from qwbot import web as web_module
from tests.conftest import MOCK_CUSTOM_URL, MOCK_PROD_URL, MOCK_TEST_URL, init_db, make_settings


class FakeWebhookClient:
    """Records sends instead of talking to WeCom."""

    sent: list[dict[str, Any]] = []

    def __init__(self, webhook_url: str, timeout_seconds: int = 10) -> None:
        self.webhook_url = webhook_url

    def send_markdown(self, content: str) -> None:
        FakeWebhookClient.sent.append(
            {"url": self.webhook_url, "msgtype": "markdown", "content": content}
        )

    def send_text(self, content: str, **kwargs: Any) -> None:
        FakeWebhookClient.sent.append({"url": self.webhook_url, "msgtype": "text", "content": content})


@pytest.fixture(autouse=True)
def no_real_sends(monkeypatch: pytest.MonkeyPatch):
    FakeWebhookClient.sent = []
    monkeypatch.setattr(web_module, "WeComWebhookClient", FakeWebhookClient)
    yield
    FakeWebhookClient.sent = []


@pytest.fixture()
def client(db_path: Path):
    from qwbot.web import create_app

    settings = make_settings(db_path)
    init_db(db_path, settings)
    app = create_app(settings)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def location(response) -> str:
    """Decoded `Location` header so Chinese flash messages can be asserted on."""
    return unquote_plus(response.headers["Location"])


def webhook_ids(db_path: Path) -> list[int]:
    return [int(item["id"]) for item in store.list_webhooks(db_path)]


def first_template_id(db_path: Path) -> int:
    return int(store.list_message_templates(db_path)[0]["id"])


def test_notifications_page_renders_three_levels(client, db_path: Path) -> None:
    response = client.get("/notifications")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "群机器人与消息通知" in html
    assert "生产群机器人" in html and "自测群机器人" in html
    assert store.MIGRATED_REMINDER_TEMPLATE_NAME in html
    assert "09:00" in html and "18:00" in html
    # Webhook keys must never be rendered in full.
    assert MOCK_PROD_URL not in html
    assert "MOCK****0001" in html


def test_index_page_still_renders(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "跑批计划维护" in response.get_data(as_text=True)


def test_create_webhook_with_first_template(client, db_path: Path) -> None:
    before = webhook_ids(db_path)
    response = client.post(
        "/webhooks",
        data={
            "name": "核算二群",
            "url": MOCK_CUSTOM_URL,
            "target": "custom",
            "remark": "联调群",
            "is_enabled": "true",
            "send_time": "10:30",
        },
    )
    assert response.status_code == 302
    assert "已新增机器人" in location(response)

    created = [item for item in store.list_webhooks(db_path) if int(item["id"]) not in before]
    assert len(created) == 1
    webhook_id = int(created[0]["id"])
    templates = store.list_message_templates(db_path, webhook_id)
    assert len(templates) == 1
    assert templates[0]["name"] == "核算二群 默认模板"
    assert [
        item["send_time"]
        for item in store.list_template_schedules(db_path, int(templates[0]["id"]))
    ] == ["10:30"]


def test_create_webhook_validates_input(client, db_path: Path) -> None:
    before = webhook_ids(db_path)

    response = client.post("/webhooks", data={"name": "", "url": MOCK_CUSTOM_URL})
    assert "请填写机器人名称" in location(response)

    response = client.post("/webhooks", data={"name": "x", "url": ""})
    assert "请填写机器人 webhook 地址" in location(response)

    response = client.post("/webhooks", data={"name": "x", "url": "ftp://nope"})
    assert "http" in location(response)

    response = client.post("/webhooks", data={"name": "x", "url": MOCK_PROD_URL})
    assert "已存在于" in location(response)

    assert webhook_ids(db_path) == before


def test_create_webhook_as_default(client, db_path: Path) -> None:
    client.post(
        "/webhooks",
        data={"name": "新默认", "url": MOCK_CUSTOM_URL, "is_default": "true", "is_enabled": "true"},
    )
    default = store.get_default_webhook(db_path)
    assert default is not None and default["name"] == "新默认"


def test_edit_webhook_keeps_url_when_field_is_blank(client, db_path: Path) -> None:
    webhook_id = webhook_ids(db_path)[0]
    response = client.post(
        f"/webhooks/{webhook_id}",
        data={"name": "生产群（改名）", "url": "", "target": "prod", "is_enabled": "true"},
    )
    assert response.status_code == 302

    webhook = store.get_webhook(db_path, webhook_id)
    assert webhook is not None
    assert webhook["name"] == "生产群（改名）"
    # The edit form never carries the stored key, so blank means "unchanged".
    assert webhook["url"] == MOCK_PROD_URL


def test_edit_webhook_can_clear_url_to_use_env_fallback(client, db_path: Path) -> None:
    webhook_id = webhook_ids(db_path)[0]
    client.post(
        f"/webhooks/{webhook_id}",
        data={
            "name": "生产群机器人",
            "url": "",
            "clear_url": "true",
            "target": "prod",
            "is_enabled": "true",
        },
    )

    settings = make_settings(db_path)
    webhook = store.get_webhook(db_path, webhook_id)
    assert webhook is not None
    assert webhook["url"] == ""
    assert store.resolve_webhook_url(webhook, settings) == MOCK_PROD_URL


def test_edit_webhook_can_replace_url(client, db_path: Path) -> None:
    webhook_id = webhook_ids(db_path)[0]
    client.post(
        f"/webhooks/{webhook_id}",
        data={"name": "生产群机器人", "url": MOCK_CUSTOM_URL, "target": "prod", "is_enabled": "true"},
    )
    webhook = store.get_webhook(db_path, webhook_id)
    assert webhook is not None and webhook["url"] == MOCK_CUSTOM_URL


def test_duplicate_webhook_url_is_rejected_on_edit(client, db_path: Path) -> None:
    ids = webhook_ids(db_path)
    response = client.post(
        f"/webhooks/{ids[0]}",
        data={"name": "生产群机器人", "url": MOCK_TEST_URL, "target": "prod", "is_enabled": "true"},
    )
    assert "已存在于" in location(response)
    webhook = store.get_webhook(db_path, ids[0])
    assert webhook is not None and webhook["url"] == MOCK_PROD_URL


def test_toggle_and_set_default_webhook(client, db_path: Path) -> None:
    ids = webhook_ids(db_path)

    client.post(f"/webhooks/{ids[0]}/toggle")
    webhook = store.get_webhook(db_path, ids[0])
    assert webhook is not None and webhook["is_enabled"] is False
    client.post(f"/webhooks/{ids[0]}/toggle")
    webhook = store.get_webhook(db_path, ids[0])
    assert webhook is not None and webhook["is_enabled"] is True

    client.post(f"/webhooks/{ids[0]}/set-default")
    default = store.get_default_webhook(db_path)
    assert default is not None and int(default["id"]) == ids[0]


def test_delete_webhook_is_blocked_for_the_last_one(client, db_path: Path) -> None:
    ids = webhook_ids(db_path)
    for webhook_id in ids[1:]:
        client.post(f"/webhooks/{webhook_id}/delete")
    assert store.count_webhooks(db_path) == 1

    response = client.post(f"/webhooks/{ids[0]}/delete")
    assert "至少需要保留一个群机器人" in location(response)
    assert store.count_webhooks(db_path) == 1


def test_delete_webhook_cascades(client, db_path: Path) -> None:
    client.post(
        "/webhooks",
        data={"name": "临时群", "url": MOCK_CUSTOM_URL, "is_enabled": "true", "send_time": "09:00"},
    )
    webhook = next(item for item in store.list_webhooks(db_path) if item["name"] == "临时群")
    webhook_id = int(webhook["id"])
    template_id = int(store.list_message_templates(db_path, webhook_id)[0]["id"])

    client.post(f"/webhooks/{webhook_id}/delete")

    assert store.get_webhook(db_path, webhook_id) is None
    assert store.get_message_template(db_path, template_id) is None
    assert store.list_template_schedules(db_path, template_id) == []


def test_webhook_connectivity_test_sends_to_that_webhook(client, db_path: Path) -> None:
    ids = webhook_ids(db_path)
    response = client.post(f"/webhooks/{ids[0]}/test")
    assert response.status_code == 302
    assert "连通性测试成功" in location(response)
    assert FakeWebhookClient.sent == [
        {"url": MOCK_PROD_URL, "msgtype": "text", "content": web_module.CONNECTIVITY_TEST_TEXT}
    ]


def test_webhook_connectivity_test_reports_missing_url(client, db_path: Path) -> None:
    webhook_id = store.add_webhook(db_path, "空地址", "", target="custom")
    response = client.post(f"/webhooks/{webhook_id}/test")
    assert "没有可用的 webhook 地址" in location(response)
    assert FakeWebhookClient.sent == []


def test_import_env_webhooks_reports_no_new_rows(client, db_path: Path) -> None:
    response = client.post("/webhooks/import-env")
    assert "没有新的 webhook" in location(response)
    assert store.count_webhooks(db_path) == 2


def test_create_template_and_schedules(client, db_path: Path) -> None:
    webhook_id = webhook_ids(db_path)[0]
    response = client.post(
        f"/webhooks/{webhook_id}/templates",
        data={
            "name": "晚间日报",
            "content": "## 晚间日报\n进度：[点击]({progress_doc_link})",
            "date_rule": "all",
            "at_all": "true",
            "is_enabled": "true",
            "send_time": "20:00",
        },
    )
    assert response.status_code == 302
    assert "已新增模板" in location(response)

    template = next(
        item for item in store.list_message_templates(db_path, webhook_id)
        if item["name"] == "晚间日报"
    )
    assert template["at_all"] is True
    assert template["date_rule"] == "all"
    assert [
        item["send_time"]
        for item in store.list_template_schedules(db_path, int(template["id"]))
    ] == ["20:00"]

    template_id = int(template["id"])
    client.post(f"/templates/{template_id}/schedules", data={"send_time": "22:15", "date_rule": ""})
    assert [
        item["send_time"] for item in store.list_template_schedules(db_path, template_id)
    ] == ["20:00", "22:15"]

    # Invalid time is rejected with a readable message.
    response = client.post(f"/templates/{template_id}/schedules", data={"send_time": "abc"})
    assert "有效的通知时间" in location(response)


def test_create_template_requires_a_name(client, db_path: Path) -> None:
    webhook_id = webhook_ids(db_path)[0]
    response = client.post(f"/webhooks/{webhook_id}/templates", data={"name": "", "content": "x"})
    assert "请填写模板名称" in location(response)


def test_edit_template_persists_content_and_flags(client, db_path: Path) -> None:
    webhook_id = webhook_ids(db_path)[0]
    client.post(
        f"/webhooks/{webhook_id}/templates",
        data={"name": "待编辑", "content": "原始内容", "date_rule": "business_day"},
    )
    template = next(
        item for item in store.list_message_templates(db_path, webhook_id)
        if item["name"] == "待编辑"
    )
    template_id = int(template["id"])

    client.post(
        f"/templates/{template_id}",
        data={
            "name": "已编辑",
            "content": "新内容 {title_date}",
            "date_rule": "holiday_only",
            "at_all": "true",
            "is_enabled": "true",
        },
    )
    updated = store.get_message_template(db_path, template_id)
    assert updated is not None
    assert updated["name"] == "已编辑"
    assert updated["content"] == "新内容 {title_date}"
    assert updated["date_rule"] == "holiday_only"
    assert updated["at_all"] is True

    # An unchecked checkbox posts the empty hidden companion value.
    client.post(
        f"/templates/{template_id}",
        data={"name": "已编辑", "content": "新内容", "date_rule": "holiday_only", "is_enabled": ""},
    )
    updated = store.get_message_template(db_path, template_id)
    assert updated is not None and updated["is_enabled"] is False

    # Omitting the field entirely keeps the stored state (programmatic posts).
    client.post(
        f"/templates/{template_id}",
        data={"name": "已编辑", "content": "新内容", "date_rule": "holiday_only"},
    )
    updated = store.get_message_template(db_path, template_id)
    assert updated is not None and updated["is_enabled"] is False
    client.post(
        f"/templates/{template_id}",
        data={
            "name": "已编辑",
            "content": "新内容",
            "date_rule": "holiday_only",
            "is_enabled": "true",
        },
    )
    updated = store.get_message_template(db_path, template_id)
    assert updated is not None and updated["is_enabled"] is True


def test_toggle_duplicate_and_delete_template(client, db_path: Path) -> None:
    webhook_id = webhook_ids(db_path)[0]
    client.post(
        f"/webhooks/{webhook_id}/templates",
        data={"name": "源模板", "content": "内容", "is_enabled": "true", "send_time": "09:00"},
    )
    template = next(
        item for item in store.list_message_templates(db_path, webhook_id)
        if item["name"] == "源模板"
    )
    template_id = int(template["id"])

    client.post(f"/templates/{template_id}/toggle")
    assert store.get_message_template(db_path, template_id)["is_enabled"] is False

    client.post(f"/templates/{template_id}/duplicate")
    copy = next(
        item for item in store.list_message_templates(db_path, webhook_id)
        if item["name"] == "源模板 副本"
    )
    assert copy["is_enabled"] is False

    client.post(f"/templates/{template_id}/delete")
    assert store.get_message_template(db_path, template_id) is None
    assert store.get_message_template(db_path, int(copy["id"])) is not None


def test_edit_and_delete_schedule(client, db_path: Path) -> None:
    webhook_id = webhook_ids(db_path)[0]
    template_id = first_template_id(db_path)
    schedule_id = store.add_template_schedule(db_path, template_id, "13:00")

    client.post(f"/schedules/{schedule_id}", data={"send_time": "14:05", "date_rule": "all"})
    schedule = next(
        item for item in store.list_template_schedules(db_path, template_id)
        if int(item["id"]) == schedule_id
    )
    assert schedule["send_time"] == "14:05"
    assert schedule["date_rule"] == "all"

    client.post(f"/schedules/{schedule_id}/toggle")
    schedule = next(
        item for item in store.list_template_schedules(db_path, template_id)
        if int(item["id"]) == schedule_id
    )
    assert schedule["is_enabled"] is False

    client.post(f"/schedules/{schedule_id}/delete")
    assert all(
        int(item["id"]) != schedule_id
        for item in store.list_template_schedules(db_path, template_id)
    )

    response = client.post(f"/schedules/{schedule_id}/delete")
    assert "通知时间不存在" in location(response)


def test_template_date_overrides(client, db_path: Path) -> None:
    template_id = first_template_id(db_path)

    client.post(f"/templates/{template_id}/skip-date", data={"target_date": "2026-10-01"})
    tree = store.load_webhook_tree(db_path)
    template = next(
        item for webhook in tree for item in webhook["templates"] if int(item["id"]) == template_id
    )
    assert template["skip_dates"] == ["2026-10-01"]

    client.post(f"/templates/{template_id}/force-date", data={"target_date": "2026-10-01"})
    tree = store.load_webhook_tree(db_path)
    template = next(
        item for webhook in tree for item in webhook["templates"] if int(item["id"]) == template_id
    )
    assert template["force_dates"] == ["2026-10-01"]
    assert template["skip_dates"] == []

    client.post(
        f"/templates/{template_id}/date-overrides/delete", data={"target_date": "2026-10-01"}
    )
    tree = store.load_webhook_tree(db_path)
    template = next(
        item for webhook in tree for item in webhook["templates"] if int(item["id"]) == template_id
    )
    assert template["force_dates"] == []


def test_global_skip_and_force_dates_still_work(client, db_path: Path) -> None:
    client.post("/scheduler-skip-dates", data={"skip_date": "2026-10-01"})
    payload = store.load_status_file(db_path)
    assert [item["date"] for item in payload["scheduler_skip_dates"]] == ["2026-10-01"]
    # A skip date also clears a force date for the same day.
    assert payload["scheduler_force_dates"] == []

    client.post("/scheduler-force-dates", data={"force_date": "2026-10-01"})
    payload = store.load_status_file(db_path)
    assert [item["date"] for item in payload["scheduler_force_dates"]] == ["2026-10-01"]
    assert payload["scheduler_skip_dates"] == []

    client.post("/scheduler-force-dates/delete", data={"force_date": "2026-10-01"})
    payload = store.load_status_file(db_path)
    assert payload["scheduler_force_dates"] == []


def test_template_vars_crud_still_works(client, db_path: Path) -> None:
    response = client.post(
        "/template-vars",
        data={
            "var_name": "extra_doc_link",
            "var_label": "额外文档",
            "var_value": "https://doc.example/extra",
        },
    )
    assert response.status_code == 302
    variables = {item["var_name"]: item for item in store.get_template_vars(db_path)}
    assert "extra_doc_link" in variables
    var_id = int(variables["extra_doc_link"]["id"])

    # Duplicate link values are rejected.
    response = client.post(
        "/template-vars",
        data={"var_name": "dup", "var_label": "重复", "var_value": "https://doc.example/extra"},
    )
    assert "链接已存在于变量" in location(response)

    client.post(
        f"/template-vars/{var_id}",
        data={"var_name": "extra_doc_link", "var_label": "额外文档2", "var_value": ""},
    )
    variables = {item["var_name"]: item for item in store.get_template_vars(db_path)}
    assert variables["extra_doc_link"]["var_label"] == "额外文档2"

    client.post(f"/template-vars/{var_id}/delete")
    assert "extra_doc_link" not in {item["var_name"] for item in store.get_template_vars(db_path)}


def test_test_send_uses_the_owning_webhook_and_unsaved_content(client, db_path: Path) -> None:
    # A template under the prod webhook must not be sent to the default (test) one.
    prod_id = next(
        int(item["id"]) for item in store.list_webhooks(db_path) if item["target"] == "prod"
    )
    template_id = store.add_message_template(
        db_path, prod_id, "生产模板", "已保存的内容", send_times=["09:00"]
    )

    response = client.post(
        f"/templates/{template_id}/test-send", data={"content": "未保存的内容", "at_all": "true"}
    )
    assert response.status_code == 302
    assert "已测试发送到「生产群机器人」" in location(response)
    assert len(FakeWebhookClient.sent) == 1
    sent = FakeWebhookClient.sent[0]
    assert sent["url"] == MOCK_PROD_URL
    assert sent["msgtype"] == "markdown"
    assert sent["content"] == "未保存的内容\n\n<@all>"

    # Without form content the stored template is used.
    FakeWebhookClient.sent = []
    client.post(f"/templates/{template_id}/test-send", data={})
    assert len(FakeWebhookClient.sent) == 1
    assert FakeWebhookClient.sent[0]["content"] == "已保存的内容"

    # The migrated built-in reminder belongs to the default (test) webhook.
    builtin_id = next(
        int(item["id"])
        for item in store.list_message_templates(db_path)
        if item["name"] == store.MIGRATED_REMINDER_TEMPLATE_NAME
    )
    FakeWebhookClient.sent = []
    client.post(f"/templates/{builtin_id}/test-send", data={})
    assert FakeWebhookClient.sent[0]["url"] == MOCK_TEST_URL
    assert "组内重点工作" in FakeWebhookClient.sent[0]["content"]


def test_test_send_reports_missing_webhook_url(client, db_path: Path) -> None:
    webhook_id = store.add_webhook(db_path, "空地址", "", target="custom")
    template_id = store.add_message_template(db_path, webhook_id, "模板", "内容")

    response = client.post(f"/templates/{template_id}/test-send", data={})
    assert "没有可用的 webhook 地址" in location(response)
    assert FakeWebhookClient.sent == []


def test_unknown_template_and_webhook_ids_are_handled(client) -> None:
    assert "不存在" in location(client.post("/templates/999999"))
    assert "不存在" in location(client.post("/templates/999999/delete"))
    assert "不存在" in location(client.post("/templates/999999/toggle"))
    assert "不存在" in location(client.post("/templates/999999/duplicate"))
    assert "不存在" in location(client.post("/templates/999999/test-send"))
    assert "不存在" in location(client.post("/webhooks/999999/test"))
    assert "不存在" in location(client.post("/webhooks/999999/toggle"))
    assert "不存在" in location(client.post("/webhooks/999999/set-default"))
    assert "不存在" in location(client.post("/webhooks/999999/delete"))
    assert "不存在" in location(client.post("/webhooks/999999/templates", data={"name": "x"}))


def test_legacy_routes_are_gone(client) -> None:
    for path in (
        "/notifications",
        "/scheduler-times",
        "/reminder-template",
        "/reminder-templates",
        "/reminder-template/test-send",
    ):
        assert client.post(path, data={}).status_code in (404, 405), path


def _create_batch(client, db_path: Path) -> int:
    client.post(
        "/items/batch_plan",
        data={
            "natural_date": "2026-09-09",
            "description": "跑批一",
            "current_accounting_date": "2026-09-08",
            "next_accounting_date": "2026-09-09",
            "execution_status": "待执行",
        },
    )
    items = store.load_status_file(db_path)["batch_plan"]
    assert len(items) == 1
    return int(items[0]["id"])


def test_batch_event_notices_are_off_by_default(client, db_path: Path) -> None:
    """一天多批时不再逐个批推送，点“开始/完成”只更新状态。"""
    index = _create_batch(client, db_path)
    assert store.batch_event_notify_enabled(db_path) is False

    client.post(f"/items/batch_plan/{index}/start")
    assert FakeWebhookClient.sent == []
    assert store.get_item(db_path, "batch_plan", index)["execution_status"] == "进行中"

    client.post(f"/items/batch_plan/{index}/complete")
    assert FakeWebhookClient.sent == []
    assert store.get_item(db_path, "batch_plan", index)["execution_status"] == "已完成"


def test_batch_event_notices_go_to_the_default_webhook_when_enabled(client, db_path: Path) -> None:
    index = _create_batch(client, db_path)
    assert "已开启" in location(client.post("/batch-event-notify", data={"enabled": "true"}))
    assert store.batch_event_notify_enabled(db_path) is True

    client.post(f"/items/batch_plan/{index}/start")
    assert FakeWebhookClient.sent, "batch start notice was not sent"
    sent = FakeWebhookClient.sent[-1]
    # `WECOM_WEBHOOK_TARGET=test` makes the test webhook the default one.
    assert sent["url"] == MOCK_TEST_URL
    assert "批量执行通知" in sent["content"]

    FakeWebhookClient.sent = []
    client.post(f"/items/batch_plan/{index}/complete")
    assert FakeWebhookClient.sent
    assert FakeWebhookClient.sent[-1]["url"] == MOCK_TEST_URL
    assert "批量完成通知" in FakeWebhookClient.sent[-1]["content"]

    assert "已关闭" in location(client.post("/batch-event-notify", data={"enabled": ""}))
    assert store.batch_event_notify_enabled(db_path) is False


def test_mask_webhook_url() -> None:
    assert web_module.mask_webhook_url("") == "未配置"
    assert web_module.mask_webhook_url(MOCK_PROD_URL) == (
        "http://mock.invalid/cgi-bin/webhook/send?key=MOCK****0001"
    )
    # Short keys are masked entirely.
    assert web_module.mask_webhook_url("https://x/send?key=abc") == "https://x/send?key=****"
    # URLs without a key parameter are truncated, not leaked in full.
    long_url = "https://example.com/some/very/long/webhook/path/without/key"
    masked = web_module.mask_webhook_url(long_url)
    assert "****" in masked and masked != long_url
