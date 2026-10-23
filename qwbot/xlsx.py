"""Minimal `.xlsx` reader built on the standard library only.

跑批计划表格通常由企业微信/WPS 导出，`styles.xml` 里常有 openpyxl 不接受的写法
（例如 `TypeError: Fill() takes no arguments`），装第三方库也不一定能读出来。
这里只按 OOXML 结构解析工作表：共享字符串、内联字符串、数字，以及按
`numFmt` 把 Excel 序列号还原成日期/时间文本，够用且不引入依赖。

SEC-02：`.xlsx` 是 zip，压缩比可以很高（实测 37 KB 解压出 12 MB，放大 335 倍；
~500 MB 载荷能把进程卡死）。所以读取任何成员前先按目录项里的**解压后**大小做预算
校验，超过上限直接拒绝；单个工作表的行数/列数也有上限，避免恶意宽表在 `_as_grid`
里放大内存分配。
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

# SEC-02 budgets. A real multi-year plan sheet is a few hundred KB uncompressed, so
# 64 MiB of total expanded content leaves ~100x headroom while still refusing a bomb.
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = MAX_EXPANDED_BYTES
# Deflate cannot expand its input: the worst case is ~1032 bytes of output per byte of
# compressed data (empty-block overhead). A stored member has no such ratio — its
# compressed size already IS its content — so it gets a small slack for headers.
_DEFLATE_MAX_EXPANSION = 1033
_STORED_SLACK = 1024
# Geometry caps: a sparse sheet can claim row 900000 or column XFE with a handful of
# bytes, and `_as_grid` would then build a rectangular grid for all of it.
MAX_SHEET_ROWS = 200_000
MAX_SHEET_COLUMNS = 16_384  # the widest sheet Excel itself allows (XFD)

# The parts the reader pulls: workbook metadata, its relationships, the style/string
# tables and every worksheet. Media and docProps are ignored, so they do not gate a read.
_REQUIRED_PARTS = frozenset(
    {
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/styles.xml",
        "xl/sharedStrings.xml",
    }
)


def _member_ceiling(info: zipfile.ZipInfo) -> int:
    """The most a member could realistically yield, from what the archive holds for it.

    A deflate stream cannot expand its own input: the worst case is ~1032 bytes out per
    byte in (empty-block overhead). A stored member has no such ratio — its compressed
    size already IS its content, plus header slack. Either way this bounds the damage of
    a central directory that under-reports `file_size`, without reading the payload.
    """
    if info.compress_type == zipfile.ZIP_STORED:
        return info.compress_size + _STORED_SLACK
    return _DEFLATE_MAX_EXPANSION * max(info.compress_size, 1)


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
        # Listing reads only the workbook metadata, so it stays available even for a
        # workbook `read_sheet` will refuse — the budget gates expansion, not discovery.
        return [name for name, _ in _sheet_targets(archive, path=path)]


def read_sheet(path: str | Path, sheet: str | None = None) -> list[list[str]]:
    """Return one worksheet as a rectangular grid of text values.

    `sheet` accepts a sheet name; when omitted the first sheet is used. Dates and
    times are converted according to the cell number format, so a `自然日历` cell
    comes back as `2026-09-10` and a `跑批启动时间` cell as `15:00`.

    Raises `XlsxError` before expanding anything when the workbook exceeds the
    SEC-02 size budgets (zip bomb protection).
    """
    with zipfile.ZipFile(path) as archive:
        _check_zip_budget(archive, path=path)
        targets = _sheet_targets(archive, path=path)
        if not targets:
            raise XlsxError(f"{path} 里没有工作表")
        target = _select_sheet(targets, sheet)
        shared_strings = _shared_strings(archive, path=path)
        number_formats = _cell_number_formats(archive, path=path)
        # A malformed worksheet raises `ET.ParseError` here rather than during
        # `fromstring`; it propagates unchanged so the CLI keeps reporting it the way it
        # always did (only size/geometry refusals are `XlsxError`).
        rows = _read_rows(archive, target, shared_strings, number_formats, path=path)
    return _as_grid(rows)


def _check_zip_budget(archive: zipfile.ZipFile, *, path: str | Path = "?") -> None:
    """SEC-02: refuse workbooks that would expand past the budget.

    `ZipInfo.file_size` is the *uncompressed* length recorded in the central
    directory, so this costs nothing — the payload is never touched here. The total
    covers every member (a real workbook's non-sheet parts are negligible, and a bomb
    can hide in any of them); encryption is only rejected for the parts we would read.
    """
    total = 0
    for info in archive.infolist():
        # `file_size` is only what the directory *claims*; a crafted zip can under-report
        # it. Masking keeps the budget check honest without ever touching the payload:
        # a real deflate stream cannot yield more than its own compressed bytes + ~1032x
        # (deflate's worst-case expansion), so a huge true size behind a tiny claim
        # cannot fit in a small file and is caught here rather than streamed.
        possible = min(info.file_size, _member_ceiling(info))
        total += possible
        if not _is_read_part(info.filename):
            continue
        if info.flag_bits & 0x1:
            raise XlsxError(f"{path} 包含加密的工作簿内容，无法读取。")
        if possible > MAX_MEMBER_BYTES:
            raise XlsxError(
                f"{path} 的内容过大：{info.filename} 解压后有 {_human_bytes(possible)}，"
                f"超过上限 {_human_bytes(MAX_MEMBER_BYTES)}。"
            )
    if total > MAX_EXPANDED_BYTES:
        raise XlsxError(
            f"{path} 的内容过大：解压后合计 {_human_bytes(total)}，"
            f"超过上限 {_human_bytes(MAX_EXPANDED_BYTES)}（疑似 zip bomb）。"
        )


def _read_part(archive: zipfile.ZipFile, name: str, *, path: str | Path) -> bytes:
    """Read one member whole, refusing to expand it past the SEC-02 budget.

    Worksheet parts are parsed in full (`ET.fromstring`), so they must be materialised;
    only the sheet *rows* stream (see `_read_rows`). Two things keep this honest:

    * encryption is re-checked, because an encrypted entry's recorded sizes describe the
      ciphertext rather than what would come out;
    * the read is capped at the ceiling computed in `_check_zip_budget` plus one byte —
      getting that extra byte proves the central directory lied about the content.

    A missing part raises KeyError, which callers handle as before.
    """
    info = archive.getinfo(name)
    if info.flag_bits & 0x1:
        raise XlsxError(f"{path} 包含加密的工作簿内容，无法读取。")
    ceiling = min(MAX_MEMBER_BYTES, _member_ceiling(info))
    with archive.open(name) as member:
        data = member.read(ceiling + 1)
    if len(data) > ceiling:
        raise XlsxError(
            f"{path} 的内容过大：{name} 解压后超过上限 {_human_bytes(MAX_MEMBER_BYTES)}。"
        )
    return data


def _is_read_part(filename: str) -> bool:
    """Whether `_read_part` would pull this member in (the parts we actually parse)."""
    return filename in _REQUIRED_PARTS or filename.startswith("xl/worksheets/")


def _human_bytes(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value} B"


def _select_sheet(targets: list[tuple[str, str]], sheet: str | None) -> str:
    if not sheet:
        return targets[0][1]
    wanted = _normalize_text(sheet)
    for name, target in targets:
        if _normalize_text(name) == wanted:
            return target
    available = "、".join(name for name, _ in targets)
    raise XlsxError(f"找不到工作表「{sheet}」，可选：{available}")


def _sheet_targets(archive: zipfile.ZipFile, *, path: str | Path = "?") -> list[tuple[str, str]]:
    workbook = ET.fromstring(_read_part(archive, "xl/workbook.xml", path=path))
    rels = ET.fromstring(_read_part(archive, "xl/_rels/workbook.xml.rels", path=path))
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


def _shared_strings(archive: zipfile.ZipFile, *, path: str | Path = "?") -> list[str]:
    try:
        root = ET.fromstring(_read_part(archive, "xl/sharedStrings.xml", path=path))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.iter(f"{NS}t"))
        for item in root.findall(f"{NS}si")
    ]


def _cell_number_formats(archive: zipfile.ZipFile, *, path: str | Path = "?") -> dict[int, str]:
    """Map a cell style index to `date` / `time` / `datetime`.

    Anything not listed is a plain number or text cell. A workbook without a
    readable `styles.xml` simply yields no conversions instead of failing.
    """
    try:
        root = ET.fromstring(_read_part(archive, "xl/styles.xml", path=path))
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
    *,
    path: str | Path = "?",
) -> dict[int, dict[int, str]]:
    """Stream one worksheet part into `{row index: {column index: value}}`.

    `ET.iterparse` with `elem.clear()` keeps memory proportional to a single row rather
    than the whole sheet, and it lets the SEC-02 geometry caps abort mid-parse instead of
    after the entire document tree has been built. The size budget was already enforced
    when the bytes were read (`_read_part`). A malformed part raises `ET.ParseError`, as
    before — this function does not convert it.
    """
    rows: dict[int, dict[int, str]] = {}
    seen_rows = 0
    row_tag = f"{NS}row"
    with archive.open(target) as member:
        # `events=("end",)` fires for every element; only the row closes matter. Handling
        # them as they finish keeps peak memory at one row and lets the geometry caps stop
        # a hostile sheet mid-stream. A malformed part raises ET.ParseError, which is left
        # to propagate just as the previous full-document parse did.
        for _, row in (
            event for event in ET.iterparse(member, events=("end",)) if event[1].tag == row_tag
        ):
            seen_rows += 1
            if seen_rows > MAX_SHEET_ROWS:
                raise XlsxError(f"{path} 的工作表过高：行数超过上限 {MAX_SHEET_ROWS}。")
            cells: dict[int, str] = {}
            for cell in row.findall(f"{NS}c"):
                column, row_index = _cell_position(cell.get("r"))
                if row_index is None or column is None:
                    continue
                # A sheet claiming an absurd coordinate would make `_as_grid` allocate a
                # huge rectangular grid; refuse it before building anything.
                if row_index > MAX_SHEET_ROWS:
                    raise XlsxError(
                        f"{path} 的工作表过高：第 {row_index} 行超过行数上限 {MAX_SHEET_ROWS}。"
                    )
                if column >= MAX_SHEET_COLUMNS:
                    raise XlsxError(
                        f"{path} 的工作表过宽：列 {cell.get('r')} 超过列数上限 {MAX_SHEET_COLUMNS}。"
                    )
                value = _cell_value(cell, shared_strings, number_formats)
                if value:
                    cells[column] = value
            if cells:
                rows.setdefault(row_index, {}).update(cells)
            row.clear()  # release this row's subtree before the next one accumulates
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
