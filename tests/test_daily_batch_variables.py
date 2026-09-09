"""当日批量口径：一天多批时消息模板要拿到批开始时间和批后日期。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from qwbot.config import Settings
from qwbot.message import build_template_variables, render_message_template
from qwbot.planner import daily_batch_context

TODAY = date(2026, 9, 11)

# 长周期一天最多三批；09-10 的批已完成并在 09-11 凌晨自动归档。
PLAN: list[dict[str, str]] = [
    {
        "id": "1",
        "natural_date": "2026-09-10",
        "batch_start_time": "15:00",
        "current_accounting_date": "2026-09-21",
        "next_accounting_date": "2026-09-22",
        "execution_status": "已完成",
        "completed_on": "2026-09-10",
        "archived": "true",
        "description": "存量特殊日期",
    },
    {
        "id": "2",
        "natural_date": "2026-09-11",
        "batch_start_time": "10:30",
        "current_accounting_date": "2026-09-22",
        "next_accounting_date": "2026-09-24",
        "execution_status": "待执行",
        "description": "存量特殊日期\n计结息专项：对公012 加批验证宽限期",
    },
    {
        "id": "3",
        "natural_date": "2026-09-11",
        "batch_start_time": "15:30",
        "current_accounting_date": "2026-09-24",
        "next_accounting_date": "2026-09-25",
        "execution_status": "待执行",
        "description": "20/21 固定停点",
    },
    {
        "id": "4",
        "natural_date": "2026-09-11",
        "batch_start_time": "19:00",
        "current_accounting_date": "2026-09-25",
        "next_accounting_date": "2026-09-30",
        "execution_status": "待执行",
        "description": "月末",
    },
    {
        "id": "5",
        "natural_date": "2026-09-14",
        "batch_start_time": "10:00",
        "current_accounting_date": "2026-09-30",
        "next_accounting_date": "2026-10-09",
        "execution_status": "待执行",
        "description": "存量特殊日期",
    },
]


def test_every_batch_of_the_day_shows_up_in_the_start_times() -> None:
    context = daily_batch_context(PLAN, TODAY)

    assert context.start_times == "10:30、15:30、19:00"
    assert [item["id"] for item in context.batches] == ["2", "3", "4"]
    assert context.current_accounting_date == "2026-09-22"  # 第一批的批前日期
    assert context.post_accounting_date == "2026-09-30"  # 最后一批的批后日期
    assert context.next_trading_date == "2026-09-30"
    assert context.detail.split("\n") == [
        "10:30 存量特殊日期（2026-09-22 → 2026-09-24）",
        "15:30 20/21 固定停点（2026-09-24 → 2026-09-25）",
        "19:00 月末（2026-09-25 → 2026-09-30）",
    ]


def test_a_day_without_batches_carries_the_previous_post_date() -> None:
    context = daily_batch_context(PLAN, date(2026, 9, 12))

    assert context.batches == []
    assert context.start_times == "无"
    assert context.detail == "无"
    # 09-11 最后一批把会计日期推到 09-30，12 号不跑批就停在 09-30。
    assert context.current_accounting_date == "2026-09-30"
    assert context.post_accounting_date == "2026-09-30"
    assert context.next_trading_date == "2026-10-09"
    assert context.next_batch is not None and context.next_batch["id"] == "5"


def test_archived_rows_still_feed_the_carried_date() -> None:
    """昨天的批今天凌晨就归档了，当天没有批时仍要拿它的批后日期。"""
    context = daily_batch_context([PLAN[0]], date(2026, 9, 11))

    assert context.current_accounting_date == "2026-09-22"
    assert context.post_accounting_date == "2026-09-22"
    assert context.next_batch is None


def test_unfinished_rows_from_earlier_days_do_not_leak_into_today() -> None:
    stale = [
        {
            "id": "9",
            "natural_date": "2026-08-12",
            "batch_start_time": "17:00",
            "current_accounting_date": "2026-07-01",
            "next_accounting_date": "2026-07-21",
            "execution_status": "待执行",
            "description": "长周期试跑结息日还款",
        },
        *PLAN,
    ]
    context = daily_batch_context(stale, TODAY)

    assert context.start_times == "10:30、15:30、19:00"
    assert context.current_accounting_date == "2026-09-22"
    assert context.next_batch is not None and context.next_batch["id"] == "2"


def test_completed_batches_of_today_still_count() -> None:
    done = [
        {**PLAN[1], "execution_status": "已完成", "completed_on": "2026-09-11"},
        PLAN[2],
    ]
    context = daily_batch_context(done, TODAY)

    assert context.start_times == "10:30、15:30"
    assert context.post_accounting_date == "2026-09-25"
    assert context.next_batch is not None and context.next_batch["id"] == "3"


def test_empty_plan_renders_placeholders() -> None:
    context = daily_batch_context([], TODAY)

    assert context.start_times == "无"
    assert context.detail == "无"
    assert context.current_accounting_date == "无"
    assert context.post_accounting_date == "无"
    assert context.next_trading_date == "无"
    assert context.next_batch is None


def _plan_for_today(settings: Settings) -> list[dict[str, str]]:
    """Same shape as `PLAN`, but anchored on the real clock the renderer uses."""
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    today_text = today.isoformat()
    return [
        {**PLAN[1], "natural_date": today_text},
        {**PLAN[2], "natural_date": today_text},
        {**PLAN[3], "natural_date": today_text},
        {**PLAN[4], "natural_date": tomorrow},
    ]


def test_template_variables_expose_the_daily_context(settings: Settings) -> None:
    variables = build_template_variables(settings, None, [], _plan_for_today(settings))

    assert variables["today_batch_start_times"] == "10:30、15:30、19:00"
    assert variables["today_post_batch_date"] == "2026-09-30"
    assert variables["current_trading_date"] == "2026-09-22"
    assert variables["next_trading_date"] == "2026-09-30"
    assert "15:30 20/21 固定停点" in variables["today_batch_detail"]


def test_template_renders_the_daily_variables(settings: Settings) -> None:
    template = {
        "content": (
            "当日批开始时间：{today_batch_start_times}\n"
            "当日批后会计日期：{today_post_batch_date}\n"
            "明细：\n{today_batch_detail}"
        ),
        "at_all": "",
    }

    rendered = render_message_template(settings, template, [], batch_plan=_plan_for_today(settings))

    assert rendered.split("\n")[0] == "当日批开始时间：10:30、15:30、19:00"
    assert rendered.split("\n")[1] == "当日批后会计日期：2026-09-30"
    assert "19:00 月末（2026-09-25 → 2026-09-30）" in rendered


def test_legacy_single_batch_call_keeps_working(settings: Settings) -> None:
    """没有传 batch_plan 时沿用旧口径，当日变量给占位符。"""
    next_batch = {
        "description": "跑批计划一",
        "current_accounting_date": "2026-09-08",
        "next_accounting_date": "2026-09-09",
    }

    variables = build_template_variables(settings, next_batch, [])

    assert variables["current_trading_date"] == "2026-09-08"
    assert variables["next_trading_date"] == "2026-09-09"
    assert variables["today_batch_start_times"] == "无"
    assert variables["today_post_batch_date"] == "无"


def test_default_template_mentions_the_daily_batch(settings: Settings, db_path: Path) -> None:
    from qwbot.store import DEFAULT_REMINDER_TEMPLATE

    rendered = render_message_template(
        settings, DEFAULT_REMINDER_TEMPLATE, [], batch_plan=_plan_for_today(settings)
    )

    assert "当日批开始时间：" in rendered
    assert "10:30、15:30、19:00" in rendered
    assert "当日批后会计日期：" in rendered
    assert "{today_post_batch_date}" not in rendered
