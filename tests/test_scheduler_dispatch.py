"""Scheduler dispatch: the webhook -> template -> schedule loop in `qwbot.cli`."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from qwbot import cli, store
from tests.conftest import MOCK_CUSTOM_URL, MOCK_PROD_URL, MOCK_TEST_URL, init_db, make_settings

BROKEN_URL = "http://mock.invalid/cgi-bin/webhook/send?key=broken"


class FakeWebhookClient:
    """Records sends; raises for URLs that contain the word `broken`."""

    sent: list[dict[str, Any]] = []

    def __init__(
        self,
        webhook_url: str,
        timeout_seconds: int = 10,
        *,
        allowed_hosts: Any = None,
        allow_local: bool = False,
        enforce_allowlist: bool = True,
    ) -> None:
        self.webhook_url = webhook_url

    def send_markdown(self, content: str) -> None:
        if "broken" in self.webhook_url:
            raise RuntimeError("WeCom webhook failed: {'errcode': 93000}")
        FakeWebhookClient.sent.append({"url": self.webhook_url, "content": content})

    def send_text(self, content: str, **kwargs: Any) -> None:
        FakeWebhookClient.sent.append({"url": self.webhook_url, "content": content})


@pytest.fixture(autouse=True)
def no_real_sends(monkeypatch: pytest.MonkeyPatch):
    FakeWebhookClient.sent = []
    monkeypatch.setattr(cli, "WeComWebhookClient", FakeWebhookClient)
    yield
    FakeWebhookClient.sent = []


class FrozenDatetime(datetime):
    """Pins `datetime.now()` so date rules can be tested deterministically."""

    frozen = datetime(2026, 9, 12, 9, 0)  # Saturday

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206 - mirrors datetime.now
        value = cls.frozen
        return value.replace(tzinfo=tz) if tz else value


def webhook_id_for(db_path: Path, target: str) -> int:
    return next(
        int(item["id"]) for item in store.list_webhooks(db_path) if item["target"] == target
    )


def add_template(
    db_path: Path,
    webhook_id: int,
    name: str,
    content: str,
    send_times: list[str],
    **kwargs: Any,
) -> int:
    return store.add_message_template(
        db_path, webhook_id, name, content, send_times=send_times, **kwargs
    )


def dispatch(settings, sent_keys: set[str], at_time: str) -> None:
    cli._send_due_notifications(settings, sent_keys, at_time=at_time)


def sent_to(url: str) -> list[str]:
    """Contents delivered to one webhook, ignoring the migrated built-in reminder."""
    return [item["content"] for item in FakeWebhookClient.sent if item["url"] == url]


def plan_entry(db_path: Path, send_time: str, template_name: str) -> dict[str, Any]:
    return next(
        entry
        for entry in store.load_dispatch_plan(db_path)
        if entry["send_time"] == send_time and entry["template_name"] == template_name
    )


def test_every_enabled_webhook_receives_its_own_template(db_path: Path, capsys) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    add_template(
        db_path, webhook_id_for(db_path, "prod"), "生产日报", "生产内容", ["09:00"], date_rule="all"
    )
    add_template(
        db_path, webhook_id_for(db_path, "test"), "自测日报", "自测内容", ["09:00"], date_rule="all"
    )

    dispatch(settings, set(), "09:00")

    assert "生产内容" in sent_to(MOCK_PROD_URL)
    assert "自测内容" in sent_to(MOCK_TEST_URL)

    out = capsys.readouterr().out
    assert "Sent: 生产群机器人 / 生产日报 at 09:00." in out
    assert "Sent: 自测群机器人 / 自测日报 at 09:00." in out


def test_one_template_with_several_times(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "多时段群", MOCK_CUSTOM_URL)
    add_template(
        db_path, webhook_id, "多时段", "内容", ["09:00", "12:00", "18:00"], date_rule="all"
    )

    dispatch(settings, set(), "12:00")
    assert sent_to(MOCK_CUSTOM_URL) == ["内容"]

    FakeWebhookClient.sent = []
    dispatch(settings, set(), "13:00")
    assert sent_to(MOCK_CUSTOM_URL) == []


def test_schedule_rule_overrides_template_rule(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "规则群", MOCK_CUSTOM_URL)
    template_id = add_template(
        db_path, webhook_id, "工作日模板", "内容", ["09:00"], date_rule="business_day"
    )
    # A weekend-capable time on the very same template.
    store.add_template_schedule(db_path, template_id, "10:00", date_rule="all")

    batch_plan = store.load_status_file(db_path)["batch_plan"]
    saturday = date(2026, 9, 12)
    assert cli.should_dispatch(plan_entry(db_path, "09:00", "工作日模板"), batch_plan, saturday) is False
    assert cli.should_dispatch(plan_entry(db_path, "10:00", "工作日模板"), batch_plan, saturday) is True


def test_broken_webhook_does_not_stop_the_others(db_path: Path, capsys) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    broken_id = store.add_webhook(db_path, "失效群", BROKEN_URL)
    healthy_id = store.add_webhook(db_path, "正常群", MOCK_CUSTOM_URL)
    add_template(db_path, broken_id, "失效模板", "坏内容", ["09:00"], date_rule="all")
    add_template(db_path, healthy_id, "正常模板", "好内容", ["09:00"], date_rule="all")

    dispatch(settings, set(), "09:00")

    urls = {item["url"] for item in FakeWebhookClient.sent}
    assert MOCK_CUSTOM_URL in urls
    assert BROKEN_URL not in urls
    assert sent_to(MOCK_CUSTOM_URL) == ["好内容"]
    out = capsys.readouterr().out
    assert "Failed: 失效群 / 失效模板" in out
    assert "Sent: 正常群 / 正常模板 at 09:00." in out


def test_webhook_without_url_is_skipped_with_a_warning(db_path: Path, capsys) -> None:
    settings = make_settings(db_path, webhook_url_prod=None, webhook_url_test=None)
    init_db(db_path, settings)
    webhook_id = int(store.list_webhooks(db_path)[0]["id"])
    # The seeded placeholder starts disabled; enabling it without an address must not
    # crash the dispatcher.
    store.toggle_webhook(db_path, webhook_id)
    add_template(db_path, webhook_id, "无地址模板", "内容", ["09:00"], date_rule="all")

    dispatch(settings, set(), "09:00")

    assert FakeWebhookClient.sent == []
    assert "has no URL configured" in capsys.readouterr().out


def test_disabled_levels_are_not_dispatched(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "群A", MOCK_CUSTOM_URL)
    template_id = add_template(db_path, webhook_id, "模板", "内容", ["09:00"], date_rule="all")

    store.toggle_message_template(db_path, template_id)
    dispatch(settings, set(), "09:00")
    assert sent_to(MOCK_CUSTOM_URL) == []

    store.toggle_message_template(db_path, template_id)
    store.toggle_webhook(db_path, webhook_id)
    dispatch(settings, set(), "09:00")
    assert sent_to(MOCK_CUSTOM_URL) == []

    store.toggle_webhook(db_path, webhook_id)
    schedule_id = int(store.list_template_schedules(db_path, template_id)[0]["id"])
    store.toggle_template_schedule(db_path, schedule_id)
    dispatch(settings, set(), "09:00")
    assert sent_to(MOCK_CUSTOM_URL) == []


def test_sent_keys_prevent_double_dispatch(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "群A", MOCK_CUSTOM_URL)
    add_template(db_path, webhook_id, "模板", "内容", ["09:00"], date_rule="all")

    sent_keys: set[str] = set()
    dispatch(settings, sent_keys, "09:00")
    assert sent_to(MOCK_CUSTOM_URL) == ["内容"]

    # The dispatcher ticks every 30s, so the same minute is visited twice.
    dispatch(settings, sent_keys, "09:00")
    assert sent_to(MOCK_CUSTOM_URL) == ["内容"]
    assert sent_keys


def test_failed_send_is_not_retried_within_the_same_minute(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "失效群", BROKEN_URL)
    add_template(db_path, webhook_id, "模板", "内容", ["09:00"], date_rule="all")

    sent_keys: set[str] = set()
    dispatch(settings, sent_keys, "09:00")
    dispatch(settings, sent_keys, "09:00")
    assert sent_keys, "the failed slot should be marked so it is not retried every 30s"


def test_date_rule_skips_weekends(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "群A", MOCK_CUSTOM_URL)
    add_template(db_path, webhook_id, "工作日模板", "内容", ["09:00"], date_rule="business_day")

    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    cli._send_due_notifications(settings, set())

    assert sent_to(MOCK_CUSTOM_URL) == []


def test_template_date_override_forces_a_weekend_send(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "群A", MOCK_CUSTOM_URL)
    template_id = add_template(
        db_path, webhook_id, "工作日模板", "内容", ["09:00"], date_rule="business_day"
    )
    store.set_template_date_override(db_path, template_id, "2026-09-12", "force")

    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    cli._send_due_notifications(settings, set())

    assert sent_to(MOCK_CUSTOM_URL) == ["内容"]


def test_at_all_is_appended_when_dispatching(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "群A", MOCK_CUSTOM_URL)
    add_template(db_path, webhook_id, "模板", "正文", ["09:00"], at_all="true", date_rule="all")

    dispatch(settings, set(), "09:00")

    assert sent_to(MOCK_CUSTOM_URL) == ["正文\n\n<@all>"]


def test_variables_are_rendered_at_dispatch_time(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    webhook_id = store.add_webhook(db_path, "群A", MOCK_CUSTOM_URL)
    add_template(
        db_path,
        webhook_id,
        "模板",
        "{title_date} · [进度]({progress_doc_link})",
        ["09:00"],
        date_rule="all",
    )

    dispatch(settings, set(), "09:00")

    contents = sent_to(MOCK_CUSTOM_URL)
    assert len(contents) == 1
    assert "{title_date}" not in contents[0]
    assert "https://doc.example/progress" in contents[0]


def test_scheduler_picks_up_new_webhooks_without_a_restart(db_path: Path) -> None:
    """The plan is re-read on every tick, so page edits apply immediately."""
    settings = make_settings(db_path)
    init_db(db_path, settings)

    dispatch(settings, set(), "09:00")
    assert MOCK_CUSTOM_URL not in {item["url"] for item in FakeWebhookClient.sent}

    webhook_id = store.add_webhook(db_path, "新群", MOCK_CUSTOM_URL)
    add_template(db_path, webhook_id, "新模板", "新内容", ["09:00"], date_rule="all")

    dispatch(settings, set(), "09:00")
    assert sent_to(MOCK_CUSTOM_URL) == ["新内容"]
