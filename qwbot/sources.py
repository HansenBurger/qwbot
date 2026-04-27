from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from qwbot.planner import is_archived, is_completed_from_previous_day


@dataclass(frozen=True)
class ReminderData:
    batch_plan: list[str]
    progress: list[str]


def load_reminder_data(
    *,
    local_status_file: Path,
    batch_plan_source_url: str | None,
    progress_source_url: str | None,
) -> ReminderData:
    if batch_plan_source_url or progress_source_url:
        return ReminderData(
            batch_plan=_load_items_from_url(batch_plan_source_url, "batch_plan"),
            progress=_load_items_from_url(progress_source_url, "progress"),
        )

    return _load_from_local_file(local_status_file)


def _load_from_local_file(path: Path) -> ReminderData:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    return ReminderData(
        batch_plan=_normalize_items(_active_batch_items(payload.get("batch_plan"))),
        progress=_normalize_items(payload.get("progress")),
    )


def _load_items_from_url(url: str | None, default_field: str) -> list[str]:
    if not url:
        return []

    response = requests.get(url, timeout=10)
    response.raise_for_status()

    content_type = response.headers.get("content-type", "").lower()
    text = response.text.strip()
    if "json" in content_type or text.startswith("{") or text.startswith("["):
        payload = response.json()
        if isinstance(payload, dict):
            return _normalize_items(payload.get(default_field) or payload.get("items"))
        return _normalize_items(payload)

    return _load_csv_items(text)


def _load_csv_items(text: str) -> list[str]:
    rows = csv.DictReader(text.splitlines())
    items: list[str] = []
    for row in rows:
        item = row.get("内容") or row.get("content") or row.get("事项") or row.get("item")
        owner = row.get("负责人") or row.get("owner")
        status = row.get("状态") or row.get("status")
        if not item:
            continue

        suffix_parts = [part for part in [owner, status] if part]
        suffix = f"（{' / '.join(suffix_parts)}）" if suffix_parts else ""
        items.append(f"{item}{suffix}")
    return items


def _normalize_items(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        return [_format_item(item) for item in value if _format_item(item)]
    return [str(value)]


def _active_batch_items(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return [item for item in value if not _is_archived(item)]


def _is_archived(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    return is_archived(item) or is_completed_from_previous_day(item)


def _format_item(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        natural_date = item.get("natural_date") or item.get("自然日历")
        current_accounting_date = (
            item.get("current_accounting_date")
            or item.get("系统当前会计日期")
            or item.get("系统当前\n会计日期")
        )
        next_accounting_date = (
            item.get("next_accounting_date")
            or item.get("跑批后会计日期")
            or item.get("跑批后\n会计日期")
        )
        holiday_flag = item.get("holiday_flag") or item.get("节假日标志")
        description = item.get("description") or item.get("说明")
        requester = item.get("requester") or item.get("提出人")
        execution_status = item.get("execution_status") or item.get("执行状态")
        block_reason = item.get("block_reason") or item.get("阻塞原因")
        batch_start_time = item.get("batch_start_time") or item.get("跑批启动时间")
        if description or current_accounting_date or next_accounting_date:
            return _format_batch_plan_item(
                natural_date=natural_date,
                current_accounting_date=current_accounting_date,
                next_accounting_date=next_accounting_date,
                holiday_flag=holiday_flag,
                description=description,
                requester=requester,
                execution_status=execution_status,
                block_reason=block_reason,
                batch_start_time=batch_start_time,
            )

        content = item.get("内容") or item.get("content") or item.get("事项") or item.get("item")
        owner = item.get("负责人") or item.get("owner")
        status = item.get("状态") or item.get("status")
        category = item.get("类型") or item.get("category")
        date = item.get("日期") or item.get("date")
        if not content:
            return ""
        prefix_parts = [str(part) for part in [date, category] if part]
        suffix_parts = [str(part) for part in [owner, status] if part]
        prefix = f"[{' / '.join(prefix_parts)}] " if prefix_parts else ""
        suffix = f"（{' / '.join(suffix_parts)}）" if suffix_parts else ""
        return f"{prefix}{content}{suffix}"
    return str(item)


def _format_batch_plan_item(
    *,
    natural_date: Any,
    current_accounting_date: Any,
    next_accounting_date: Any,
    holiday_flag: Any,
    description: Any,
    requester: Any,
    execution_status: Any,
    block_reason: Any,
    batch_start_time: Any,
) -> str:
    date_part = str(natural_date) if natural_date else "未填自然日历"
    accounting_part = " -> ".join(
        str(part) for part in [current_accounting_date, next_accounting_date] if part
    )
    detail_parts = []
    if accounting_part:
        detail_parts.append(f"会计日期 {accounting_part}")
    if holiday_flag:
        detail_parts.append(f"节假日 {holiday_flag}")
    if batch_start_time:
        detail_parts.append(f"启动时间 {batch_start_time}")

    suffix_parts = [str(part) for part in [requester, execution_status] if part]
    suffix = f"（{' / '.join(suffix_parts)}）" if suffix_parts else ""
    if execution_status == "有阻塞" and block_reason:
        detail_parts.append(f"阻塞原因 {block_reason}")
    details = f"；{'；'.join(detail_parts)}" if detail_parts else ""
    return f"[{date_part}] {description or '未填写说明'}{details}{suffix}"
