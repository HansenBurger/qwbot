from __future__ import annotations

from datetime import date
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

from qwbot.config import Settings
from qwbot.message import build_batch_complete_notice, build_batch_start_notice
from qwbot.planner import is_completed, next_batch_after, sort_key
from qwbot.store import add_item, delete_item, load_status_file, save_status_file, update_item
from qwbot.wecom import WeComWebhookClient


def create_app(settings: Settings) -> Flask:
    app = Flask(__name__)
    status_file = settings.local_status_file

    @app.get("/")
    def index():
        payload = load_status_file(status_file)
        _archive_completed_from_previous_days(status_file, payload)
        view = request.args.get("view", "active")
        batch_plan = _batch_items_for_view(payload["batch_plan"], view)
        return render_template(
            "index.html",
            batch_plan=batch_plan,
            blocked_plan=_blocked_items(payload["batch_plan"]),
            active_count=len(_batch_items_for_view(payload["batch_plan"], "active")),
            archived_count=len(_batch_items_for_view(payload["batch_plan"], "archived")),
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
        payload = load_status_file(status_file)
        existing_item = payload[collection][index]
        if collection == "batch_plan" and _is_archived(existing_item):
            return redirect(url_for("index", view="archived"))
        item = _item_from_form()
        _apply_completion_metadata(existing_item, item)
        update_item(status_file, collection, index, item)
        return redirect(url_for("index"))

    @app.post("/items/<collection>/<int:index>/delete")
    def remove_item(collection: str, index: int):
        if collection == "batch_plan" and _is_archived(load_status_file(status_file)[collection][index]):
            return redirect(url_for("index", view="archived"))
        delete_item(status_file, collection, index)
        return redirect(url_for("index"))

    @app.post("/items/batch_plan/<int:index>/archive")
    def archive_item(index: int):
        payload = load_status_file(status_file)
        item = payload["batch_plan"][index]
        item["archived"] = "true"
        item["archived_on"] = date.today().isoformat()
        save_status_file(status_file, payload)
        return redirect(url_for("index"))

    @app.post("/items/batch_plan/<int:index>/start")
    def start_item(index: int):
        payload = load_status_file(status_file)
        item = payload["batch_plan"][index]
        if _is_archived(item):
            return redirect(url_for("index", view="archived"))
        if not _can_start_batch(payload["batch_plan"], index):
            return redirect(url_for("index"))

        item["execution_status"] = "进行中"
        item["started_on"] = date.today().isoformat()
        save_status_file(status_file, payload)
        WeComWebhookClient(settings.webhook_url).send_text(
            build_batch_start_notice(item),
            mentioned_list=["@all"],
        )
        return redirect(url_for("index"))

    @app.post("/items/batch_plan/<int:index>/complete")
    def complete_item(index: int):
        payload = load_status_file(status_file)
        item = payload["batch_plan"][index]
        if _is_archived(item):
            return redirect(url_for("index", view="archived"))
        if not _can_complete_batch(payload["batch_plan"], index):
            return redirect(url_for("index"))

        item["execution_status"] = "已完成"
        item["completed_on"] = date.today().isoformat()
        next_entry = next_batch_after(payload["batch_plan"], index)
        save_status_file(status_file, payload)
        WeComWebhookClient(settings.webhook_url).send_text(
            build_batch_complete_notice(item, next_entry[1] if next_entry else None),
        )
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


def _batch_items_for_view(items: list[dict[str, str]], view: str) -> list[dict[str, object]]:
    archived = view == "archived"
    indexed_items = [
        {"index": index, "item": item}
        for index, item in enumerate(items)
        if _is_archived(item) is archived
    ]
    sorted_items = sorted(indexed_items, key=lambda entry: sort_key(entry["item"]))
    if not archived:
        _attach_execution_permissions(sorted_items)
    return sorted_items


def _is_archived(item: dict[str, str]) -> bool:
    return item.get("archived") == "true"


def _blocked_items(items: list[dict[str, str]]) -> list[dict[str, object]]:
    indexed_items = [
        {"index": index, "item": item}
        for index, item in enumerate(items)
        if not _is_archived(item) and (item.get("execution_status") or item.get("status")) == "有阻塞"
    ]
    return sorted(indexed_items, key=lambda entry: sort_key(entry["item"]))


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


def _apply_completion_metadata(existing_item: dict[str, str], item: dict[str, str]) -> None:
    status = item.get("execution_status")
    existing_status = existing_item.get("execution_status") or existing_item.get("status")
    if status == "已完成":
        item["completed_on"] = existing_item.get("completed_on") or date.today().isoformat()
    elif existing_status == "已完成":
        item["completed_on"] = ""


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
        save_status_file(status_file, payload)
