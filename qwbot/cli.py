from __future__ import annotations

import argparse
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from qwbot.config import load_settings
from qwbot.message import build_custom_notification, build_scheduled_reminder
from qwbot.planner import active_batch_items, is_business_day, is_completed, is_holiday
from qwbot.store import get_active_template, init_store, load_status_file
from qwbot.wecom import WeComWebhookClient


def main() -> None:
    parser = argparse.ArgumentParser(description="企业微信贷款核算测试提醒机器人")
    parser.add_argument(
        "command",
        choices=["send-now", "run-scheduled-once", "schedule", "test-webhook", "preview", "web"],
        help=(
            "send-now 强制发送；run-scheduled-once 手动执行一次定时任务逻辑；"
            "schedule 常驻定时；test-webhook 发送测试消息；preview 仅预览；web 启动维护页面"
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="web 命令监听地址")
    parser.add_argument("--port", type=int, default=5000, help="web 命令监听端口")
    parser.add_argument(
        "--webhook",
        choices=["prod", "test"],
        help="覆盖 WECOM_WEBHOOK_TARGET，选择生产或自测机器人",
    )
    args = parser.parse_args()

    settings = load_settings()
    if args.webhook:
        settings = settings.with_webhook_target(args.webhook)
    if args.command == "web":
        from qwbot.web import create_app

        app = create_app(settings)
        app.run(host=args.host, port=args.port, debug=False)
        return

    if args.command == "test-webhook":
        WeComWebhookClient(settings.webhook_url).send_text("企业微信机器人连通性测试成功。")
        print("Webhook test message sent.")
        return

    message = _build_scheduled_message(settings)
    if args.command == "preview":
        print(message)
        return

    if args.command == "send-now":
        _send(settings)
        return

    if args.command == "run-scheduled-once":
        _send_due_notifications(settings, set())
        return

    _schedule(settings)


def _build_scheduled_message(settings):
    init_store(settings.db_path, settings.local_status_file)
    payload = load_status_file(settings.db_path)
    next_batch = next(
        (item for _, item in active_batch_items(payload["batch_plan"]) if not is_completed(item)),
        None,
    )
    template = get_active_template(settings.db_path)
    return build_scheduled_reminder(settings, next_batch, template, payload.get("template_vars"))


def _send(settings) -> None:
    message = _build_scheduled_message(settings)
    client = WeComWebhookClient(settings.webhook_url)
    client.send_markdown(message)
    print("Scheduled reminder sent.")


def _send_if_business_day(settings) -> None:
    init_store(settings.db_path, settings.local_status_file)
    payload = load_status_file(settings.db_path)
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    today_text = today.isoformat()
    skipped_dates = {item["date"] for item in payload["scheduler_skip_dates"]}
    forced_dates = {item["date"] for item in payload["scheduler_force_dates"]}
    if today_text in skipped_dates:
        print("Scheduled reminder skipped: date configured to skip.")
        return
    if today_text not in forced_dates and not is_business_day(payload["batch_plan"], today):
        print("Scheduled reminder skipped: non-business day.")
        return
    _send(settings)


def _schedule(settings) -> None:
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
    print(
        "Scheduler started. Notification times are loaded from the SQLite database."
    )

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print("Scheduler stopped.")


def _send_due_notifications(settings, sent_keys: set[str]) -> None:
    init_store(settings.db_path, settings.local_status_file)
    payload = load_status_file(settings.db_path)
    now = datetime.now(ZoneInfo(settings.timezone))
    today_text = now.date().isoformat()
    current_time = now.strftime("%H:%M")

    # Keep only today's sent markers so long-running schedulers do not grow unbounded.
    sent_keys.intersection_update({key for key in sent_keys if key.startswith(today_text)})
    client = WeComWebhookClient(settings.webhook_url)

    skipped_dates = {item["date"] for item in payload["scheduler_skip_dates"]}
    forced_dates = {item["date"] for item in payload["scheduler_force_dates"]}
    if today_text in skipped_dates:
        print(f"Built-in reminder skipped: {today_text} is configured to skip.")
    elif today_text not in forced_dates and not is_business_day(payload["batch_plan"], now.date()):
        print(f"Built-in reminder skipped: {today_text} is non-business day.")
    else:
        for reminder in payload["scheduler_times"]:
            if reminder.get("send_time") == current_time:
                key = f"{today_text}:builtin:{reminder['id']}:{current_time}"
                if key in sent_keys:
                    continue
                client.send_markdown(_build_scheduled_message(settings))
                sent_keys.add(key)
                print(f"Built-in reminder sent: {reminder['id']} {current_time}.")

    for task in payload["notification_tasks"]:
        if task.get("send_time") != current_time:
            continue
        if not _should_send_custom_notification(payload["batch_plan"], task, now.date()):
            print(f"Custom notification skipped: {task.get('title') or task['id']} date rule.")
            continue
        key = f"{today_text}:custom:{task['id']}:{current_time}"
        if key in sent_keys:
            continue
        client.send_markdown(build_custom_notification(task, payload.get("template_vars", []), settings))
        sent_keys.add(key)
        print(f"Custom notification sent: {task.get('title') or task['id']} {current_time}.")


def _should_send_custom_notification(batch_plan, task, today) -> bool:
    date_rule = task.get("date_rule") or "business_day"
    if date_rule == "all":
        return True
    if date_rule == "holiday_only":
        return not is_holiday(batch_plan, today)
    return is_business_day(batch_plan, today)


if __name__ == "__main__":
    main()
