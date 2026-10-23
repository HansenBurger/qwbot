"""把导出的跑批计划表格同步进 `batch_plan`。

一份表格（如 `UAT2主环境及长周期批量计划.xlsx`）里有多个工作表，每个工作表是
一个环境的跑批日历：一天可能有多个批（长周期一天最多 3 批），每批记录
“自然日历 / 跑批启动时间 / 系统当前会计日期 / 跑批后会计日期”。

同步策略：
- 以「分类 + 自然日历 + 跑批启动时间」作为业务主键，重复导入只更新计划字段，
  不会把已经在页面上点过“开始/完成”的执行状态、执行时间和阻塞记录冲掉。
- 默认只导入今天及以后的批次。历史批次要么跳过，要么按 `past="archive"`
  直接归档，避免它们挡住页面上“只能从最早一条待执行开始”的执行顺序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from qwbot.xlsx import read_sheet, serial_to_date

DEFAULT_SHEET = "长周期"
DEFAULT_CATEGORY = "长周期"
PAST_MODES = ("skip", "archive")

# 表头在不同环境的表格里写法不完全一致（“跑批说明”/“说明”），按前缀匹配。
FIELD_HEADERS = {
    "natural_date": ("自然日历", "自然日", "日期"),
    "batch_start_time": ("跑批启动时间", "计划执行时间", "跑批时间", "启动时间"),
    "current_accounting_date": ("系统当前会计日期", "跑批前交易日期", "当前会计日期"),
    "next_accounting_date": ("跑批后会计日期", "跑批后交易日期", "下一会计日期"),
    "holiday_flag": ("节假日标志",),
    "description": ("跑批说明", "批量说明", "说明", "备注"),
}
IGNORED_HEADERS = ("序号", "星期", "周几")
RATE_CHANGE_HEADERS = ("利率变更标识", "利率变更")
# 表格里用“/”表示当天没有批量。
PLACEHOLDERS = {"", "/", "-", "—", "无", "n/a"}
YES_VALUES = {"y", "yes", "是", "true", "1"}

PLAN_FIELDS = (
    "natural_date",
    "date",
    "batch_start_time",
    "current_accounting_date",
    "next_accounting_date",
    "holiday_flag",
    "description",
    "content",
    "category",
)
# 只在 past="archive" 时一起同步，其余情况保留页面上维护的执行态。
ARCHIVE_FIELDS = ("execution_status", "status", "completed_on", "archived", "archived_on")


@dataclass
class ImportSummary:
    """What one import run did, so the CLI can print a useful report."""

    sheet: str = ""
    category: str = ""
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    # 与 created/updated 重叠：只表示这些历史批次是按“已完成 + 已归档”写进去的。
    archived_past: list[str] = field(default_factory=list)
    skipped_past: list[str] = field(default_factory=list)
    skipped_rows: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def changed(self) -> int:
        """Rows actually written; `archived_past` overlaps with created/updated."""
        return len(self.created) + len(self.updated)

    def as_text(self) -> str:
        prefix = "（预演，未写库）" if self.dry_run else ""
        counts = (
            f"新增 {len(self.created)} 条，更新 {len(self.updated)} 条，"
            f"无变化 {len(self.unchanged)} 条。"
        )
        lines = [f"工作表：{self.sheet or '-'}　分类：{self.category or '-'}{prefix}", counts]
        if self.archived_past:
            lines.append(f"今天之前的批次按已完成归档 {len(self.archived_past)} 条。")
        if self.skipped_past:
            lines.append(
                f"跳过今天之前的批次 {len(self.skipped_past)} 条"
                f"（{self.skipped_past[0]} ~ {self.skipped_past[-1]}），"
                + "需要保留历史可用 --past archive。"
            )
        if self.skipped_rows:
            lines.append(
                f"忽略无法识别的行 {len(self.skipped_rows)} 条：{'、'.join(self.skipped_rows[:5])}"
            )
        return "\n".join(lines)


def load_plan_rows(
    path: str | Path,
    sheet: str | None = DEFAULT_SHEET,
    *,
    category: str = DEFAULT_CATEGORY,
    keep_source_holiday_flag: bool = False,
) -> tuple[list[dict[str, str]], list[str]]:
    """Read one worksheet into `batch_plan` item dicts.

    Returns the rows plus the labels of the rows that were ignored (no usable
    natural date, e.g. a day without any batch in the source sheet).
    """
    grid = read_sheet(path, sheet)
    if not grid:
        return [], []
    columns = _map_columns(grid[0])
    rows: list[dict[str, str]] = []
    ignored: list[str] = []
    for number, raw in enumerate(grid[1:], start=2):
        item = _row_to_item(raw, columns, category, keep_source_holiday_flag)
        if item is None:
            ignored.append(f"第{number}行")
            continue
        rows.append(item)
    return rows, ignored


def import_plan_rows(
    db_path: Path,
    rows: list[dict[str, str]],
    ignored: list[str] | None = None,
    *,
    sheet: str = DEFAULT_SHEET,
    category: str = DEFAULT_CATEGORY,
    past: str = "skip",
    today: date | None = None,
    dry_run: bool = False,
) -> ImportSummary:
    """Upsert plan rows into the database; see the module docstring for the rules.

    P1: the sync decides every row first, then writes on one connection inside one
    transaction via `store.write_items`. Going row by row through `add_item` /
    `update_item` meant one connect + schema pass + COMMIT(fsync) per batch — about
    10 ms/row, so a 400-row sheet took ~4 s. Now it is a single fsync (~20 ms), and any
    error rolls the entire import back instead of leaving half a sheet applied. A dry run
    never opens a write transaction at all.
    """
    from qwbot.store import load_status_file, write_items

    if past not in PAST_MODES:
        raise ValueError(f"past 只能是 {'/'.join(PAST_MODES)}")
    today = today or date.today()
    today_text = today.isoformat()
    summary = ImportSummary(
        sheet=sheet, category=category, skipped_rows=list(ignored or []), dry_run=dry_run
    )
    existing = {_business_key(item): item for item in load_status_file(db_path)["batch_plan"]}

    # Decide everything first, then write: a dry run never opens a write transaction,
    # and a real run commits exactly once at the end.
    inserts: list[dict[str, Any]] = []
    updates: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        natural_date = row["natural_date"]
        label = f"{natural_date} {row['batch_start_time'] or '--:--'}"
        is_past = natural_date < today_text
        if is_past and past == "skip":
            summary.skipped_past.append(label)
            continue
        if is_past:
            row = {**row, **_archived_past_fields(natural_date, today)}
        synced_fields = PLAN_FIELDS + (ARCHIVE_FIELDS if is_past else ())

        current = existing.get(_business_key(row))
        if current is None:
            summary.created.append(label)
            if is_past:
                summary.archived_past.append(label)
            inserts.append(row)
            continue

        merged = {**current, **{name: row[name] for name in synced_fields if name in row}}
        if _same_fields(current, merged, synced_fields):
            summary.unchanged.append(label)
            continue
        summary.updated.append(label)
        if is_past:
            summary.archived_past.append(label)
        updates.append((int(current["id"]), merged))

    if dry_run or not (inserts or updates):
        return summary

    write_items(db_path, "batch_plan", inserts=inserts, updates=updates)
    return summary


def import_plan_file(
    db_path: Path,
    path: str | Path,
    *,
    sheet: str | None = DEFAULT_SHEET,
    category: str | None = None,
    past: str = "skip",
    today: date | None = None,
    dry_run: bool = False,
    keep_source_holiday_flag: bool = False,
) -> ImportSummary:
    """Read one workbook sheet and sync it into `batch_plan` in a single call."""
    sheet_name = sheet or DEFAULT_SHEET
    resolved_category = category or sheet_name or DEFAULT_CATEGORY
    rows, ignored = load_plan_rows(
        path,
        sheet_name,
        category=resolved_category,
        keep_source_holiday_flag=keep_source_holiday_flag,
    )
    return import_plan_rows(
        db_path,
        rows,
        ignored,
        sheet=sheet_name,
        category=resolved_category,
        past=past,
        today=today,
        dry_run=dry_run,
    )


def _map_columns(header: list[str]) -> dict[int, str]:
    """Map column index -> field name; unmapped columns become `notes:<表头>`.

    Every field binds to its first matching column only, so a sheet carrying both
    “说明” and “备注” keeps one description plus one extra note.
    """
    columns: dict[int, str] = {}
    taken: set[str] = set()
    for index, raw in enumerate(header):
        name = _normalize_header(raw)
        if not name or _matches(name, IGNORED_HEADERS):
            continue
        if _matches(name, RATE_CHANGE_HEADERS):
            columns[index] = "rate_change"
            continue
        field_name = next(
            (
                candidate
                for candidate, aliases in FIELD_HEADERS.items()
                if candidate not in taken and _matches(name, aliases)
            ),
            None,
        )
        if field_name:
            columns[index] = field_name
            taken.add(field_name)
        else:
            columns[index] = f"notes:{_display_header(raw)}"
    return columns


def _matches(name: str, aliases: tuple[str, ...]) -> bool:
    return any(name == alias or name.startswith(alias) for alias in aliases)


def _row_to_item(
    raw: list[str],
    columns: dict[int, str],
    category: str,
    keep_source_holiday_flag: bool,
) -> dict[str, str] | None:
    values: dict[str, str] = {}
    notes: list[str] = []
    for index, field_name in columns.items():
        value = (raw[index] if index < len(raw) else "").strip()
        if value.lower() in PLACEHOLDERS:
            value = ""
        if field_name == "rate_change":
            if value.lower() in YES_VALUES:
                notes.append("利率变更")
            continue
        if field_name.startswith("notes:"):
            if value:
                notes.append(f"{field_name.split(':', 1)[1]}：{value}")
            continue
        values.setdefault(field_name, value)

    natural_date = _normalize_date(values.get("natural_date", ""))
    if not natural_date:
        return None

    if values.get("holiday_flag", "").lower() in YES_VALUES and not keep_source_holiday_flag:
        # 源表的“节假日标志”说的是批后会计日期落在节假日，不是自然日是节假日；
        # 直接写进 holiday_flag 会让调度器把这些工作日当成节假日跳过，所以只记进说明。
        notes.insert(0, "批后会计日期为节假日")

    description = values.get("description", "") or f"{category}跑批"
    return {
        "category": category,
        "natural_date": natural_date,
        "date": natural_date,
        "batch_start_time": _normalize_time(values.get("batch_start_time", "")),
        "current_accounting_date": _normalize_date(values.get("current_accounting_date", "")),
        "next_accounting_date": _normalize_date(values.get("next_accounting_date", "")),
        "holiday_flag": _normalize_flag(values.get("holiday_flag", ""))
        if keep_source_holiday_flag
        else "N",
        "description": "\n".join([description, *notes]),
        "content": description,
        "execution_status": "待执行",
        "status": "待执行",
        "requester": "",
    }


def _archived_past_fields(natural_date: str, today: date) -> dict[str, str]:
    return {
        "execution_status": "已完成",
        "status": "已完成",
        "completed_on": natural_date,
        "archived": "true",
        "archived_on": today.isoformat(),
    }


def _business_key(item: dict[str, Any]) -> tuple[str, str, str]:
    """Identity of one batch: environment + natural day + planned start time."""
    return (
        str(item.get("category") or ""),
        str(item.get("natural_date") or item.get("date") or ""),
        str(item.get("batch_start_time") or ""),
    )


def _same_fields(current: dict[str, Any], merged: dict[str, Any], fields: tuple[str, ...]) -> bool:
    return all(str(current.get(name) or "") == str(merged.get(name) or "") for name in fields)


def _normalize_header(value: str) -> str:
    return "".join((value or "").split()).replace("（", "(").replace("）", ")")


def _display_header(value: str) -> str:
    return " ".join((value or "").split())


def _normalize_date(value: str) -> str:
    """Accept `2026-09-10`, `2026/9/10`, `2026年9月10日` and Excel serials."""
    text = (value or "").strip()
    if not text or text.lower() in PLACEHOLDERS:
        return ""
    if " " in text:  # a datetime cell: only the date part matters here
        text = text.split(" ", 1)[0]
    digits = (
        text.replace(".", "/")
        .replace("-", "/")
        .replace("年", "/")
        .replace("月", "/")
        .replace("日", "")
    )
    parts = [part for part in digits.split("/") if part]
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        try:
            return date(*(int(part) for part in parts)).isoformat()
        except ValueError:
            return ""
    try:
        serial = float(text)
    except ValueError:
        return ""
    if 20000 < serial < 80000:  # 1954-07-05 ~ 2118-10-04, a sane planning horizon
        return serial_to_date(int(serial)).isoformat()
    return ""


def _normalize_time(value: str) -> str:
    """Accept `15:00`, `15:00:00` and Excel day fractions such as 0.625."""
    text = (value or "").strip()
    if not text or text.lower() in PLACEHOLDERS:
        return ""
    if ":" in text:
        parts = text.split(":")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return f"{min(23, int(parts[0])):02d}:{min(59, int(parts[1])):02d}"
        return ""
    try:
        fraction = float(text)
    except ValueError:
        return ""
    if not 0 <= fraction < 1:
        return ""
    seconds = round(fraction * 86400)
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def _normalize_flag(value: str) -> str:
    return "Y" if (value or "").strip().lower() in YES_VALUES else "N"
