from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import Flask, redirect, render_template, request, url_for

from qwbot.config import Settings
from qwbot.message import (
    build_batch_complete_notice,
    build_batch_start_notice,
    render_message_template,
)
from qwbot.planner import (
    active_batch_items,
    is_business_day,
    is_completed,
    next_batch_after,
    sort_key,
)
from qwbot.store import (
    DEFAULT_REMINDER_TEMPLATE,
    REMINDER_TEMPLATE_VARS,
    WEBHOOK_TARGETS,
    WEBHOOK_TARGET_LABELS,
    add_block_event,
    add_item,
    add_message_template,
    add_scheduler_force_date,
    add_scheduler_skip_date,
    add_template_schedule,
    add_template_var,
    add_webhook,
    check_duplicate_var_value,
    close_open_block,
    delete_item,
    delete_message_template,
    delete_scheduler_force_date,
    delete_scheduler_skip_date,
    delete_template_date_override,
    delete_template_schedule,
    delete_template_var,
    delete_webhook,
    duplicate_message_template,
    get_item,
    get_message_template,
    get_webhook,
    has_open_block,
    import_env_webhooks,
    init_store,
    list_template_schedules,
    list_webhooks,
    load_status_file,
    load_webhook_tree,
    resolve_default_webhook_url,
    resolve_webhook_url,
    set_default_webhook,
    set_template_date_override,
    toggle_message_template,
    toggle_template_schedule,
    toggle_webhook,
    update_item,
    update_message_template,
    update_open_block_reason,
    update_template_schedule,
    update_template_var,
    update_webhook,
)
from qwbot.wecom import WeComWebhookClient

DATE_RULE_LABELS = {
    "all": "所有自然日",
    "business_day": "跳过周末和节假日",
    "holiday_only": "仅跳过节假日",
}
CONNECTIVITY_TEST_TEXT = "企业微信机器人连通性测试成功。"
WEBHOOK_FORM_TARGETS = WEBHOOK_TARGETS


def create_app(settings: Settings) -> Flask:
    app = Flask(__name__)
    status_file = settings.db_path
    init_store(
        status_file,
        settings.local_status_file,
        webhook_seeds=settings.webhook_seeds(),
        default_target=settings.default_seed_target(),
    )

    @app.get("/")
    def index():
        payload = load_status_file(status_file)
        _archive_completed_from_previous_days(status_file, payload, settings.timezone)
        view = request.args.get("view", "active")
        filters = _archive_filters_from_request()
        if view == "archived" and not _has_archive_filter_args():
            filters = _default_archive_filters(payload["batch_plan"], settings.timezone)
        all_archived_entries = _batch_items_for_view(
            payload["batch_plan"],
            "archived",
            timezone=settings.timezone,
        )
        archived_entries = _batch_items_for_view(
            payload["batch_plan"],
            "archived",
            filters,
            settings.timezone,
        )
        active_entries = _batch_items_for_view(
            payload["batch_plan"],
            "active",
            timezone=settings.timezone,
        )
        pagination = _paginate_entries(
            archived_entries if view == "archived" else active_entries,
            page=_int_arg("page", 1),
            per_page=10 if view == "archived" else 1000,
        )
        return render_template(
            "index.html",
            batch_plan=pagination["items"],
            blocked_plan=_blocked_items(payload["batch_plan"], settings.timezone),
            active_count=len(active_entries),
            archived_count=len(all_archived_entries),
            archive_filters=filters,
            pagination=pagination,
            today=_today_iso(settings.timezone),
            view=view,
            status_file=_display_path(status_file),
        )

    @app.get("/notifications")
    def notification_page():
        payload = load_status_file(status_file)
        return render_template(
            "notifications.html",
            webhook_tree=_webhook_tree_for_view(status_file),
            webhook_count=len(list_webhooks(status_file)),
            reminder_dates=_reminder_date_entries(
                payload["batch_plan"],
                payload["scheduler_skip_dates"],
                payload["scheduler_force_dates"],
                settings.timezone,
            ),
            current_hour=_current_hour(settings.timezone),
            current_time=_current_time(settings.timezone),
            default_reminder_template=DEFAULT_REMINDER_TEMPLATE,
            reminder_template_vars=REMINDER_TEMPLATE_VARS,
            custom_template_vars=payload.get("template_vars", []),
            date_rule_labels=DATE_RULE_LABELS,
            webhook_targets=WEBHOOK_FORM_TARGETS,
            webhook_target_labels=WEBHOOK_TARGET_LABELS,
        )

    @app.post("/items/<collection>")
    def create_item(collection: str):
        add_item(status_file, collection, _item_from_form())
        return redirect(url_for("index"))

    @app.post("/items/<collection>/<int:index>")
    def edit_item(collection: str, index: int):
        existing_item = get_item(status_file, collection, index)
        if collection == "batch_plan" and _is_archived(existing_item):
            return redirect(url_for("index", view="archived"))
        if collection == "batch_plan" and not _can_edit_item(existing_item):
            return redirect(url_for("index"))
        item = _item_from_form()
        _apply_completion_metadata(existing_item, item, settings.timezone)
        update_item(status_file, collection, index, item)
        if collection == "batch_plan":
            _sync_block_events(status_file, index, existing_item, item, settings.timezone)
        return redirect(url_for("index"))

    @app.post("/items/<collection>/<int:index>/delete")
    def remove_item(collection: str, index: int):
        if collection == "batch_plan":
            item = get_item(status_file, collection, index)
            if _is_archived(item):
                return redirect(url_for("index", view="archived"))
            if not _can_delete_item(item):
                return redirect(url_for("index"))
        delete_item(status_file, collection, index)
        return redirect(url_for("index"))

    @app.post("/items/batch_plan/<int:index>/archive")
    def archive_item(index: int):
        item = get_item(status_file, "batch_plan", index)
        if not is_completed(item):
            return redirect(url_for("index"))
        item["archived"] = "true"
        item["archived_on"] = _today_iso(settings.timezone)
        update_item(status_file, "batch_plan", index, item)
        return redirect(url_for("index"))

    @app.post("/items/batch_plan/<int:index>/start")
    def start_item(index: int):
        payload = load_status_file(status_file)
        item = get_item(status_file, "batch_plan", index)
        if _is_archived(item):
            return redirect(url_for("index", view="archived"))
        if not _can_start_batch(payload["batch_plan"], index):
            return redirect(url_for("index"))

        item["execution_status"] = "进行中"
        item["started_on"] = _today_iso(settings.timezone)
        item["started_at"] = _now_iso(settings.timezone)
        update_item(status_file, "batch_plan", index, item)
        _send_batch_notice(settings, status_file, build_batch_start_notice(item), mention_all=True)
        return redirect(url_for("index"))

    @app.post("/items/batch_plan/<int:index>/complete")
    def complete_item(index: int):
        payload = load_status_file(status_file)
        item = get_item(status_file, "batch_plan", index)
        if _is_archived(item):
            return redirect(url_for("index", view="archived"))
        if not _can_complete_batch(payload["batch_plan"], index):
            return redirect(url_for("index"))

        item["execution_status"] = "已完成"
        item["completed_on"] = _today_iso(settings.timezone)
        item["completed_at"] = _now_iso(settings.timezone)
        item["execution_seconds"] = str(_duration_seconds(item.get("started_at"), item["completed_at"]))
        next_entry = next_batch_after(payload["batch_plan"], index)
        update_item(status_file, "batch_plan", index, item)
        _send_batch_notice(
            settings,
            status_file,
            build_batch_complete_notice(item, next_entry[1] if next_entry else None),
        )
        return redirect(url_for("index"))

    @app.post("/items/batch_plan/<int:index>/close-block")
    def close_block(index: int):
        item = get_item(status_file, "batch_plan", index)
        if _is_archived(item) or _execution_status(item) != "有阻塞":
            return redirect(url_for("index"))

        close_open_block(status_file, index, _now_iso(settings.timezone))
        item["execution_status"] = "进行中"
        item["block_reason"] = ""
        update_item(status_file, "batch_plan", index, item)
        return redirect(url_for("index"))

    # ------------------------------------------------------------------
    # Webhooks (QWB-002): webhook -> message template -> notification time
    # ------------------------------------------------------------------

    @app.post("/webhooks")
    def create_webhook():
        name = request.form.get("name", "").strip()
        url = request.form.get("url", "").strip()
        if not name:
            return _notification_redirect(error="请填写机器人名称")
        if not url:
            return _notification_redirect(error="请填写机器人 webhook 地址")
        if not _is_valid_webhook_url(url):
            return _notification_redirect(error="webhook 地址必须以 http:// 或 https:// 开头")
        duplicate = _find_webhook_by_url(status_file, url)
        if duplicate:
            return _notification_redirect(error=f"该 webhook 地址已存在于「{duplicate['name']}」")
        webhook_id = add_webhook(
            status_file,
            name,
            url,
            target=request.form.get("target", "custom"),
            remark=request.form.get("remark", ""),
            is_enabled=_flag("is_enabled", True),
            is_default=_flag("is_default"),
        )
        send_time = _send_time_from_form()
        if send_time:
            template_id = add_message_template(
                status_file,
                webhook_id,
                f"{name} 默认模板",
                DEFAULT_REMINDER_TEMPLATE,
                send_times=[send_time],
            )
            return _notification_redirect(
                notice=f"已新增机器人「{name}」和默认模板", anchor=f"template-{template_id}"
            )
        return _notification_redirect(notice=f"已新增机器人「{name}」", anchor=f"webhook-{webhook_id}")

    @app.post("/webhooks/import-env")
    def import_webhooks_from_env():
        added = import_env_webhooks(
            status_file,
            settings.webhook_seeds(),
            settings.default_seed_target(),
        )
        if added:
            return _notification_redirect(notice=f"已从 .env 导入 {added} 个机器人")
        return _notification_redirect(notice=".env 中没有新的 webhook 需要导入")

    @app.post("/webhooks/<int:webhook_id>")
    def edit_webhook(webhook_id: int):
        webhook = get_webhook(status_file, webhook_id)
        if webhook is None:
            return _notification_redirect(error="机器人不存在")
        name = request.form.get("name", "").strip()
        submitted_url = request.form.get("url", "").strip()
        if not name:
            return _notification_redirect(error="请填写机器人名称")
        if submitted_url and not _is_valid_webhook_url(submitted_url):
            return _notification_redirect(error="webhook 地址必须以 http:// 或 https:// 开头")
        # The edit form never renders the stored key, so an empty field means
        # "keep the current URL" unless the operator explicitly asks to clear it.
        if submitted_url:
            url = submitted_url
        elif _flag("clear_url"):
            url = ""
        else:
            url = webhook["url"]
        duplicate = _find_webhook_by_url(status_file, url, exclude_id=webhook_id)
        if duplicate:
            return _notification_redirect(error=f"该 webhook 地址已存在于「{duplicate['name']}」")
        update_webhook(
            status_file,
            webhook_id,
            name,
            url,
            target=request.form.get("target", webhook["target"]),
            remark=request.form.get("remark", ""),
            is_enabled=_flag("is_enabled"),
        )
        return _notification_redirect(notice=f"已保存机器人「{name}」", anchor=f"webhook-{webhook_id}")

    @app.post("/webhooks/<int:webhook_id>/delete")
    def remove_webhook(webhook_id: int):
        webhook = get_webhook(status_file, webhook_id)
        if webhook is None:
            return _notification_redirect(error="机器人不存在")
        try:
            delete_webhook(status_file, webhook_id)
        except ValueError as exc:
            return _notification_redirect(error=str(exc))
        return _notification_redirect(notice=f"已删除机器人「{webhook['name']}」及其模板和时间")

    @app.post("/webhooks/<int:webhook_id>/toggle")
    def toggle_webhook_state(webhook_id: int):
        if get_webhook(status_file, webhook_id) is None:
            return _notification_redirect(error="机器人不存在")
        enabled = toggle_webhook(status_file, webhook_id)
        return _notification_redirect(
            notice="已启用机器人" if enabled else "已停用机器人",
            anchor=f"webhook-{webhook_id}",
        )

    @app.post("/webhooks/<int:webhook_id>/set-default")
    def mark_default_webhook(webhook_id: int):
        webhook = get_webhook(status_file, webhook_id)
        if webhook is None:
            return _notification_redirect(error="机器人不存在")
        set_default_webhook(status_file, webhook_id)
        return _notification_redirect(
            notice=f"已将「{webhook['name']}」设为默认机器人", anchor=f"webhook-{webhook_id}"
        )

    @app.post("/webhooks/<int:webhook_id>/test")
    def test_webhook(webhook_id: int):
        webhook = get_webhook(status_file, webhook_id)
        if webhook is None:
            return _notification_redirect(error="机器人不存在")
        url = resolve_webhook_url(webhook, settings)
        if not url:
            return _notification_redirect(
                error=f"机器人「{webhook['name']}」没有可用的 webhook 地址",
                anchor=f"webhook-{webhook_id}",
            )
        try:
            WeComWebhookClient(url).send_text(CONNECTIVITY_TEST_TEXT)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator in the UI
            return _notification_redirect(
                error=f"机器人「{webhook['name']}」连通性测试失败：{exc}",
                anchor=f"webhook-{webhook_id}",
            )
        return _notification_redirect(
            notice=f"机器人「{webhook['name']}」连通性测试成功",
            anchor=f"webhook-{webhook_id}",
        )

    @app.post("/webhooks/<int:webhook_id>/templates")
    def create_message_template(webhook_id: int):
        webhook = get_webhook(status_file, webhook_id)
        if webhook is None:
            return _notification_redirect(error="机器人不存在")
        name = request.form.get("name", "").strip()
        content = request.form.get("content", "")
        if not name:
            return _notification_redirect(error="请填写模板名称", anchor=f"webhook-{webhook_id}")
        send_times = _send_times_from_form()
        template_id = add_message_template(
            status_file,
            webhook_id,
            name,
            content or DEFAULT_REMINDER_TEMPLATE,
            at_all=_flag("at_all"),
            date_rule=_date_rule_from_form(),
            is_enabled=_flag("is_enabled", True),
            send_times=send_times,
        )
        return _notification_redirect(
            notice=f"已新增模板「{name}」", anchor=f"template-{template_id}"
        )

    @app.post("/templates/<int:template_id>")
    def edit_message_template(template_id: int):
        template = get_message_template(status_file, template_id)
        if template is None:
            return _notification_redirect(error="消息模板不存在")
        name = request.form.get("name", "").strip()
        if not name:
            return _notification_redirect(error="请填写模板名称", anchor=f"template-{template_id}")
        update_message_template(
            status_file,
            template_id,
            name,
            request.form.get("content", ""),
            at_all=_flag("at_all"),
            date_rule=_date_rule_from_form(),
            is_enabled=_flag("is_enabled"),
        )
        return _notification_redirect(notice=f"已保存模板「{name}」", anchor=f"template-{template_id}")

    @app.post("/templates/<int:template_id>/delete")
    def remove_message_template(template_id: int):
        template = get_message_template(status_file, template_id)
        if template is None:
            return _notification_redirect(error="消息模板不存在")
        delete_message_template(status_file, template_id)
        return _notification_redirect(
            notice=f"已删除模板「{template['name']}」及其通知时间",
            anchor=f"webhook-{template['webhook_id']}",
        )

    @app.post("/templates/<int:template_id>/toggle")
    def toggle_message_template_state(template_id: int):
        template = get_message_template(status_file, template_id)
        if template is None:
            return _notification_redirect(error="消息模板不存在")
        enabled = toggle_message_template(status_file, template_id)
        return _notification_redirect(
            notice=f"模板「{template['name']}」已启用" if enabled else f"模板「{template['name']}」已停用",
            anchor=f"template-{template_id}",
        )

    @app.post("/templates/<int:template_id>/duplicate")
    def copy_message_template(template_id: int):
        template = get_message_template(status_file, template_id)
        if template is None:
            return _notification_redirect(error="消息模板不存在")
        new_id = duplicate_message_template(status_file, template_id)
        return _notification_redirect(
            notice=f"已复制模板「{template['name']}」（副本默认停用）", anchor=f"template-{new_id}"
        )

    @app.post("/templates/<int:template_id>/test-send")
    def test_send_message_template(template_id: int):
        template = get_message_template(status_file, template_id)
        if template is None:
            return _notification_redirect(error="消息模板不存在")
        webhook = get_webhook(status_file, int(template["webhook_id"]))
        url = resolve_webhook_url(webhook, settings)
        if not url:
            return _notification_redirect(
                error=f"机器人「{webhook['name'] if webhook else ''}」没有可用的 webhook 地址",
                anchor=f"template-{template_id}",
            )
        payload = load_status_file(status_file)
        preview = dict(template)
        # Allow testing unsaved edits straight from the editor modal.
        form_content = request.form.get("content", "")
        if form_content.strip():
            preview["content"] = form_content
        if "at_all" in request.form:
            preview["at_all"] = _flag("at_all")
        message = render_message_template(
            settings, preview, payload.get("template_vars", []), _next_active_batch(payload)
        )
        try:
            WeComWebhookClient(url).send_markdown(message)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator in the UI
            return _notification_redirect(
                error=f"测试发送失败：{exc}", anchor=f"template-{template_id}"
            )
        return _notification_redirect(
            notice=f"已测试发送到「{webhook['name'] if webhook else '机器人'}」",
            anchor=f"template-{template_id}",
        )

    @app.post("/templates/<int:template_id>/schedules")
    def create_template_schedule(template_id: int):
        template = get_message_template(status_file, template_id)
        if template is None:
            return _notification_redirect(error="消息模板不存在")
        send_time = _send_time_from_form()
        if not send_time:
            return _notification_redirect(error="请选择有效的通知时间", anchor=f"template-{template_id}")
        try:
            schedule_id = add_template_schedule(
                status_file,
                template_id,
                send_time,
                date_rule=request.form.get("date_rule", ""),
                is_enabled=_flag("is_enabled", True),
            )
        except ValueError as exc:
            return _notification_redirect(error=str(exc), anchor=f"template-{template_id}")
        return _notification_redirect(
            notice=f"已新增通知时间 {send_time}", anchor=f"schedule-{schedule_id}"
        )

    @app.post("/schedules/<int:schedule_id>")
    def edit_template_schedule(schedule_id: int):
        schedule = _find_schedule(status_file, schedule_id)
        if schedule is None:
            return _notification_redirect(error="通知时间不存在")
        send_time = _send_time_from_form()
        if not send_time:
            return _notification_redirect(
                error="请选择有效的通知时间", anchor=f"template-{schedule['template_id']}"
            )
        try:
            update_template_schedule(
                status_file,
                schedule_id,
                send_time,
                date_rule=(
                    request.form["date_rule"]
                    if "date_rule" in request.form
                    else schedule["date_rule"]
                ),
            )
        except ValueError as exc:
            return _notification_redirect(error=str(exc), anchor=f"template-{schedule['template_id']}")
        return _notification_redirect(
            notice=f"通知时间已更新为 {send_time}", anchor=f"template-{schedule['template_id']}"
        )

    @app.post("/schedules/<int:schedule_id>/delete")
    def remove_template_schedule(schedule_id: int):
        schedule = _find_schedule(status_file, schedule_id)
        if schedule is None:
            return _notification_redirect(error="通知时间不存在")
        delete_template_schedule(status_file, schedule_id)
        return _notification_redirect(
            notice="已删除通知时间", anchor=f"template-{schedule['template_id']}"
        )

    @app.post("/schedules/<int:schedule_id>/toggle")
    def toggle_template_schedule_state(schedule_id: int):
        schedule = _find_schedule(status_file, schedule_id)
        if schedule is None:
            return _notification_redirect(error="通知时间不存在")
        enabled = toggle_template_schedule(status_file, schedule_id)
        return _notification_redirect(
            notice="通知时间已启用" if enabled else "通知时间已停用",
            anchor=f"template-{schedule['template_id']}",
        )

    @app.post("/templates/<int:template_id>/skip-date")
    def skip_template_date(template_id: int):
        set_template_date_override(
            status_file, template_id, request.form.get("target_date", ""), "skip"
        )
        return _notification_redirect(anchor=f"template-{template_id}")

    @app.post("/templates/<int:template_id>/force-date")
    def force_template_date(template_id: int):
        set_template_date_override(
            status_file, template_id, request.form.get("target_date", ""), "force"
        )
        return _notification_redirect(anchor=f"template-{template_id}")

    @app.post("/templates/<int:template_id>/date-overrides/delete")
    def remove_template_date_override(template_id: int):
        delete_template_date_override(
            status_file, template_id, request.form.get("target_date", "")
        )
        return _notification_redirect(anchor=f"template-{template_id}")

    @app.post("/scheduler-skip-dates")
    def create_scheduler_skip_date():
        add_scheduler_skip_date(status_file, request.form.get("skip_date", ""))
        return redirect(url_for("notification_page"))

    @app.post("/scheduler-skip-dates/delete")
    def remove_scheduler_skip_date():
        delete_scheduler_skip_date(status_file, request.form.get("skip_date", ""))
        return redirect(url_for("notification_page"))

    @app.post("/scheduler-force-dates")
    def create_scheduler_force_date():
        add_scheduler_force_date(status_file, request.form.get("force_date", ""))
        return redirect(url_for("notification_page"))

    @app.post("/scheduler-force-dates/delete")
    def remove_scheduler_force_date():
        delete_scheduler_force_date(status_file, request.form.get("force_date", ""))
        return redirect(url_for("notification_page"))

    @app.post("/template-vars")
    def create_template_var():
        var_name = request.form.get("var_name", "").strip()
        var_label = request.form.get("var_label", "").strip()
        var_value = request.form.get("var_value", "").strip()
        if var_name:
            if var_value:
                duplicate = check_duplicate_var_value(status_file, var_value)
                if duplicate:
                    return _notification_redirect(error=f"链接已存在于变量 {duplicate}")
            add_template_var(status_file, var_name, var_label, var_value)
        return redirect(url_for("notification_page"))

    @app.post("/template-vars/<int:var_id>")
    def edit_template_var(var_id: int):
        var_name = request.form.get("var_name", "").strip()
        var_label = request.form.get("var_label", "").strip()
        var_value = request.form.get("var_value", "").strip()
        if var_name:
            if var_value:
                duplicate = check_duplicate_var_value(status_file, var_value, exclude_id=var_id)
                if duplicate:
                    return _notification_redirect(error=f"链接已存在于变量 {duplicate}")
            update_template_var(status_file, var_id, var_name, var_label, var_value)
        return redirect(url_for("notification_page"))

    @app.post("/template-vars/<int:var_id>/delete")
    def remove_template_var(var_id: int):
        delete_template_var(status_file, var_id)
        return redirect(url_for("notification_page"))

    return app


def _notification_redirect(
    error: str | None = None,
    notice: str | None = None,
    anchor: str | None = None,
):
    args: dict[str, str] = {}
    if error:
        args["error"] = error
    if notice:
        args["notice"] = notice
    response = redirect(url_for("notification_page", **args))
    if anchor:
        response.headers["Location"] = f"{response.headers['Location']}#{anchor}"
    return response


def _send_batch_notice(
    settings: Settings,
    status_file: Path,
    message: str,
    mention_all: bool = False,
) -> None:
    """Batch start/complete notices always go to the default webhook."""
    url = resolve_default_webhook_url(status_file, settings)
    if not url:
        return
    client = WeComWebhookClient(url)
    if mention_all:
        client.send_text(message, mentioned_list=["@all"])
    else:
        client.send_text(message)


def _next_active_batch(payload: dict[str, Any]) -> dict[str, str] | None:
    return next(
        (item for _, item in active_batch_items(payload["batch_plan"]) if not is_completed(item)),
        None,
    )


def mask_webhook_url(url: str) -> str:
    """Hide the middle of the webhook key so it is safe to show on screen."""
    value = (url or "").strip()
    if not value:
        return "未配置"
    match = re.search(r"([?&]key=)([^&#]+)", value)
    if match:
        key = match.group(2)
        masked = "****" if len(key) <= 8 else f"{key[:4]}****{key[-4:]}"
        return f"{value[: match.start(2)]}{masked}{value[match.end(2):]}"
    if len(value) <= 24:
        return value
    return f"{value[:16]}****{value[-6:]}"


def _is_valid_webhook_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _find_webhook_by_url(
    path: Path,
    url: str,
    exclude_id: int | None = None,
) -> dict[str, Any] | None:
    if not url:
        return None
    for webhook in list_webhooks(path):
        if exclude_id is not None and int(webhook["id"]) == exclude_id:
            continue
        if webhook["url"] == url:
            return webhook
    return None


def _find_schedule(path: Path, schedule_id: int) -> dict[str, Any] | None:
    for schedule in list_template_schedules(path):
        if int(schedule["id"]) == schedule_id:
            return schedule
    return None


def _webhook_tree_for_view(path: Path) -> list[dict[str, Any]]:
    tree = load_webhook_tree(path)
    for webhook in tree:
        webhook["masked_url"] = mask_webhook_url(webhook["url"])
        webhook["has_url"] = bool(webhook["url"])
        for template in webhook["templates"]:
            template["date_rule_label"] = DATE_RULE_LABELS.get(template["date_rule"], "")
            template["enabled_times"] = ", ".join(
                schedule["send_time"]
                for schedule in template["schedules"]
                if schedule["is_enabled"]
            ) or "未配置"
            template["override_dates"] = [
                {"date": value, "mode": "skip"} for value in template["skip_dates"]
            ] + [{"date": value, "mode": "force"} for value in template["force_dates"]]
    return tree


def _item_from_form() -> dict[str, str]:
    return {
        "date": request.form.get("date", ""),
        "category": request.form.get("category", ""),
        "content": request.form.get("content", ""),
        "owner": request.form.get("owner", ""),
        "status": request.form.get("status", ""),
        "natural_date": request.form.get("natural_date", ""),
        "current_accounting_date": request.form.get("current_accounting_date", ""),
        "next_accounting_date": request.form.get("next_accounting_date", ""),
        "holiday_flag": request.form.get("holiday_flag", ""),
        "description": request.form.get("description", ""),
        "requester": request.form.get("requester", ""),
        "execution_status": request.form.get("execution_status", ""),
        "block_reason": request.form.get("block_reason", ""),
        "batch_start_time": request.form.get("batch_start_time", ""),
        "started_on": request.form.get("started_on", ""),
    }


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _archive_filters_from_request() -> dict[str, str]:
    return {
        "natural_date": request.args.get("natural_date", "").strip(),
        "accounting_date": request.args.get("accounting_date", "").strip(),
        "holiday_flag": request.args.get("holiday_flag", "").strip(),
    }


def _has_archive_filter_args() -> bool:
    return any(name in request.args for name in ("natural_date", "accounting_date", "holiday_flag"))


def _default_archive_filters(
    items: list[dict[str, object]],
    timezone: str = "Asia/Shanghai",
) -> dict[str, str]:
    archived_dates = sorted(
        {
            str(item.get("natural_date") or item.get("date") or "")
            for item in items
            if _is_archived(item) and (item.get("natural_date") or item.get("date"))
        }
    )
    if not archived_dates:
        return {"natural_date": "", "accounting_date": "", "holiday_flag": ""}

    today = _today_iso(timezone)
    natural_date = today if today in archived_dates else archived_dates[-1]
    return {"natural_date": natural_date, "accounting_date": "", "holiday_flag": ""}


def _flag(name: str, default: bool = False) -> bool:
    """Checkbox state that tells "not submitted" apart from "submitted unchecked".

    Every checkbox in the maintenance forms is paired with a hidden input carrying
    an empty value, so an unchecked box arrives as `[""]` while a missing field
    arrives as `[]`. Programmatic posts that omit the field therefore keep the
    `default` instead of silently disabling the row.
    """
    values = request.form.getlist(name)
    if not values:
        return default
    return any(value == "true" for value in values)


def _send_time_from_form() -> str:
    """Normalize the `send_time` form field to HH:MM, empty string when invalid."""
    return _normalize_send_time(request.form.get("send_time", ""))


def _send_times_from_form() -> list[str]:
    """Normalize every `send_time` value submitted (a template may have several)."""
    times: list[str] = []
    for raw in request.form.getlist("send_time"):
        value = _normalize_send_time(raw)
        if value and value not in times:
            times.append(value)
    return times


def _normalize_send_time(value: object) -> str:
    parts = str(value or "").strip().split(":")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        hour = max(0, min(23, int(parts[0])))
        minute = max(0, min(59, int(parts[1])))
        return f"{hour:02d}:{minute:02d}"
    return ""


def _current_hour(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).strftime("%H")


def _current_time(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).strftime("%H:%M")


def _reminder_date_entries(
    batch_plan: list[dict[str, str]],
    skip_dates: list[dict[str, str]],
    force_dates: list[dict[str, str]],
    timezone: str,
    days: int = 10,
) -> list[dict[str, object]]:
    today = datetime.now(ZoneInfo(timezone)).date()
    skipped = {item["date"] for item in skip_dates}
    forced = {item["date"] for item in force_dates}
    entries = []
    for offset in range(days):
        current = today + timedelta(days=offset)
        current_text = current.isoformat()
        default_remind = is_business_day(batch_plan, current)
        is_skipped = current_text in skipped
        is_forced = current_text in forced
        will_remind = is_forced or (default_remind and not is_skipped)
        entries.append(
            {
                "date": current_text,
                "day": current.strftime("%m-%d"),
                "weekday": _weekday_label(current.weekday()),
                "is_today": offset == 0,
                "is_skipped": not will_remind,
                "is_forced": is_forced,
                "default_remind": default_remind,
                "will_remind": will_remind,
            }
        )
    return entries


def _date_rule_from_form() -> str:
    return _normalize_date_rule(request.form.get("date_rule", ""))


def _normalize_date_rule(value: str) -> str:
    return value if value in DATE_RULE_LABELS else "business_day"


def _weekday_label(weekday: int) -> str:
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][weekday]


def _int_arg(name: str, default: int) -> int:
    try:
        return max(1, int(request.args.get(name, default)))
    except (TypeError, ValueError):
        return default


def _batch_items_for_view(
    items: list[dict[str, object]],
    view: str,
    filters: dict[str, str] | None = None,
    timezone: str = "Asia/Shanghai",
) -> list[dict[str, object]]:
    archived = view == "archived"
    indexed_items = [
        _entry_for_item(item)
        for item in items
        if _is_archived(item) is archived and _matches_archive_filters(item, filters)
    ]
    sorted_items = sorted(indexed_items, key=lambda entry: sort_key(entry["item"]))
    if not archived:
        _attach_execution_permissions(sorted_items)
    _attach_metrics(sorted_items, timezone)
    return sorted_items


def _entry_for_item(item: dict[str, object]) -> dict[str, object]:
    return {"index": int(item.get("id") or 0), "item": item}


def _matches_archive_filters(item: dict[str, object], filters: dict[str, str] | None) -> bool:
    if not filters:
        return True
    natural_date = str(item.get("natural_date") or item.get("date") or "")
    accounting_date = str(item.get("current_accounting_date") or "")
    holiday_flag = str(item.get("holiday_flag") or "N")
    return (
        (not filters.get("natural_date") or natural_date == filters["natural_date"])
        and (not filters.get("accounting_date") or accounting_date == filters["accounting_date"])
        and (not filters.get("holiday_flag") or holiday_flag == filters["holiday_flag"])
    )


def _paginate_entries(
    entries: list[dict[str, object]],
    *,
    page: int,
    per_page: int,
) -> dict[str, object]:
    total = len(entries)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), total_pages)
    start = (page - 1) * per_page
    return {
        "items": entries[start : start + per_page],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1,
        "next_page": page + 1,
        "start_index": start,
    }


def _is_archived(item: dict[str, object]) -> bool:
    return item.get("archived") == "true"


def _blocked_items(items: list[dict[str, object]], timezone: str = "Asia/Shanghai") -> list[dict[str, object]]:
    current_item = _current_running_or_blocked_item(items)
    if not current_item:
        return []

    events = list(current_item.get("block_events") or [])
    if not events and current_item.get("block_reason"):
        events = [
            {
                "id": "",
                "reason": current_item.get("block_reason"),
                "started_at": "",
                "ended_at": "",
                "duration_seconds": 0,
            }
        ]

    return sorted(
        [
            _block_event_entry(
                batch_index=int(current_item.get("id") or 0),
                event=event,
                timezone=timezone,
            )
            for event in events
        ],
        key=lambda entry: entry["sort_key"],
        reverse=True,
    )


def _current_running_or_blocked_item(items: list[dict[str, object]]) -> dict[str, object] | None:
    candidates = [
        item
        for item in items
        if not _is_archived(item) and _execution_status(item) in {"进行中", "有阻塞"}
    ]
    if not candidates:
        return None
    return sorted(candidates, key=sort_key)[0]


def _block_event_entry(
    batch_index: int,
    event: dict[str, object],
    timezone: str = "Asia/Shanghai",
) -> dict[str, object]:
    is_open = not event.get("ended_at")
    duration_seconds = (
        _duration_seconds(event.get("started_at"), _now_iso(timezone))
        if is_open
        else int(event.get("duration_seconds") or 0)
    )
    return {
        "batch_index": batch_index,
        "event": event,
        "is_open": is_open,
        "row_class": "blocked-row-open" if is_open else "blocked-row-closed",
        "status_label": "未关闭" if is_open else "已关闭",
        "registered_at": _format_datetime(event.get("started_at")),
        "reason": event.get("reason") or "未填写",
        "duration": _format_duration(duration_seconds),
        "sort_key": str(event.get("started_at") or ""),
    }


def _attach_execution_permissions(entries: list[dict[str, object]]) -> None:
    running_index = next(
        (
            entry["index"]
            for entry in entries
            if _execution_status(entry["item"]) == "进行中"
        ),
        None,
    )
    first_unfinished_index = next(
        (
            entry["index"]
            for entry in entries
            if not is_completed(entry["item"])
        ),
        None,
    )

    for entry in entries:
        item = entry["item"]
        status = _execution_status(item)
        entry["can_start"] = (
            running_index is None
            and entry["index"] == first_unfinished_index
            and status == "待执行"
        )
        entry["can_complete"] = running_index == entry["index"] and status == "进行中"
        entry["can_archive"] = status == "已完成"
        entry["can_edit"] = _can_edit_item(item)
        entry["can_delete"] = _can_delete_item(item)
        entry["is_sequence_blocked"] = (
            running_index is None
            and entry["index"] == first_unfinished_index
            and status == "有阻塞"
        )


def _can_start_batch(items: list[dict[str, str]], index: int) -> bool:
    entries = _batch_items_for_view(items, "active")
    return any(entry["index"] == index and entry.get("can_start") for entry in entries)


def _can_complete_batch(items: list[dict[str, str]], index: int) -> bool:
    entries = _batch_items_for_view(items, "active")
    return any(entry["index"] == index and entry.get("can_complete") for entry in entries)


def _execution_status(item: dict[str, str]) -> str:
    return item.get("execution_status") or item.get("status") or "待执行"


def _can_edit_item(item: dict[str, object]) -> bool:
    return _execution_status(item) in {"待执行", "进行中", "有阻塞"}


def _can_delete_item(item: dict[str, object]) -> bool:
    return _execution_status(item) == "待执行"


def _apply_completion_metadata(
    existing_item: dict[str, str],
    item: dict[str, str],
    timezone: str = "Asia/Shanghai",
) -> None:
    status = item.get("execution_status")
    existing_status = existing_item.get("execution_status") or existing_item.get("status")
    if status == "已完成":
        completed_at = existing_item.get("completed_at") or _now_iso(timezone)
        item["completed_on"] = existing_item.get("completed_on") or _today_iso(timezone)
        item["completed_at"] = completed_at
        item["execution_seconds"] = str(_duration_seconds(existing_item.get("started_at"), completed_at))
    elif existing_status == "已完成":
        item["completed_on"] = ""
        item["completed_at"] = ""
        item["execution_seconds"] = "0"


def _sync_block_events(
    status_file: Path,
    index: int,
    existing_item: dict[str, object],
    item: dict[str, str],
    timezone: str = "Asia/Shanghai",
) -> None:
    old_status = _execution_status(existing_item)
    new_status = _execution_status(item)
    if new_status == "有阻塞":
        reason = item.get("block_reason", "")
        if old_status != "有阻塞" or not has_open_block(status_file, index):
            add_block_event(status_file, index, reason, _now_iso(timezone))
        else:
            update_open_block_reason(status_file, index, reason)
        return

    if old_status == "有阻塞":
        close_open_block(status_file, index, _now_iso(timezone))


def _attach_metrics(entries: list[dict[str, object]], timezone: str = "Asia/Shanghai") -> None:
    for entry in entries:
        item = entry["item"]
        block_total = int(item.get("block_total_seconds") or 0)
        execution_seconds = int(item.get("execution_seconds") or 0)
        item["block_total_duration"] = _format_duration(block_total)
        item["execution_duration"] = _format_duration(execution_seconds)
        item["completed_time_display"] = _format_time_of_day(item.get("completed_at"))
        active_event = item.get("active_block_event")
        if active_event:
            item["active_block_duration"] = _format_duration(
                _duration_seconds(active_event.get("started_at"), _now_iso(timezone))
            )
        else:
            item["active_block_duration"] = "-"


def _archive_completed_from_previous_days(
    status_file: Path,
    payload: dict[str, list[dict[str, str]]],
    timezone: str = "Asia/Shanghai",
) -> None:
    today = _today_iso(timezone)
    changed = False
    for item in payload["batch_plan"]:
        if _is_archived(item):
            continue
        if (item.get("execution_status") or item.get("status")) != "已完成":
            continue
        completed_on = item.get("completed_on")
        if completed_on and completed_on < today:
            item["archived"] = "true"
            item["archived_on"] = today
            changed = True
    if changed:
        for item in payload["batch_plan"]:
            if _is_archived(item) and item.get("archived_on") == today:
                update_item(status_file, "batch_plan", int(item["id"]), item)


def _today_iso(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).date().isoformat()


def _now_iso(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).replace(microsecond=0, tzinfo=None).isoformat()


def _duration_seconds(started_at: object, ended_at: object) -> int:
    if not started_at or not ended_at:
        return 0
    try:
        started = datetime.fromisoformat(str(started_at))
        ended = datetime.fromisoformat(str(ended_at))
    except ValueError:
        return 0
    return max(0, int((ended - started).total_seconds()))


def _format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "-"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def _format_time_of_day(value: object) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return "-"
    return f"{parsed.hour}点{parsed.minute}分"


def _format_datetime(value: object) -> str:
    if not value:
        return "未记录"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return parsed.strftime("%Y-%m-%d %H:%M")
