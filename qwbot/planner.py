from __future__ import annotations

from datetime import date
from typing import Any


def active_batch_items(items: list[dict[str, str]]) -> list[tuple[int, dict[str, str]]]:
    indexed_items = [
        (index, item)
        for index, item in enumerate(items)
        if not is_archived(item) and not is_completed_from_previous_day(item)
    ]
    return sorted(indexed_items, key=lambda entry: sort_key(entry[1]))


def today_batch_items(items: list[dict[str, str]], today: date) -> list[tuple[int, dict[str, str]]]:
    today_text = today.isoformat()
    return [
        entry
        for entry in active_batch_items(items)
        if (entry[1].get("natural_date") or entry[1].get("date")) == today_text
    ]


def first_today_batch(items: list[dict[str, str]], today: date) -> tuple[int, dict[str, str]] | None:
    today_items = today_batch_items(items, today)
    return today_items[0] if today_items else None


def next_batch_after(
    items: list[dict[str, str]],
    current_index: int,
) -> tuple[int, dict[str, str]] | None:
    active_items = [
        entry
        for entry in active_batch_items(items)
        if entry[0] != current_index and not is_completed(entry[1])
    ]
    return active_items[0] if active_items else None


def is_business_day(items: list[dict[str, str]], today: date) -> bool:
    if today.weekday() >= 5:
        return False
    today_text = today.isoformat()
    return not any(
        (item.get("natural_date") or item.get("date")) == today_text
        and (item.get("holiday_flag") or "N") == "Y"
        for item in items
    )


def is_archived(item: dict[str, str]) -> bool:
    return item.get("archived") == "true"


def is_completed(item: dict[str, str]) -> bool:
    return (item.get("execution_status") or item.get("status")) == "已完成"


def is_completed_from_previous_day(item: dict[str, str]) -> bool:
    completed_on = item.get("completed_on")
    return bool(completed_on and completed_on < date.today().isoformat() and is_completed(item))


def sort_key(item: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        item.get("natural_date") or item.get("date") or "9999-12-31",
        item.get("batch_start_time") or "99:99",
        item.get("current_accounting_date") or "9999-12-31",
        item.get("next_accounting_date") or "9999-12-31",
    )


def batch_title(item: dict[str, str] | None) -> str:
    if not item:
        return "无"
    description = item.get("description") or item.get("content") or "未填写说明"
    current_date = item.get("current_accounting_date") or "-"
    next_date = item.get("next_accounting_date") or "-"
    return f"{description}，当前交易日 {current_date}，下一交易日 {next_date}"


def batch_detail_lines(item: dict[str, str] | None) -> list[str]:
    if not item:
        return ["当前自然日暂无跑批计划。"]

    lines = [
        f"当前批量：{item.get('description') or item.get('content') or '未填写说明'}",
        f"系统时间：<font color=\"info\">{item.get('current_accounting_date') or '-'}</font>",
        f"跑完会计时间：<font color=\"info\">{item.get('next_accounting_date') or '-'}</font>",
    ]
    if item.get("batch_start_time"):
        lines.append(f"启动时间：<font color=\"info\">{item['batch_start_time']}</font>")
    return lines


def normalize_bool(value: Any) -> bool:
    return str(value).lower() in {"true", "1", "yes", "y"}
