from __future__ import annotations

import argparse
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from qwbot.config import load_settings
from qwbot.message import build_scheduled_reminder
from qwbot.planner import active_batch_items, is_business_day, is_completed
from qwbot.store import init_store, load_status_file
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
        _send_if_business_day(settings)
        return

    _schedule(settings)


def _build_scheduled_message(settings):
    init_store(settings.db_path, settings.local_status_file)
    payload = load_status_file(settings.db_path)
    next_batch = next(
        (item for _, item in active_batch_items(payload["batch_plan"]) if not is_completed(item)),
        None,
    )
    return build_scheduled_reminder(settings, next_batch)


def _send(settings) -> None:
    message = _build_scheduled_message(settings)
    client = WeComWebhookClient(settings.webhook_url)
    client.send_markdown(message)
    print("Scheduled reminder sent.")


def _send_if_business_day(settings) -> None:
    init_store(settings.db_path, settings.local_status_file)
    payload = load_status_file(settings.db_path)
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    if not is_business_day(payload["batch_plan"], today):
        print("Scheduled reminder skipped: non-business day.")
        return
    _send(settings)


def _schedule(settings) -> None:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        lambda: _send_if_business_day(settings),
        CronTrigger(
            hour=9,
            minute=0,
            timezone=settings.timezone,
        ),
        id="morning-reminder",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _send_if_business_day(settings),
        CronTrigger(
            hour=18,
            minute=0,
            timezone=settings.timezone,
        ),
        id="evening-reminder",
        replace_existing=True,
    )
    scheduler.start()
    print(
        "Scheduler started. Scheduled reminders will run at 09:00 and 18:00 on business days."
    )

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print("Scheduler stopped.")


if __name__ == "__main__":
    main()
