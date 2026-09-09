from __future__ import annotations

import argparse
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from qwbot.config import Settings, load_settings
from qwbot.message import render_message_template
from qwbot.plan_import import DEFAULT_SHEET, PAST_MODES, import_plan_file
from qwbot.planner import daily_batch_context, is_business_day, is_holiday
from qwbot.store import (
    get_default_webhook,
    get_webhook,
    init_store,
    list_message_templates,
    load_dispatch_plan,
    load_status_file,
    load_webhook_tree,
    resolve_target_webhook,
    resolve_webhook_url,
)
from qwbot.wecom import WeComWebhookClient
from qwbot.xlsx import XlsxError

CONNECTIVITY_TEST_TEXT = "企业微信机器人连通性测试成功。"


def main() -> None:
    parser = argparse.ArgumentParser(description="企业微信贷款核算测试提醒机器人")
    parser.add_argument(
        "command",
        choices=[
            "send-now",
            "run-scheduled-once",
            "schedule",
            "test-webhook",
            "preview",
            "import-plan",
            "web",
        ],
        help=(
            "send-now 强制发送；run-scheduled-once 手动执行一次定时任务逻辑；"
            "schedule 常驻定时；test-webhook 发送测试消息；preview 仅预览；"
            "import-plan 同步跑批计划表格；web 启动维护页面"
        ),
    )
    parser.add_argument(
        "plan_file",
        nargs="?",
        help="import-plan 读取的跑批计划表格（.xlsx），如 tmp/UAT2主环境及长周期批量计划.xlsx",
    )
    parser.add_argument(
        "--sheet",
        default=DEFAULT_SHEET,
        help=f"import-plan 读取的工作表，默认 {DEFAULT_SHEET}",
    )
    parser.add_argument("--category", help="import-plan 写入的分类，默认取工作表名")
    parser.add_argument(
        "--past",
        choices=PAST_MODES,
        default="skip",
        help="import-plan 对今天之前批次的处理：skip 跳过（默认），archive 按已完成归档",
    )
    parser.add_argument("--today", help="import-plan 用指定的 YYYY-MM-DD 代替今天，便于自测")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="import-plan 只打印将要发生的改动，不写库",
    )
    parser.add_argument(
        "--keep-source-holiday-flag",
        action="store_true",
        help="import-plan 把表格里的节假日标志原样写进 holiday_flag（默认只记进说明，避免影响提醒日历）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="web 命令监听地址")
    parser.add_argument("--port", type=int, default=5000, help="web 命令监听端口")
    parser.add_argument(
        "--webhook",
        choices=["prod", "test"],
        help="按 target 选择数据库中的群机器人，覆盖默认机器人",
    )
    parser.add_argument("--webhook-id", type=int, help="按 ID 选择数据库中的群机器人")
    parser.add_argument("--template", type=int, help="只处理指定 ID 的消息模板")
    parser.add_argument(
        "--at",
        dest="at_time",
        help="run-scheduled-once 使用指定的 HH:MM 代替当前时间，便于自测",
    )
    args = parser.parse_args()

    settings = load_settings()
    _init(settings)

    if args.command == "import-plan":
        _import_plan(settings, args)
        return

    if args.command == "web":
        from qwbot.web import create_app

        app = create_app(settings)
        app.run(host=args.host, port=args.port, debug=False)
        return

    webhook = _resolve_webhook(settings, args)
    if args.command == "test-webhook":
        url = resolve_webhook_url(webhook, settings)
        if not url:
            raise SystemExit("选中的群机器人没有配置 webhook 地址，请在维护页面填写。")
        WeComWebhookClient(url).send_text(CONNECTIVITY_TEST_TEXT)
        name = webhook.get("name") if webhook else "默认群机器人"
        print(f"Webhook test message sent to {name}.")
        return

    if args.command == "preview":
        _preview(settings, webhook, args.template)
        return

    if args.command == "send-now":
        _send_now(settings, webhook, args.template)
        return

    if args.command == "run-scheduled-once":
        _send_due_notifications(settings, set(), at_time=args.at_time)
        return

    _schedule(settings)


def _import_plan(settings: Settings, args: argparse.Namespace) -> None:
    """Sync one worksheet of an exported batch-plan workbook into `batch_plan`."""
    if not args.plan_file:
        raise SystemExit(
            "import-plan 需要指定表格路径，例如："
            "python -m qwbot.cli import-plan tmp/UAT2主环境及长周期批量计划.xlsx"
        )
    path = Path(args.plan_file)
    if not path.exists():
        raise SystemExit(f"找不到跑批计划表格：{path}")
    today = _today_from_args(args.today, settings.timezone)
    try:
        summary = import_plan_file(
            settings.db_path,
            path,
            sheet=args.sheet,
            category=args.category,
            past=args.past,
            today=today,
            dry_run=args.dry_run,
            keep_source_holiday_flag=args.keep_source_holiday_flag,
        )
    except XlsxError as exc:
        raise SystemExit(str(exc)) from exc

    print(summary.as_text())
    if args.dry_run:
        return
    context = daily_batch_context(load_status_file(settings.db_path)["batch_plan"], today)
    print(
        f"{today.isoformat()} 当日批开始时间：{context.start_times}　"
        f"当日批后会计日期：{context.post_accounting_date}"
    )


def _today_from_args(value: str | None, timezone: str) -> date:
    if not value:
        return datetime.now(ZoneInfo(timezone)).date()
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise SystemExit(f"--today 需要 YYYY-MM-DD 格式，收到：{value}") from None


def _init(settings: Settings) -> None:
    """Create/upgrade the SQLite schema and seed webhooks from `.env` once."""
    init_store(
        settings.db_path,
        settings.local_status_file,
        webhook_seeds=settings.webhook_seeds(),
        default_target=settings.default_seed_target(),
    )


def _resolve_webhook(settings: Settings, args: argparse.Namespace) -> dict[str, Any] | None:
    if args.webhook_id:
        webhook = get_webhook(settings.db_path, args.webhook_id)
        if webhook is None:
            raise SystemExit(f"群机器人不存在: {args.webhook_id}")
        return webhook
    if args.webhook:
        webhook, _ = resolve_target_webhook(settings.db_path, args.webhook, settings)
        return webhook
    return get_default_webhook(settings.db_path)


def _selected_plan(
    settings: Settings,
    webhook: dict[str, Any] | None,
    template_id: int | None,
) -> list[dict[str, Any]]:
    plan = load_dispatch_plan(settings.db_path)
    if webhook is not None:
        plan = [entry for entry in plan if entry["webhook_id"] == int(webhook["id"])]
    if template_id is not None:
        plan = [entry for entry in plan if entry["template_id"] == template_id]
    return plan


def _render_plan(
    settings: Settings,
    plan: list[dict[str, Any]],
    payload: dict[str, Any],
) -> list[tuple[dict[str, Any], str]]:
    template_vars = payload.get("template_vars", [])
    batch_plan = payload["batch_plan"]
    return [
        (entry, render_message_template(settings, entry, template_vars, batch_plan=batch_plan))
        for entry in plan
    ]


def _preview(settings: Settings, webhook: dict[str, Any] | None, template_id: int | None) -> None:
    payload = load_status_file(settings.db_path)
    plan = _selected_plan(settings, webhook, template_id)
    if not plan:
        _preview_templates_without_schedule(settings, webhook, template_id, payload)
        return

    rendered = _render_plan(settings, plan, payload)
    grouped: dict[tuple[int, int], list[str]] = {}
    for entry, _ in rendered:
        grouped.setdefault((entry["webhook_id"], entry["template_id"]), []).append(entry["send_time"])

    for (webhook_id, group_template_id), times in grouped.items():
        entry = next(
            item
            for item in plan
            if item["webhook_id"] == webhook_id and item["template_id"] == group_template_id
        )
        message = next(
            text
            for item, text in rendered
            if item["webhook_id"] == webhook_id and item["template_id"] == group_template_id
        )
        print(
            f"=== {entry['webhook_name']} · {entry['template_name']}"
            f" · {'/'.join(sorted(set(times)))} ==="
        )
        print(message)
        print()


def _preview_templates_without_schedule(
    settings: Settings,
    webhook: dict[str, Any] | None,
    template_id: int | None,
    payload: dict[str, Any],
) -> None:
    """Render templates that are not in the dispatch plan (disabled or no time set)."""
    templates = _selected_templates(settings, webhook, template_id)
    if not templates:
        print("没有可预览的消息模板，请先在维护页面 /notifications 新建。")
        return
    for template in templates:
        state = "启用" if template["is_enabled"] else "停用"
        print(f"=== {template['name']} · {state} · 暂无启用的通知时间 ===")
        print(
            render_message_template(
                settings,
                template,
                payload.get("template_vars", []),
                batch_plan=payload["batch_plan"],
            )
        )
        print()


def _selected_templates(
    settings: Settings,
    webhook: dict[str, Any] | None,
    template_id: int | None,
) -> list[dict[str, Any]]:
    templates = list_message_templates(
        settings.db_path,
        int(webhook["id"]) if webhook is not None else None,
    )
    if template_id is not None:
        templates = [item for item in templates if int(item["id"]) == template_id]
    return templates


def _send_now(settings: Settings, webhook: dict[str, Any] | None, template_id: int | None) -> None:
    payload = load_status_file(settings.db_path)
    plan = _selected_plan(settings, webhook, template_id)
    if plan:
        seen: set[tuple[int, int]] = set()
        sent = 0
        for entry, message in _render_plan(settings, plan, payload):
            key = (entry["webhook_id"], entry["template_id"])
            if key in seen:
                continue
            seen.add(key)
            if _send_markdown(settings, entry, message):
                sent += 1
        print(f"Sent {sent} message(s).")
        return

    # No enabled schedule: still allow a forced send of the template itself.
    templates = _selected_templates(settings, webhook, template_id)
    if not templates:
        raise SystemExit("没有可发送的消息模板，请先在维护页面 /notifications 新建。")
    target = webhook or get_default_webhook(settings.db_path)
    url = resolve_webhook_url(target, settings)
    if not url:
        raise SystemExit("选中的群机器人没有配置 webhook 地址，请在维护页面填写。")
    client = WeComWebhookClient(url)
    name = target.get("name") if target else "默认群机器人"
    for template in templates:
        message = render_message_template(
            settings,
            template,
            payload.get("template_vars", []),
            batch_plan=payload["batch_plan"],
        )
        client.send_markdown(message)
        print(f"Sent template '{template['name']}' to {name}.")


def _send_markdown(settings: Settings, entry: dict[str, Any], message: str) -> bool:
    url = resolve_webhook_url(
        {"url": entry["webhook_url"], "target": entry["webhook_target"]},
        settings,
    )
    if not url:
        print(f"Skipped: webhook '{entry['webhook_name']}' has no URL configured.")
        return False
    try:
        WeComWebhookClient(url).send_markdown(message)
    except Exception as exc:  # noqa: BLE001 - one bad webhook must not stop the others
        print(f"Failed: {entry['webhook_name']} / {entry['template_name']}: {exc}")
        return False
    print(f"Sent: {entry['webhook_name']} / {entry['template_name']} at {entry['send_time']}.")
    return True


def _schedule(settings: Settings) -> None:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    sent_keys: set[str] = set()
    scheduler.add_job(
        lambda: _send_due_notifications(settings, sent_keys),
        "interval",
        seconds=30,
        next_run_time=datetime.now(ZoneInfo(settings.timezone)),
        id="notification-dispatcher",
        replace_existing=True,
    )
    scheduler.start()
    webhook_count = len(load_webhook_tree(settings.db_path))
    print(
        "Scheduler started. "
        f"{webhook_count} webhook(s) loaded from the SQLite database; "
        "notification times are configured per message template."
    )

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print("Scheduler stopped.")


def _send_due_notifications(
    settings: Settings,
    sent_keys: set[str],
    at_time: str | None = None,
) -> None:
    """Dispatch every enabled webhook x template x schedule combination that is due."""
    _init(settings)
    payload = load_status_file(settings.db_path)
    now = datetime.now(ZoneInfo(settings.timezone))
    today = now.date()
    today_text = today.isoformat()
    current_time = (at_time or now.strftime("%H:%M")).strip()

    # Keep only today's sent markers so long-running schedulers do not grow unbounded.
    sent_keys.intersection_update({key for key in sent_keys if key.startswith(today_text)})

    batch_plan = payload["batch_plan"]
    template_vars = payload.get("template_vars", [])
    dispatched = 0

    for entry in load_dispatch_plan(settings.db_path):
        if entry["send_time"] != current_time:
            continue
        key = f"{today_text}:tpl:{entry['template_id']}:sch:{entry['schedule_id']}:{current_time}"
        if key in sent_keys:
            continue
        # Mark before sending so a failure is not retried twice within the same minute.
        sent_keys.add(key)
        if not should_dispatch(entry, payload["batch_plan"], today):
            print(
                f"Skipped: {entry['webhook_name']} / {entry['template_name']} "
                f"({today_text} date rule)."
            )
            continue
        message = render_message_template(settings, entry, template_vars, batch_plan=batch_plan)
        if _send_markdown(settings, entry, message):
            dispatched += 1

    if at_time and dispatched == 0:
        print(f"No enabled webhook template is scheduled at {current_time}.")


def should_dispatch(
    entry: dict[str, Any],
    batch_plan: list[dict[str, str]],
    today: date,
) -> bool:
    """Decide whether one dispatch-plan entry should actually be sent today.

    Priority: template date override > global date override > date rule.
    """
    today_text = today.isoformat()
    if today_text in entry.get("force_dates", []):
        return True
    if today_text in entry.get("skip_dates", []):
        return False

    date_rule = (
        entry.get("schedule_date_rule") or entry.get("template_date_rule") or "business_day"
    )
    if date_rule == "all":
        return True
    if today_text in entry.get("global_force_dates", []):
        return True
    if today_text in entry.get("global_skip_dates", []):
        return False
    if date_rule == "holiday_only":
        return not is_holiday(batch_plan, today)
    return is_business_day(batch_plan, today)


if __name__ == "__main__":
    main()
