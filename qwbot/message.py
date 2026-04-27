from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from qwbot.config import Settings
from qwbot.planner import batch_title
from qwbot.sources import ReminderData


def build_scheduled_reminder(settings: Settings, next_batch: dict[str, str] | None) -> str:
    now = datetime.now(ZoneInfo(settings.timezone))
    title_date = now.strftime("%m月%d日")

    return "\n".join(
        [
            f"## {title_date} 组内重点工作",
            (
                f"问题登记：{_format_doc_link(settings.progress_doc_url)}　"
                f"环境统计：{_format_doc_link(settings.environment_stats_doc_url)}"
            ),
            (
                f"跑批计划：{_format_doc_link(settings.batch_register_doc_url)}　"
                f"事项安排：{_format_doc_link(settings.agenda_doc_url)}"
            ),
            "",
            f"当前交易日：<font color=\"info\">{_batch_current_date(next_batch)}</font>",
            f"下一交易日：<font color=\"info\">{_batch_next_date(next_batch)}</font>",
            f"跑批计划修改和登记：{_format_doc_link(settings.frontend_url)}",
        ]
    )


def build_daily_reminder(settings: Settings, data: ReminderData) -> str:
    # 保留旧入口，供 CLI preview/send-now 兼容使用。
    now = datetime.now(ZoneInfo(settings.timezone))
    system_date = now.strftime(settings.system_date_format)
    return "\n".join(
        [
            f"## {settings.bot_title}",
            f"> 当前系统日期：<font color=\"info\">{system_date}</font>",
            "",
            "### 今日跑批计划",
            _format_list(data.batch_plan, "暂无跑批计划，请在在线文档中补充。"),
            "",
            "请相关同事按计划执行，并在执行进度在线文档中更新当天进度。",
            _format_doc_line("跑批登记簿", settings.batch_register_doc_url),
            _format_doc_line("执行进度在线文档", settings.progress_doc_url),
            "",
            "<@all>",
        ]
    )


def build_batch_start_notice(item: dict[str, str]) -> str:
    return "\n".join(
        [
            "【批量执行通知】",
            f"当前批量：{batch_title(item)}",
            "批量开始执行，在环境中不要测试。",
        ]
    )


def build_batch_complete_notice(
    completed_item: dict[str, str],
    next_item: dict[str, str] | None,
) -> str:
    lines = [
        "【批量完成通知】",
        f"已完成批量：{_batch_description(completed_item)}",
        f"待执行批量：{batch_title(next_item)}",
    ]
    requester_mentions = _format_requester_mentions(completed_item)
    if requester_mentions:
        lines.append(requester_mentions)
    return "\n".join(lines)


def _format_list(items: list[str], empty_text: str) -> str:
    if not items:
        return f"- {empty_text}"
    return "\n".join(f"- {item}" for item in items)


def _format_doc_line(label: str, url: str | None) -> str:
    if not url:
        return f"{label}：未配置"
    return f"{label}：[点击打开]({url})"


def _format_doc_link(url: str | None) -> str:
    if not url:
        return "未配置"
    return f"[点击]({url})"


def _batch_description(item: dict[str, str] | None) -> str:
    if not item:
        return "无"
    return item.get("description") or item.get("content") or "未填写说明"


def _batch_current_date(item: dict[str, str] | None) -> str:
    if not item:
        return "无"
    return item.get("current_accounting_date") or "-"


def _batch_next_date(item: dict[str, str] | None) -> str:
    if not item:
        return "无"
    return item.get("next_accounting_date") or "-"


def _format_requester_mentions(item: dict[str, str] | None) -> str:
    if not item:
        return ""

    requester = item.get("requester") or item.get("owner") or ""
    names = [
        name.strip()
        for part in requester.replace("，", "、").replace(",", "、").split("、")
        for name in part.split()
        if name.strip()
    ]
    return " ".join(f"@{name}" for name in names)
