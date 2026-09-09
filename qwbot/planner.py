from __future__ import annotations

from dataclasses import dataclass
from datetime import date

EMPTY_TEXT = "无"


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
    description = short_description(item) or "未填写说明"
    current_date = item.get("current_accounting_date") or "-"
    next_date = item.get("next_accounting_date") or "-"
    return f"{description}，当前交易日 {current_date}，下一交易日 {next_date}"


def _item_id(item: dict[str, str], fallback: int) -> int:
    try:
        return int(item.get("id") or fallback)
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class DailyBatchContext:
    """一天可能跑好几个批，这里给出“当日口径”，消息模板直接取用。

    - `start_times`: 当日全部批次的计划开始时间，如 `10:00、15:30、19:00`
    - `current_accounting_date`: 当日第一批的批前会计日期（当天没有批时为上一批的批后日期）
    - `post_accounting_date`: 当日最后一批的批后会计日期，即“批后的日期”
    - `next_trading_date`: 下一个待执行批量的批后会计日期（当天有批时等于 `post_accounting_date`）
    """

    today: date
    batches: list[dict[str, str]]
    start_times: str
    detail: str
    current_accounting_date: str
    post_accounting_date: str
    next_trading_date: str
    next_batch: dict[str, str] | None


def daily_batch_context(
    items: list[dict[str, str]],
    today: date | None = None,
) -> DailyBatchContext:
    """Collect today's batches and the accounting dates the messages should quote."""
    today = today or date.today()
    today_text = today.isoformat()
    pending = [item for item in items if not is_archived(item)]
    batches = sorted(
        (item for item in pending if _natural_date(item) == today_text),
        key=sort_key,
    )
    carried = _carried_accounting_date(items, today)
    if batches:
        current = batches[0].get("current_accounting_date") or carried
        post = batches[-1].get("next_accounting_date") or current
    else:
        current = post = carried

    upcoming = _upcoming_batch(pending, today_text)
    next_trading = post if batches else ((upcoming or {}).get("next_accounting_date") or carried)
    times = [item.get("batch_start_time") for item in batches if item.get("batch_start_time")]
    return DailyBatchContext(
        today=today,
        batches=batches,
        start_times="、".join(times) or EMPTY_TEXT,
        detail="\n".join(_batch_line(item) for item in batches) or EMPTY_TEXT,
        current_accounting_date=current or EMPTY_TEXT,
        post_accounting_date=post or EMPTY_TEXT,
        next_trading_date=next_trading or EMPTY_TEXT,
        next_batch=upcoming,
    )


def short_description(item: dict[str, str] | None) -> str:
    """First line of the description: imports fold team notes into later lines."""
    if not item:
        return ""
    description = item.get("description") or item.get("content") or ""
    return description.split("\n", 1)[0].strip()


def _batch_line(item: dict[str, str]) -> str:
    start_time = item.get("batch_start_time") or "--:--"
    description = short_description(item) or "未填写说明"
    current = item.get("current_accounting_date") or "-"
    post = item.get("next_accounting_date") or "-"
    return f"{start_time} {description}（{current} → {post}）"


def _upcoming_batch(items: list[dict[str, str]], today_text: str) -> dict[str, str] | None:
    """Earliest not-yet-completed batch of today or later; past leftovers are ignored."""
    candidates = [
        item
        for item in items
        if not is_completed(item) and _natural_date(item) >= today_text
    ]
    return min(candidates, key=sort_key) if candidates else None


def _carried_accounting_date(items: list[dict[str, str]], today: date) -> str:
    """Accounting date in force on a day without any batch.

    Archived rows are included on purpose: yesterday's batch is archived the next
    morning, and its post-batch date is exactly today's system date.
    """
    today_text = today.isoformat()
    dated = [item for item in items if _natural_date(item)]
    past = [item for item in dated if _natural_date(item) < today_text]
    if past:
        latest = max(past, key=sort_key)
        return latest.get("next_accounting_date") or latest.get("current_accounting_date") or ""
    future = [item for item in dated if _natural_date(item) > today_text]
    if future:
        return min(future, key=sort_key).get("current_accounting_date") or ""
    return ""


def _natural_date(item: dict[str, str]) -> str:
    return item.get("natural_date") or item.get("date") or ""
