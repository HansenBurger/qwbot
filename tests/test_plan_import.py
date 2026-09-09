"""跑批计划表格同步：xlsx 解析、字段映射、重复导入的行为。"""

from __future__ import annotations

import zipfile
from datetime import date, time
from pathlib import Path

import pytest

from qwbot import store
from qwbot.plan_import import import_plan_file, import_plan_rows, load_plan_rows
from qwbot.xlsx import XlsxError, list_sheets, read_sheet

TODAY = date(2026, 9, 10)

HEADER = [
    "序号",
    "星期",
    "自然日历",
    "跑批启动时间",
    "系统当前\n会计日期",
    "跑批后\n会计日期",
    "节假日标志\nY-是/N-否",
    "利率变更标识\nY-是/N-否",
    "数据备份",
    "跑批说明",
    "计结息专项",
    "流程作业组\n（零售）",
]

ROWS: list[list[object]] = [
    # 今天只有一批
    [1, "星期四", date(2026, 9, 10), time(15, 0), date(2026, 9, 21), date(2026, 9, 22),
     "N", "N", "", "存量特殊日期", "", ""],
    # 明天三批，其中一批的批后会计日期是节假日，还带利率变更和小组备注
    [2, "星期五", date(2026, 9, 11), time(10, 30), date(2026, 9, 22), date(2026, 9, 24),
     "N", "", "", "存量特殊日期", "", ""],
    [3, "星期五", date(2026, 9, 11), time(15, 30), date(2026, 9, 24), date(2026, 9, 25),
     "Y", "Y", "", "20/21 固定停点", "对公012：加批验证宽限期", "批前数据准备：放款日2026/9/1"],
    [4, "星期五", date(2026, 9, 11), time(19, 0), date(2026, 9, 25), date(2026, 9, 30),
     "N", "", "", "月末", "", ""],
    # 历史批次
    [5, "星期三", date(2026, 9, 9), time(19, 0), date(2026, 9, 20), date(2026, 9, 21),
     "N", "", "", "20/21 固定停点", "", ""],
]


@pytest.fixture()
def workbook(tmp_path: Path) -> Path:
    path = tmp_path / "跑批计划.xlsx"
    _write_xlsx(path, [("UAT2", [HEADER, ROWS[0]]), ("长周期", [HEADER, *ROWS])])
    return path


def test_reader_converts_dates_times_and_wrapped_headers(workbook: Path) -> None:
    assert list_sheets(workbook) == ["UAT2", "长周期"]
    grid = read_sheet(workbook, "长周期")
    assert grid[0][4] == "系统当前 会计日期"  # the line break becomes a plain space
    assert grid[1][2] == "2026-09-10"
    assert grid[1][3] == "15:00"
    assert grid[2][1] == "星期五"


def test_missing_sheet_lists_the_available_ones(workbook: Path) -> None:
    with pytest.raises(XlsxError) as error:
        read_sheet(workbook, "小环境")
    assert "UAT2" in str(error.value) and "长周期" in str(error.value)


def test_rows_are_mapped_onto_batch_plan_fields(workbook: Path) -> None:
    rows, ignored = load_plan_rows(workbook, "长周期")
    assert ignored == []
    first = rows[0]
    assert first["natural_date"] == "2026-09-10"
    assert first["date"] == "2026-09-10"
    assert first["batch_start_time"] == "15:00"
    assert first["current_accounting_date"] == "2026-09-21"
    assert first["next_accounting_date"] == "2026-09-22"
    assert first["category"] == "长周期"
    assert first["execution_status"] == "待执行"


def test_team_notes_are_folded_into_the_description(workbook: Path) -> None:
    rows, _ = load_plan_rows(workbook, "长周期")
    described = next(row for row in rows if row["batch_start_time"] == "15:30")
    assert described["content"] == "20/21 固定停点"
    assert described["description"].split("\n") == [
        "20/21 固定停点",
        "批后会计日期为节假日",
        "利率变更",
        "计结息专项：对公012：加批验证宽限期",
        "流程作业组 （零售）：批前数据准备：放款日2026/9/1",
    ]


def test_source_holiday_flag_stays_out_of_the_reminder_calendar(workbook: Path) -> None:
    """源表的节假日标志说的是批后会计日期，写进 holiday_flag 会让工作日被当成节假日。"""
    rows, _ = load_plan_rows(workbook, "长周期")
    assert all(row["holiday_flag"] == "N" for row in rows)

    raw, _ = load_plan_rows(workbook, "长周期", keep_source_holiday_flag=True)
    flagged = [row["batch_start_time"] for row in raw if row["holiday_flag"] == "Y"]
    assert flagged == ["15:30"]
    assert all("批后会计日期为节假日" not in row["description"] for row in raw)


def test_import_skips_past_batches_by_default(workbook: Path, db_path: Path) -> None:
    store.init_store(db_path)
    summary = import_plan_file(db_path, workbook, today=TODAY)

    assert [item["natural_date"] for item in store.load_status_file(db_path)["batch_plan"]] == [
        "2026-09-10",
        "2026-09-11",
        "2026-09-11",
        "2026-09-11",
    ]
    assert len(summary.created) == 4
    assert summary.skipped_past == ["2026-09-09 19:00"]
    assert "跳过今天之前的批次 1 条" in summary.as_text()


def test_import_can_archive_past_batches(workbook: Path, db_path: Path) -> None:
    store.init_store(db_path)
    summary = import_plan_file(db_path, workbook, past="archive", today=TODAY)

    assert len(summary.created) == 5  # 4 条今天的 + 1 条历史
    assert len(summary.archived_past) == 1
    assert summary.skipped_past == []
    archived = next(
        item
        for item in store.load_status_file(db_path)["batch_plan"]
        if item["natural_date"] == "2026-09-09"
    )
    assert archived["execution_status"] == "已完成"
    assert archived["archived"] == "true"
    assert archived["completed_on"] == "2026-09-09"
    assert archived["archived_on"] == TODAY.isoformat()


def test_reimport_is_idempotent_and_keeps_execution_state(workbook: Path, db_path: Path) -> None:
    store.init_store(db_path)
    import_plan_file(db_path, workbook, today=TODAY)
    items = store.load_status_file(db_path)["batch_plan"]
    index = int(next(item for item in items if item["batch_start_time"] == "15:00")["id"])

    item = store.get_item(db_path, "batch_plan", index)
    item["execution_status"] = "进行中"
    item["started_at"] = "2026-09-10T15:00:00"
    item["description"] = "页面上改过的说明"
    store.update_item(db_path, "batch_plan", index, item)

    summary = import_plan_file(db_path, workbook, today=TODAY)

    assert summary.created == []
    assert len(summary.unchanged) == 3
    # 说明属于计划字段，重新导入会以表格为准；执行态不受影响。
    assert summary.updated == ["2026-09-10 15:00"]
    refreshed = store.get_item(db_path, "batch_plan", index)
    assert refreshed["description"] == "存量特殊日期"
    assert refreshed["execution_status"] == "进行中"
    assert refreshed["started_at"] == "2026-09-10T15:00:00"
    assert len(store.load_status_file(db_path)["batch_plan"]) == 4


def test_dry_run_does_not_touch_the_database(workbook: Path, db_path: Path) -> None:
    store.init_store(db_path)
    summary = import_plan_file(db_path, workbook, today=TODAY, dry_run=True)

    assert len(summary.created) == 4
    assert "预演" in summary.as_text()
    assert store.load_status_file(db_path)["batch_plan"] == []


def test_unreadable_rows_are_reported(db_path: Path) -> None:
    store.init_store(db_path)
    rows = [
        {
            "category": "长周期",
            "natural_date": "2026-09-10",
            "date": "2026-09-10",
            "batch_start_time": "15:00",
            "current_accounting_date": "2026-09-21",
            "next_accounting_date": "2026-09-22",
            "holiday_flag": "N",
            "description": "存量特殊日期",
            "content": "存量特殊日期",
        }
    ]
    summary = import_plan_rows(db_path, rows, ["第9行"], today=TODAY)
    assert summary.skipped_rows == ["第9行"]
    assert "忽略无法识别的行 1 条" in summary.as_text()


def test_unknown_past_mode_is_rejected(workbook: Path, db_path: Path) -> None:
    store.init_store(db_path)
    with pytest.raises(ValueError):
        import_plan_file(db_path, workbook, past="delete", today=TODAY)


# ---------------------------------------------------------------------------
# Minimal .xlsx writer: the tests must not depend on the exported workbook in tmp/
# ---------------------------------------------------------------------------

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
{sheets}
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2">
<numFmt numFmtId="300" formatCode="yyyy\\-mm\\-dd"/>
<numFmt numFmtId="301" formatCode="hh:mm"/>
</numFmts>
<fonts count="1"><font/></fonts>
<fills count="1"><fill/></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="3">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="300" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="301" fontId="0" fillId="0" borderId="0" xfId="0"/>
</cellXfs>
</styleSheet>"""


def _write_xlsx(path: Path, sheets: list[tuple[str, list[list[object]]]]) -> None:
    """Write a workbook whose cells look like the exported plan (serials + shared strings)."""
    strings: list[str] = []

    def string_index(value: str) -> int:
        if value not in strings:
            strings.append(value)
        return strings.index(value)

    sheet_parts = [
        (f"worksheets/sheet{offset}.xml", _sheet_xml(rows, string_index))
        for offset, (_, rows) in enumerate(sheets, start=1)
    ]

    workbook_sheets = "".join(
        f'<sheet name="{_escape(name)}" sheetId="{offset}" r:id="rId{offset + 1}"/>'
        for offset, (name, _) in enumerate(sheets, start=1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{workbook_sheets}</sheets></workbook>"
    )
    workbook_rels = "".join(
        f'<Relationship Id="rId{offset + 1}"'
        ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"'
        f' Target="{target}"/>'
        for offset, (target, _) in enumerate(sheet_parts, start=1)
    )
    next_id = len(sheet_parts) + 2
    workbook_rels += (
        f'<Relationship Id="rId{next_id}"'
        ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"'
        ' Target="styles.xml"/>'
        f'<Relationship Id="rId{next_id + 1}"'
        ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"'
        ' Target="sharedStrings.xml"/>'
    )
    shared_strings = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        f' count="{len(strings)}">'
        + "".join(
            f'<si><t xml:space="preserve">{_escape(value)}</t></si>' for value in strings
        )
        + "</sst>"
    )
    overrides = "".join(
        f'<Override PartName="/xl/{target}"'
        ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for target, _ in sheet_parts
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES.format(sheets=overrides))
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{workbook_rels}</Relationships>",
        )
        archive.writestr("xl/styles.xml", _STYLES)
        archive.writestr("xl/sharedStrings.xml", shared_strings)
        for target, payload in sheet_parts:
            archive.writestr(f"xl/{target}", payload)


def _sheet_xml(rows: list[list[object]], string_index) -> str:
    body: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column, value in enumerate(row):
            reference = f"{_column_letter(column)}{row_number}"
            if isinstance(value, date):
                serial = (value - date(1899, 12, 30)).days
                cells.append(f'<c r="{reference}" s="1"><v>{serial}</v></c>')
            elif isinstance(value, time):
                fraction = (value.hour * 3600 + value.minute * 60) / 86400
                cells.append(f'<c r="{reference}" s="2"><v>{fraction}</v></c>')
            elif isinstance(value, (int, float)):
                cells.append(f'<c r="{reference}"><v>{value}</v></c>')
            else:
                text = "" if value is None else str(value)
                cells.append(f'<c r="{reference}" t="s"><v>{string_index(text)}</v></c>')
        body.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(body)}</sheetData></worksheet>"
    )


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
