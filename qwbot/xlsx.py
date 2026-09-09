"""Minimal `.xlsx` reader built on the standard library only.

跑批计划表格通常由企业微信/WPS 导出，`styles.xml` 里常有 openpyxl 不接受的写法
（例如 `TypeError: Fill() takes no arguments`），装第三方库也不一定能读出来。
这里只按 OOXML 结构解析工作表：共享字符串、内联字符串、数字，以及按
`numFmt` 把 Excel 序列号还原成日期/时间文本，够用且不引入依赖。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from pathlib import Path

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Excel 1900 date system: day 1 is 1900-01-01, and the fictitious 1900-02-29
# leap day is baked into every serial above 60. 1899-12-30 as day 0 absorbs both.
EXCEL_EPOCH = date(1899, 12, 30)

_BUILTIN_DATE_FORMATS = {
    14, 15, 16, 17,
    *range(27, 37),
    *range(48, 59),
}
_BUILTIN_TIME_FORMATS = {18, 19, 20, 21, 45, 46, 47}
_BUILTIN_DATETIME_FORMATS = {22}


class XlsxError(RuntimeError):
    """Raised when a workbook cannot be read at all."""


def list_sheets(path: str | Path) -> list[str]:
    """Return every worksheet name in the workbook, in tab order."""
    with zipfile.ZipFile(path) as archive:
        return [name for name, _ in _sheet_targets(archive)]


def read_sheet(path: str | Path, sheet: str | None = None) -> list[list[str]]:
    """Return one worksheet as a rectangular grid of text values.

    `sheet` accepts a sheet name; when omitted the first sheet is used. Dates and
    times are converted according to the cell number format, so a `自然日历` cell
    comes back as `2026-09-10` and a `跑批启动时间` cell as `15:00`.
    """
    with zipfile.ZipFile(path) as archive:
        targets = _sheet_targets(archive)
        if not targets:
            raise XlsxError(f"{path} 里没有工作表")
        target = _select_sheet(targets, sheet)
        shared_strings = _shared_strings(archive)
        number_formats = _cell_number_formats(archive)
        rows = _read_rows(archive, target, shared_strings, number_formats)
    return _as_grid(rows)


def _select_sheet(targets: list[tuple[str, str]], sheet: str | None) -> str:
    if not sheet:
        return targets[0][1]
    wanted = _normalize_text(sheet)
    for name, target in targets:
        if _normalize_text(name) == wanted:
            return target
    available = "、".join(name for name, _ in targets)
    raise XlsxError(f"找不到工作表「{sheet}」，可选：{available}")


def _sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relationships = rels.findall(f"{PKG_REL_NS}Relationship")
    targets = {rel.get("Id"): rel.get("Target") or "" for rel in relationships}
    sheets: list[tuple[str, str]] = []
    for element in workbook.iter(f"{NS}sheet"):
        target = targets.get(element.get(f"{REL_NS}id"), "")
        if target:
            sheets.append((element.get("name") or "", _resolve_target(target)))
    return sheets


def _resolve_target(target: str) -> str:
    normalized = target.replace("\\", "/").lstrip("/")
    if normalized.startswith("xl/"):
        return normalized
    return f"xl/{normalized}"


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.iter(f"{NS}t"))
        for item in root.findall(f"{NS}si")
    ]


def _cell_number_formats(archive: zipfile.ZipFile) -> dict[int, str]:
    """Map a cell style index to `date` / `time` / `datetime`.

    Anything not listed is a plain number or text cell. A workbook without a
    readable `styles.xml` simply yields no conversions instead of failing.
    """
    try:
        root = ET.fromstring(archive.read("xl/styles.xml"))
    except (KeyError, ET.ParseError):
        return {}

    custom_codes: dict[int, str] = {}
    for element in root.iter(f"{NS}numFmt"):
        try:
            custom_codes[int(element.get("numFmtId") or 0)] = element.get("formatCode") or ""
        except ValueError:
            continue

    kinds: dict[int, str] = {}
    for format_id, code in custom_codes.items():
        kinds[format_id] = _classify_format_code(code)

    styles: dict[int, str] = {}
    cell_xfs = root.find(f"{NS}cellXfs")
    if cell_xfs is None:
        return {}
    for index, xf in enumerate(cell_xfs.findall(f"{NS}xf")):
        try:
            format_id = int(xf.get("numFmtId") or 0)
        except ValueError:
            continue
        if format_id in kinds:
            kind = kinds[format_id]
        elif format_id in _BUILTIN_DATETIME_FORMATS:
            kind = "datetime"
        elif format_id in _BUILTIN_DATE_FORMATS:
            kind = "date"
        elif format_id in _BUILTIN_TIME_FORMATS:
            kind = "time"
        else:
            continue
        styles[index] = kind
    return styles


def _classify_format_code(code: str) -> str:
    """Classify a custom number format as date, time, datetime or plain."""
    text = (code or "").lower()
    if re.search(r"\[[hms]", text):  # elapsed time, e.g. [h]:mm:ss
        return "time"
    # Drop locale switches ([$-F800]), literal text and escaped characters so
    # only the real placeholders drive the decision.
    stripped = re.sub(r"\[[^\]]*\]", "", text)
    stripped = re.sub(r'"[^"]*"', "", stripped)
    stripped = re.sub(r"\\.", "", stripped)
    has_date = "y" in stripped or "d" in stripped
    has_time = "h" in stripped or "s" in stripped
    if not has_date and not has_time:
        return "date" if "m" in stripped else ""
    if has_date and has_time:
        return "datetime"
    return "date" if has_date else "time"


def _read_rows(
    archive: zipfile.ZipFile,
    target: str,
    shared_strings: list[str],
    number_formats: dict[int, str],
) -> dict[int, dict[int, str]]:
    root = ET.fromstring(archive.read(target))
    rows: dict[int, dict[int, str]] = {}
    for row in root.iter(f"{NS}row"):
        for cell in row.findall(f"{NS}c"):
            column, row_index = _cell_position(cell.get("r"))
            if row_index is None or column is None:
                continue
            value = _cell_value(cell, shared_strings, number_formats)
            if value:
                rows.setdefault(row_index, {})[column] = value
    return rows


def _cell_position(reference: str | None) -> tuple[int | None, int | None]:
    match = re.match(r"([A-Za-z]+)(\d+)", reference or "")
    if not match:
        return None, None
    column = 0
    for char in match.group(1).upper():
        column = column * 26 + (ord(char) - 64)
    return column - 1, int(match.group(2))


def _cell_value(
    cell: ET.Element,
    shared_strings: list[str],
    number_formats: dict[int, str],
) -> str:
    cell_type = cell.get("t") or ""
    if cell_type == "inlineStr":
        inline = cell.find(f"{NS}is")
        if inline is None:
            return ""
        return _normalize_text("".join(node.text or "" for node in inline.iter(f"{NS}t")))

    raw = cell.find(f"{NS}v")
    if raw is None or raw.text is None:
        return ""
    text = raw.text.strip()
    if cell_type == "s":
        try:
            return _normalize_text(shared_strings[int(text)])
        except (ValueError, IndexError):
            return ""
    if cell_type in {"str", "e"}:
        return _normalize_text(text)
    if cell_type == "b":
        return "TRUE" if text == "1" else "FALSE"
    return _format_number(text, _style_kind(cell, number_formats))


def _style_kind(cell: ET.Element, number_formats: dict[int, str]) -> str:
    try:
        style = int(cell.get("s") or 0)
    except ValueError:
        return ""
    return number_formats.get(style, "")


def _format_number(text: str, kind: str) -> str:
    if not kind:
        return _normalize_text(text)
    try:
        serial = float(text)
    except ValueError:
        return _normalize_text(text)
    days = int(serial)
    seconds = round((serial - days) * 86400)
    if seconds >= 86400:  # 23:59:59.6 and friends round up to the next day
        days, seconds = days + 1, 0
    if kind == "date":
        return serial_to_date(days).isoformat()
    time_text = f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"
    if kind == "time":
        return time_text
    return f"{serial_to_date(days).isoformat()} {time_text}"


def serial_to_date(days: int) -> date:
    """Convert an Excel date serial (1900 date system) to a `date`."""
    return EXCEL_EPOCH + timedelta(days=days)


def _as_grid(rows: dict[int, dict[int, str]]) -> list[list[str]]:
    if not rows:
        return []
    width = max(max(cells) for cells in rows.values()) + 1
    return [
        [rows[index].get(column, "") for column in range(width)]
        for index in sorted(rows)
    ]


def _normalize_text(value: str) -> str:
    """Collapse the line breaks WPS puts inside wrapped header cells."""
    return re.sub(r"\s+", " ", (value or "").replace("\r", " ")).strip()
