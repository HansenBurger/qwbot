from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

from qwbot.config import Settings
from qwbot.message import build_batch_complete_notice, build_batch_start_notice
from qwbot.planner import is_completed, next_batch_after, sort_key
from qwbot.store import (
    add_block_event,
    add_item,
    close_open_block,
    delete_item,
    get_item,
    has_open_block,
    init_store,
    load_status_file,
    update_item,
    update_open_block_reason,
)
from qwbot.wecom import WeComWebhookClient


def create_app(settings: Settings) -> Flask:
    app = Flask(__name__)
    status_file = settings.db_path
    init_store(status_file, settings.local_status_file)

    @app.get("/")
    def index():
        payload = load_status_file(status_file)
        _archive_completed_from_previous_days(status_file, payload)
        view = request.args.get("view", "active")
        filters = _archive_filters_from_request()
        archived_entries = _batch_items_for_view(payload["batch_plan"], "archived", filters)
        active_entries = _batch_items_for_view(payload["batch_plan"], "active")
        pagination = _paginate_entries(
            archived_entries if view == "archived" else active_entries,
            page=_int_arg("page", 1),
            per_page=10 if view == "archived" else 1000,
        )
        return render_template(
            "index.html",
            batch_plan=pagination["items"],
            blocked_plan=_blocked_items(payload["batch_plan"]),
            active_count=len(active_entries),
            archived_count=len(archived_entries),
            archive_filters=filters,
            pagination=pagination,
            today=date.today().isoformat(),
            view=view,
            status_file=_display_path(status_file),
        )

    @app.post("/items/<collection>")
    def create_item(collection: str):
        add_item(status_file, collection, _item_from_form())
        return redirect(url_for("index"))

    @app.post("/items/<collection>/<int:index>")
    def edit_item(collection: str, index: int):
        existing_item = get_item(status_file, collection, index)
        if collection == "batch_plan" and _is_archived(existing_item):
            return redirect(url_for("index", view="archived"))
        if collection == "batch_plan" and not _can_edit_item(existing_item):
            return redirect(url_for("index"))
        item = _item_from_form()
        _apply_completion_metadata(existing_item, item)
        update_item(status_file, collection, index, item)
        if collection == "batch_plan":
            _sync_block_events(status_file, index, existing_item, item)
        return redirect(url_for("index"))

    @app.post("/items/<collection>/<int:index>/delete")
    def remove_item(collection: str, index: int):
        if collection == "batch_plan":
            item = get_item(status_file, collection, index)
            if _is_archived(item):
                return redirect(url_for("index", view="archived"))
            if not _can_delete_item(item):
                return redirect(url_for("index"))
        delete_item(status_file, collection, index)
        return redirect(url_for("index"))

    @app.post("/items/batch_plan/<int:index>/archive")
    def archive_item(index: int):
        item = get_item(status_file, "batch_plan", index)
        if not is_completed(item):
            return redirect(url_for("index"))
        item["archived"] = "true"
        item["archived_on"] = date.today().isoformat()
        update_item(status_file, "batch_plan", index, item)
        return redirect(url_for("index"))

    @app.post("/items/batch_plan/<int:index>/start")
    def start_item(index: int):
        payload = load_status_file(status_file)
        item = get_item(status_file, "batch_plan", index)
        if _is_archived(item):
            return redirect(url_for("index", view="archived"))
        if not _can_start_batch(payload["batch_plan"], index):
            return redirect(url_for("index"))

        item["execution_status"] = "进行中"
        item["started_on"] = date.today().isoformat()
        item["started_at"] = _now_iso()
        update_item(status_file, "batch_plan", index, item)
        WeComWebhookClient(settings.webhook_url).send_text(
            build_batch_start_notice(item),
            mentioned_list=["@all"],
        )
        return redirect(url_for("index"))

    @app.post("/items/batch_plan/<int:index>/complete")
    def complete_item(index: int):
        payload = load_status_file(status_file)
        item = get_item(status_file, "batch_plan", index)
        if _is_archived(item):
            return redirect(url_for("index", view="archived"))
        if not _can_complete_batch(payload["batch_plan"], index):
            return redirect(url_for("index"))

        item["execution_status"] = "已完成"
        item["completed_on"] = date.today().isoformat()
        item["completed_at"] = _now_iso()
        item["execution_seconds"] = str(_duration_seconds(item.get("started_at"), item["completed_at"]))
        next_entry = next_batch_after(payload["batch_plan"], index)
        update_item(status_file, "batch_plan", index, item)
        WeComWebhookClient(settings.webhook_url).send_text(
            build_batch_complete_notice(item, next_entry[1] if next_entry else None),
        )
        return redirect(url_for("index"))

    @app.post("/items/batch_plan/<int:index>/close-block")
    def close_block(index: int):
        item = get_item(status_file, "batch_plan", index)
        if _is_archived(item) or _execution_status(item) != "有阻塞":
            return redirect(url_for("index"))

        close_open_block(status_file, index)
        item["execution_status"] = "进行中"
        item["block_reason"] = ""
        update_item(status_file, "batch_plan", index, item)
        return redirect(url_for("index"))

    return app


def _item_from_form() -> dict[str, str]:
    return {
        "date": request.form.get("date", ""),
        "category": request.form.get("category", ""),
        "content": request.form.get("content", ""),
        "owner": request.form.get("owner", ""),
        "status": request.form.get("status", ""),
        "natural_date": request.form.get("natural_date", ""),
        "current_accounting_date": request.form.get("current_accounting_date", ""),
        "next_accounting_date": request.form.get("next_accounting_date", ""),
        "holiday_flag": request.form.get("holiday_flag", ""),
        "description": request.form.get("description", ""),
        "requester": request.form.get("requester", ""),
        "execution_status": request.form.get("execution_status", ""),
        "block_reason": request.form.get("block_reason", ""),
        "batch_start_time": request.form.get("batch_start_time", ""),
        "started_on": request.form.get("started_on", ""),
    }


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _archive_filters_from_request() -> dict[str, str]:
    return {
        "natural_date": request.args.get("natural_date", "").strip(),
        "accounting_date": request.args.get("accounting_date", "").strip(),
        "holiday_flag": request.args.get("holiday_flag", "").strip(),
    }


def _int_arg(name: str, default: int) -> int:
    try:
        return max(1, int(request.args.get(name, default)))
    except (TypeError, ValueError):
        return default


def _batch_items_for_view(
    items: list[dict[str, object]],
    view: str,
    filters: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    archived = view == "archived"
    indexed_items = [
        _entry_for_item(item)
        for item in items
        if _is_archived(item) is archived and _matches_archive_filters(item, filters)
    ]
    sorted_items = sorted(indexed_items, key=lambda entry: sort_key(entry["item"]))
    if not archived:
        _attach_execution_permissions(sorted_items)
    _attach_metrics(sorted_items)
    return sorted_items


def _entry_for_item(item: dict[str, object]) -> dict[str, object]:
    return {"index": int(item.get("id") or 0), "item": item}


def _matches_archive_filters(item: dict[str, object], filters: dict[str, str] | None) -> bool:
    if not filters:
        return True
    natural_date = str(item.get("natural_date") or item.get("date") or "")
    accounting_date = str(item.get("current_accounting_date") or "")
    holiday_flag = str(item.get("holiday_flag") or "N")
    return (
        (not filters.get("natural_date") or natural_date == filters["natural_date"])
        and (not filters.get("accounting_date") or accounting_date == filters["accounting_date"])
        and (not filters.get("holiday_flag") or holiday_flag == filters["holiday_flag"])
    )


def _paginate_entries(
    entries: list[dict[str, object]],
    *,
    page: int,
    per_page: int,
) -> dict[str, object]:
    total = len(entries)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), total_pages)
    start = (page - 1) * per_page
    return {
        "items": entries[start : start + per_page],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1,
        "next_page": page + 1,
        "start_index": start,
    }


def _is_archived(item: dict[str, object]) -> bool:
    return item.get("archived") == "true"


def _blocked_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    current_item = _current_running_or_blocked_item(items)
    if not current_item:
        return []

    events = list(current_item.get("block_events") or [])
    if not events and current_item.get("block_reason"):
        events = [
            {
                "id": "",
                "reason": current_item.get("block_reason"),
                "started_at": "",
                "ended_at": "",
                "duration_seconds": 0,
            }
        ]

    return sorted(
        [
            _block_event_entry(
                batch_index=int(current_item.get("id") or 0),
                event=event,
            )
            for event in events
        ],
        key=lambda entry: entry["sort_key"],
        reverse=True,
    )


def _current_running_or_blocked_item(items: list[dict[str, object]]) -> dict[str, object] | None:
    candidates = [
        item
        for item in items
        if not _is_archived(item) and _execution_status(item) in {"进行中", "有阻塞"}
    ]
    if not candidates:
        return None
    return sorted(candidates, key=sort_key)[0]


def _block_event_entry(batch_index: int, event: dict[str, object]) -> dict[str, object]:
    is_open = not event.get("ended_at")
    duration_seconds = (
        _duration_seconds(event.get("started_at"), _now_iso())
        if is_open
        else int(event.get("duration_seconds") or 0)
    )
    return {
        "batch_index": batch_index,
        "event": event,
        "is_open": is_open,
        "row_class": "blocked-row-open" if is_open else "blocked-row-closed",
        "status_label": "未关闭" if is_open else "已关闭",
        "registered_at": _format_datetime(event.get("started_at")),
        "reason": event.get("reason") or "未填写",
        "duration": _format_duration(duration_seconds),
        "sort_key": str(event.get("started_at") or ""),
    }


def _attach_execution_permissions(entries: list[dict[str, object]]) -> None:
    running_index = next(
        (
            entry["index"]
            for entry in entries
            if _execution_status(entry["item"]) == "进行中"
        ),
        None,
    )
    first_unfinished_index = next(
        (
            entry["index"]
            for entry in entries
            if not is_completed(entry["item"])
        ),
        None,
    )

    for entry in entries:
        item = entry["item"]
        status = _execution_status(item)
        entry["can_start"] = (
            running_index is None
            and entry["index"] == first_unfinished_index
            and status == "待执行"
        )
        entry["can_complete"] = running_index == entry["index"] and status == "进行中"
        entry["can_archive"] = status == "已完成"
        entry["can_edit"] = _can_edit_item(item)
        entry["can_delete"] = _can_delete_item(item)
        entry["is_sequence_blocked"] = (
            running_index is None
            and entry["index"] == first_unfinished_index
            and status == "有阻塞"
        )


def _can_start_batch(items: list[dict[str, str]], index: int) -> bool:
    entries = _batch_items_for_view(items, "active")
    return any(entry["index"] == index and entry.get("can_start") for entry in entries)


def _can_complete_batch(items: list[dict[str, str]], index: int) -> bool:
    entries = _batch_items_for_view(items, "active")
    return any(entry["index"] == index and entry.get("can_complete") for entry in entries)


def _execution_status(item: dict[str, str]) -> str:
    return item.get("execution_status") or item.get("status") or "待执行"


def _can_edit_item(item: dict[str, object]) -> bool:
    return _execution_status(item) in {"待执行", "进行中", "有阻塞"}


def _can_delete_item(item: dict[str, object]) -> bool:
    return _execution_status(item) == "待执行"


def _apply_completion_metadata(existing_item: dict[str, str], item: dict[str, str]) -> None:
    status = item.get("execution_status")
    existing_status = existing_item.get("execution_status") or existing_item.get("status")
    if status == "已完成":
        item["completed_on"] = existing_item.get("completed_on") or date.today().isoformat()
    elif existing_status == "已完成":
        item["completed_on"] = ""


def _sync_block_events(
    status_file: Path,
    index: int,
    existing_item: dict[str, object],
    item: dict[str, str],
) -> None:
    old_status = _execution_status(existing_item)
    new_status = _execution_status(item)
    if new_status == "有阻塞":
        reason = item.get("block_reason", "")
        if old_status != "有阻塞" or not has_open_block(status_file, index):
            add_block_event(status_file, index, reason)
        else:
            update_open_block_reason(status_file, index, reason)
        return

    if old_status == "有阻塞":
        close_open_block(status_file, index)


def _attach_metrics(entries: list[dict[str, object]]) -> None:
    for entry in entries:
        item = entry["item"]
        block_total = int(item.get("block_total_seconds") or 0)
        execution_seconds = int(item.get("execution_seconds") or 0)
        item["block_total_duration"] = _format_duration(block_total)
        item["execution_duration"] = _format_duration(execution_seconds)
        item["completed_time_display"] = _format_time_of_day(item.get("completed_at"))
        active_event = item.get("active_block_event")
        if active_event:
            item["active_block_duration"] = _format_duration(
                _duration_seconds(active_event.get("started_at"), _now_iso())
            )
        else:
            item["active_block_duration"] = "-"


def _archive_completed_from_previous_days(
    status_file: Path,
    payload: dict[str, list[dict[str, str]]],
) -> None:
    today = date.today().isoformat()
    changed = False
    for item in payload["batch_plan"]:
        if _is_archived(item):
            continue
        if (item.get("execution_status") or item.get("status")) != "已完成":
            continue
        completed_on = item.get("completed_on")
        if completed_on and completed_on < today:
            item["archived"] = "true"
            item["archived_on"] = today
            changed = True
    if changed:
        for item in payload["batch_plan"]:
            if _is_archived(item) and item.get("archived_on") == today:
                update_item(status_file, "batch_plan", int(item["id"]), item)


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _duration_seconds(started_at: object, ended_at: object) -> int:
    if not started_at or not ended_at:
        return 0
    try:
        started = datetime.fromisoformat(str(started_at))
        ended = datetime.fromisoformat(str(ended_at))
    except ValueError:
        return 0
    return max(0, int((ended - started).total_seconds()))


def _format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "-"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def _format_time_of_day(value: object) -> str:
    if not value:
        return "0点0分"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return "0点0分"
    return f"{parsed.hour}点{parsed.minute}分"


def _format_datetime(value: object) -> str:
    if not value:
        return "未记录"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return parsed.strftime("%Y-%m-%d %H:%M")
