from __future__ import annotations

from datetime import date


def active_batch_items(items: list[dict[str, str]]) -> list[tuple[int, dict[str, str]]]:
    indexed_items = [
        (_item_id(item, index), item)
        for index, item in enumerate(items)
        if not is_archived(item) and not is_completed_from_previous_day(item)
    ]
    return sorted(indexed_items, key=lambda entry: sort_key(entry[1]))


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
    return not is_holiday(items, today)


def is_holiday(items: list[dict[str, str]], today: date) -> bool:
    today_text = today.isoformat()
    return any(
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
        item.get("current_accounting_date") or "9999-12-31",
        item.get("next_accounting_date") or "9999-12-31",
        item.get("batch_start_time") or "99:99",
    )


def batch_title(item: dict[str, str] | None) -> str:
    if not item:
        return "无"
    description = item.get("description") or item.get("content") or "未填写说明"
    current_date = item.get("current_accounting_date") or "-"
    next_date = item.get("next_accounting_date") or "-"
    return f"{description}，当前交易日 {current_date}，下一交易日 {next_date}"


def _item_id(item: dict[str, str], fallback: int) -> int:
    try:
        return int(item.get("id") or fallback)
    except (TypeError, ValueError):
        return fallback
