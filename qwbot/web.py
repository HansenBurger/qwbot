from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, redirect, render_template, request, url_for

from qwbot.config import Settings
from qwbot.message import build_batch_complete_notice, build_batch_start_notice, build_custom_notification
from qwbot.planner import is_business_day, is_completed, next_batch_after, sort_key
from qwbot.store import (
    DEFAULT_REMINDER_TEMPLATE,
    REMINDER_TEMPLATE_VARS,
    add_block_event,
    add_item,
    add_scheduler_force_date,
    add_scheduler_skip_date,
    add_scheduler_time,
    close_open_block,
    delete_scheduler_force_date,
    delete_scheduler_skip_date,
    delete_scheduler_time,
    delete_item,
    get_item,
    get_reminder_template,
    has_open_block,
    init_store,
    load_status_file,
    set_notification_date_override,
    set_reminder_template,
    update_scheduler_time,
    update_item,
    update_open_block_reason,
)
from qwbot.wecom import WeComWebhookClient

DATE_RULE_LABELS = {
    "all": "所有自然日",
    "business_day": "跳过周末和节假日",
    "holiday_only": "仅跳过节假日",
}


def create_app(settings: Settings) -> Flask:
    app = Flask(__name__)
    status_file = settings.db_path
    init_store(status_file, settings.local_status_file)

    @app.get("/")
    def index():
        payload = load_status_file(status_file)
        _archive_completed_from_previous_days(status_file, payload, settings.timezone)
        view = request.args.get("view", "active")
        filters = _archive_filters_from_request()
        if view == "archived" and not _has_archive_filter_args():
            filters = _default_archive_filters(payload["batch_plan"], settings.timezone)
        all_archived_entries = _batch_items_for_view(
            payload["batch_plan"],
            "archived",
            timezone=settings.timezone,
        )
        archived_entries = _batch_items_for_view(
            payload["batch_plan"],
            "archived",
            filters,
            settings.timezone,
        )
        active_entries = _batch_items_for_view(
            payload["batch_plan"],
            "active",
            timezone=settings.timezone,
        )
        pagination = _paginate_entries(
            archived_entries if view == "archived" else active_entries,
            page=_int_arg("page", 1),
            per_page=10 if view == "archived" else 1000,
        )
        return render_template(
            "index.html",
            batch_plan=pagination["items"],
            blocked_plan=_blocked_items(payload["batch_plan"], settings.timezone),
            active_count=len(active_entries),
            archived_count=len(all_archived_entries),
            archive_filters=filters,
            pagination=pagination,
            today=_today_iso(settings.timezone),
            view=view,
            status_file=_display_path(status_file),
        )

    @app.get("/notifications")
    def notification_page():
        payload = load_status_file(status_file)
        notification_tasks = _notification_tasks_for_view(
            payload["notification_tasks"],
            payload["batch_plan"],
            settings.timezone,
        )
        notification_pagination = _paginate_entries(
            notification_tasks,
            page=_int_arg("notification_page", 1),
            per_page=5,
        )
        return render_template(
            "notifications.html",
            scheduler_times=payload["scheduler_times"],
            notification_tasks=notification_pagination["items"],
            notification_pagination=notification_pagination,
            reminder_dates=_reminder_date_entries(
                payload["batch_plan"],
                payload["scheduler_skip_dates"],
                payload["scheduler_force_dates"],
                settings.timezone,
            ),
            current_hour=_current_hour(settings.timezone),
            current_time=_current_time(settings.timezone),
            reminder_template=get_reminder_template(status_file),
            default_reminder_template=DEFAULT_REMINDER_TEMPLATE,
            reminder_template_vars=REMINDER_TEMPLATE_VARS,
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
        _apply_completion_metadata(existing_item, item, settings.timezone)
        update_item(status_file, collection, index, item)
        if collection == "batch_plan":
            _sync_block_events(status_file, index, existing_item, item, settings.timezone)
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
        item["archived_on"] = _today_iso(settings.timezone)
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
        item["started_on"] = _today_iso(settings.timezone)
        item["started_at"] = _now_iso(settings.timezone)
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
        item["completed_on"] = _today_iso(settings.timezone)
        item["completed_at"] = _now_iso(settings.timezone)
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

        close_open_block(status_file, index, _now_iso(settings.timezone))
        item["execution_status"] = "进行中"
        item["block_reason"] = ""
        update_item(status_file, "batch_plan", index, item)
        return redirect(url_for("index"))

    @app.post("/notifications")
    def create_notification():
        add_item(status_file, "notification_tasks", _notification_from_form())
        return redirect(url_for("notification_page"))

    @app.post("/notifications/<int:index>")
    def edit_notification(index: int):
        update_item(status_file, "notification_tasks", index, _notification_from_form())
        return redirect(url_for("notification_page"))

    @app.post("/notifications/<int:index>/delete")
    def remove_notification(index: int):
        delete_item(status_file, "notification_tasks", index)
        return redirect(url_for("notification_page"))

    @app.post("/notifications/<int:index>/date-rule")
    def edit_notification_date_rule(index: int):
        task = get_item(status_file, "notification_tasks", index)
        task["date_rule"] = _date_rule_from_form()
        update_item(status_file, "notification_tasks", index, task)
        return redirect(url_for("notification_page"))

    @app.post("/notifications/<int:index>/send-now")
    def send_notification_now(index: int):
        task = get_item(status_file, "notification_tasks", index)
        WeComWebhookClient(settings.webhook_url).send_markdown(build_custom_notification(task))
        return redirect(url_for("notification_page"))

    @app.post("/notifications/<int:index>/skip-date")
    def skip_notification_date(index: int):
        set_notification_date_override(status_file, index, request.form.get("target_date", ""), "skip")
        return redirect(url_for("notification_page"))

    @app.post("/notifications/<int:index>/force-date")
    def force_notification_date(index: int):
        set_notification_date_override(status_file, index, request.form.get("target_date", ""), "force")
        return redirect(url_for("notification_page"))

    @app.post("/scheduler-times/<reminder_id>")
    def edit_scheduler_time(reminder_id: str):
        update_scheduler_time(status_file, reminder_id, _send_time_from_form())
        return redirect(url_for("notification_page"))

    @app.post("/scheduler-times")
    def create_scheduler_time():
        add_scheduler_time(status_file, _send_time_from_form())
        return redirect(url_for("notification_page"))

    @app.post("/scheduler-times/<reminder_id>/delete")
    def remove_scheduler_time(reminder_id: str):
        delete_scheduler_time(status_file, reminder_id)
        return redirect(url_for("notification_page"))

    @app.post("/scheduler-skip-dates")
    def create_scheduler_skip_date():
        add_scheduler_skip_date(status_file, request.form.get("skip_date", ""))
        return redirect(url_for("notification_page"))

    @app.post("/scheduler-skip-dates/delete")
    def remove_scheduler_skip_date():
        delete_scheduler_skip_date(status_file, request.form.get("skip_date", ""))
        return redirect(url_for("notification_page"))

    @app.post("/scheduler-force-dates")
    def create_scheduler_force_date():
        add_scheduler_force_date(status_file, request.form.get("force_date", ""))
        return redirect(url_for("notification_page"))

    @app.post("/scheduler-force-dates/delete")
    def remove_scheduler_force_date():
        delete_scheduler_force_date(status_file, request.form.get("force_date", ""))
        return redirect(url_for("notification_page"))

    @app.post("/reminder-template")
    def save_reminder_template():
        template = request.form.get("template", "").strip()
        if template:
            set_reminder_template(status_file, template)
        return redirect(url_for("notification_page"))

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


def _has_archive_filter_args() -> bool:
    return any(name in request.args for name in ("natural_date", "accounting_date", "holiday_flag"))


def _default_archive_filters(
    items: list[dict[str, object]],
    timezone: str = "Asia/Shanghai",
) -> dict[str, str]:
    archived_dates = sorted(
        {
            str(item.get("natural_date") or item.get("date") or "")
            for item in items
            if _is_archived(item) and (item.get("natural_date") or item.get("date"))
        }
    )
    if not archived_dates:
        return {"natural_date": "", "accounting_date": "", "holiday_flag": ""}

    today = _today_iso(timezone)
    natural_date = today if today in archived_dates else archived_dates[-1]
    return {"natural_date": natural_date, "accounting_date": "", "holiday_flag": ""}


def _notification_from_form() -> dict[str, str]:
    return {
        "title": request.form.get("title", ""),
        "content": request.form.get("content", ""),
        "doc_links": _doc_links_from_form(),
        "send_time": _send_time_from_form(),
        "at_all": "true" if request.form.get("at_all") == "true" else "",
        "date_rule": _date_rule_from_form(),
    }


def _doc_links_from_form() -> list[dict[str, str]]:
    labels = request.form.getlist("doc_label")
    urls = request.form.getlist("doc_url")
    return [
        {"label": label.strip(), "url": url.strip()}
        for label, url in zip(labels, urls)
        if url.strip()
    ]


def _send_time_from_form() -> str:
    send_time = request.form.get("send_time", "").strip()
    parts = send_time.split(":")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        hour = max(0, min(23, int(parts[0])))
        minute = max(0, min(59, int(parts[1])))
        return f"{hour:02d}:{minute:02d}"
    return "00:00"


def _current_hour(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).strftime("%H")


def _current_time(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).strftime("%H:%M")


def _doc_links_json(doc_links: object) -> str:
    if not isinstance(doc_links, list):
        return "[]"
    return json.dumps(doc_links, ensure_ascii=False)


def _notification_tasks_for_view(
    tasks: list[dict[str, object]],
    batch_plan: list[dict[str, str]] | None = None,
    timezone: str | None = None,
) -> list[dict[str, object]]:
    for task in tasks:
        date_rule = _normalize_date_rule(str(task.get("date_rule") or ""))
        task["date_rule"] = date_rule
        task["date_rule_label"] = DATE_RULE_LABELS[date_rule]
    return sorted(tasks, key=_notification_sort_key)


def _notification_sort_key(task: dict[str, object]) -> tuple[str, int]:
    try:
        task_id = int(task.get("id") or 0)
    except (TypeError, ValueError):
        task_id = 0
    return (str(task.get("send_time") or "99:99"), task_id)


def _reminder_date_entries(
    batch_plan: list[dict[str, str]],
    skip_dates: list[dict[str, str]],
    force_dates: list[dict[str, str]],
    timezone: str,
    days: int = 10,
) -> list[dict[str, object]]:
    today = datetime.now(ZoneInfo(timezone)).date()
    skipped = {item["date"] for item in skip_dates}
    forced = {item["date"] for item in force_dates}
    entries = []
    for offset in range(days):
        current = today + timedelta(days=offset)
        current_text = current.isoformat()
        default_remind = is_business_day(batch_plan, current)
        is_skipped = current_text in skipped
        is_forced = current_text in forced
        will_remind = is_forced or (default_remind and not is_skipped)
        entries.append(
            {
                "date": current_text,
                "day": current.strftime("%m-%d"),
                "weekday": _weekday_label(current.weekday()),
                "is_today": offset == 0,
                "is_skipped": not will_remind,
                "is_forced": is_forced,
                "default_remind": default_remind,
                "will_remind": will_remind,
            }
        )
    return entries


def _date_rule_from_form() -> str:
    return _normalize_date_rule(request.form.get("date_rule", ""))


def _normalize_date_rule(value: str) -> str:
    return value if value in DATE_RULE_LABELS else "business_day"


def _weekday_label(weekday: int) -> str:
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][weekday]


def _int_arg(name: str, default: int) -> int:
    try:
        return max(1, int(request.args.get(name, default)))
    except (TypeError, ValueError):
        return default


def _batch_items_for_view(
    items: list[dict[str, object]],
    view: str,
    filters: dict[str, str] | None = None,
    timezone: str = "Asia/Shanghai",
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
    _attach_metrics(sorted_items, timezone)
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


def _blocked_items(items: list[dict[str, object]], timezone: str = "Asia/Shanghai") -> list[dict[str, object]]:
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
                timezone=timezone,
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


def _block_event_entry(
    batch_index: int,
    event: dict[str, object],
    timezone: str = "Asia/Shanghai",
) -> dict[str, object]:
    is_open = not event.get("ended_at")
    duration_seconds = (
        _duration_seconds(event.get("started_at"), _now_iso(timezone))
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


def _apply_completion_metadata(
    existing_item: dict[str, str],
    item: dict[str, str],
    timezone: str = "Asia/Shanghai",
) -> None:
    status = item.get("execution_status")
    existing_status = existing_item.get("execution_status") or existing_item.get("status")
    if status == "已完成":
        completed_at = existing_item.get("completed_at") or _now_iso(timezone)
        item["completed_on"] = existing_item.get("completed_on") or _today_iso(timezone)
        item["completed_at"] = completed_at
        item["execution_seconds"] = str(_duration_seconds(existing_item.get("started_at"), completed_at))
    elif existing_status == "已完成":
        item["completed_on"] = ""
        item["completed_at"] = ""
        item["execution_seconds"] = "0"


def _sync_block_events(
    status_file: Path,
    index: int,
    existing_item: dict[str, object],
    item: dict[str, str],
    timezone: str = "Asia/Shanghai",
) -> None:
    old_status = _execution_status(existing_item)
    new_status = _execution_status(item)
    if new_status == "有阻塞":
        reason = item.get("block_reason", "")
        if old_status != "有阻塞" or not has_open_block(status_file, index):
            add_block_event(status_file, index, reason, _now_iso(timezone))
        else:
            update_open_block_reason(status_file, index, reason)
        return

    if old_status == "有阻塞":
        close_open_block(status_file, index, _now_iso(timezone))


def _attach_metrics(entries: list[dict[str, object]], timezone: str = "Asia/Shanghai") -> None:
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
                _duration_seconds(active_event.get("started_at"), _now_iso(timezone))
            )
        else:
            item["active_block_duration"] = "-"


def _archive_completed_from_previous_days(
    status_file: Path,
    payload: dict[str, list[dict[str, str]]],
    timezone: str = "Asia/Shanghai",
) -> None:
    today = _today_iso(timezone)
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


def _today_iso(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).date().isoformat()


def _now_iso(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).replace(microsecond=0, tzinfo=None).isoformat()


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
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return "-"
    return f"{parsed.hour}点{parsed.minute}分"


def _format_datetime(value: object) -> str:
    if not value:
        return "未记录"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return parsed.strftime("%Y-%m-%d %H:%M")
