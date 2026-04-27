from __future__ import annotations

import json
from pathlib import Path
from typing import Any

COLLECTIONS = {"batch_plan", "progress"}


def load_status_file(path: Path) -> dict[str, list[dict[str, str]]]:
    if not path.exists():
        return {"batch_plan": [], "progress": []}

    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    return {
        "batch_plan": _normalize_collection(payload.get("batch_plan")),
        "progress": _normalize_collection(payload.get("progress")),
    }


def save_status_file(path: Path, payload: dict[str, list[dict[str, str]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def add_item(path: Path, collection: str, item: dict[str, str]) -> None:
    _ensure_collection(collection)
    payload = load_status_file(path)
    payload[collection].append(_clean_item(item))
    save_status_file(path, payload)


def update_item(path: Path, collection: str, index: int, item: dict[str, str]) -> None:
    _ensure_collection(collection)
    payload = load_status_file(path)
    _ensure_index(payload[collection], index)
    existing_item = payload[collection][index]
    updated_item = _clean_item(item)
    for key in ["archived", "archived_on", "completed_on", "started_on"]:
        if key not in item and existing_item.get(key):
            updated_item[key] = existing_item[key]
    payload[collection][index] = updated_item
    save_status_file(path, payload)


def delete_item(path: Path, collection: str, index: int) -> None:
    _ensure_collection(collection)
    payload = load_status_file(path)
    _ensure_index(payload[collection], index)
    del payload[collection][index]
    save_status_file(path, payload)


def _normalize_collection(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [_clean_item(item) for item in value if _clean_item(item).get("content")]


def _clean_item(item: Any) -> dict[str, str]:
    if isinstance(item, str):
        return _empty_item(content=item.strip())
    if not isinstance(item, dict):
        return _empty_item(content=str(item))

    natural_date = _value(item, "natural_date", "自然日历", "date", "日期")
    description = _value(item, "description", "说明", "content", "内容", "事项")
    requester = _value(item, "requester", "提出人", "owner", "负责人")
    execution_status = _value(item, "execution_status", "执行状态", "status", "状态")
    block_reason = _value(item, "block_reason", "阻塞原因")
    batch_start_time = _value(item, "batch_start_time", "跑批启动时间")
    return {
        "content": _value(item, "content", "内容", "事项") or description,
        "owner": _value(item, "owner", "负责人") or requester,
        "status": _value(item, "status", "状态") or execution_status,
        "category": str(item.get("category") or item.get("类型") or "").strip(),
        "date": _value(item, "date", "日期") or natural_date,
        "natural_date": natural_date,
        "current_accounting_date": _value(
            item,
            "current_accounting_date",
            "系统当前会计日期",
            "系统当前\n会计日期",
        ),
        "next_accounting_date": _value(
            item,
            "next_accounting_date",
            "跑批后会计日期",
            "跑批后\n会计日期",
        ),
        "holiday_flag": _value(item, "holiday_flag", "节假日标志", "节假日标志\nY-是/N-否"),
        "description": description,
        "requester": requester,
        "execution_status": execution_status,
        "block_reason": block_reason,
        "batch_start_time": batch_start_time,
        "archived": _value(item, "archived"),
        "archived_on": _value(item, "archived_on"),
        "completed_on": _value(item, "completed_on"),
        "started_on": _value(item, "started_on"),
    }


def _empty_item(content: str = "") -> dict[str, str]:
    return {
        "content": content,
        "owner": "",
        "status": "",
        "category": "",
        "date": "",
        "natural_date": "",
        "current_accounting_date": "",
        "next_accounting_date": "",
        "holiday_flag": "",
        "description": content,
        "requester": "",
        "execution_status": "",
        "block_reason": "",
        "batch_start_time": "",
        "archived": "",
        "archived_on": "",
        "completed_on": "",
        "started_on": "",
    }


def _value(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _ensure_collection(collection: str) -> None:
    if collection not in COLLECTIONS:
        raise ValueError(f"Unsupported collection: {collection}")


def _ensure_index(items: list[dict[str, str]], index: int) -> None:
    if index < 0 or index >= len(items):
        raise IndexError(f"Item index out of range: {index}")
