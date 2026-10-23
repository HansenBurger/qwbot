"""跑批计划表格同步：xlsx 解析、字段映射、重复导入的行为。"""

from __future__ import annotations

import sqlite3
import time as time_module
import zipfile
from datetime import date, time
from pathlib import Path

import pytest

from qwbot import store, xlsx
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
# P1 / P3 / P4 / SEC-02: the performance work must not change observable behaviour
# ---------------------------------------------------------------------------


def test_import_writes_every_row_in_one_transaction(workbook: Path, db_path: Path) -> None:
    """P1: one connect + one commit for the whole sheet, not one per row."""
    store.init_store(db_path)
    rows, _ = load_plan_rows(workbook, "长周期")

    real_connect = sqlite3.connect
    calls: list[str] = []

    def counting_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        calls.append("connect")
        return real_connect(*args, **kwargs)  # type: ignore[arg-return]

    sqlite3.connect = counting_connect  # type: ignore[assignment]
    try:
        summary = import_plan_rows(db_path, rows, today=TODAY)
    finally:
        sqlite3.connect = real_connect  # type: ignore[assignment]

    assert len(summary.created) == 4
    # The read-only `load_status_file` pass costs one connection; the write phase adds
    # exactly one more. Before the fix this was 2N+1 (9 connections for these 4 rows).
    assert len(calls) == 2, calls


def test_failed_import_rolls_the_whole_batch_back(db_path: Path) -> None:
    """P1 side effect worth locking down: a mid-write error leaves nothing behind."""
    store.init_store(db_path)
    rows = [
        {
            "category": "长周期",
            "natural_date": f"2026-09-{day:02d}",
            "date": f"2026-09-{day:02d}",
            "batch_start_time": "15:00",
            "current_accounting_date": "",
            "next_accounting_date": "",
            "holiday_flag": "N",
            "description": "存量特殊日期",
            "content": "存量特殊日期",
        }
        for day in (10, 11)
    ]

    # The import writes through `store.write_items`, which resolves its row writer by
    # collection; wrap that writer so the second statement trips the `block_events` FK.
    real_writer = store._collection_writer

    def writer_that_fails(collection: str):
        insert_row = real_writer(collection)

        def wrapped(connection: sqlite3.Connection, item: dict[str, str]) -> int:
            row_id = insert_row(connection, item)
            if str(item.get("natural_date", "")).endswith("-11"):
                connection.execute(
                    "INSERT INTO block_events (batch_plan_id, reason) VALUES (?, ?)",
                    (999_999, "boom"),
                )
            return row_id

        return wrapped

    store._collection_writer = writer_that_fails  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.IntegrityError):
            import_plan_rows(db_path, rows, today=TODAY)
    finally:
        store._collection_writer = real_writer  # type: ignore[assignment]

    # One transaction means one all-or-nothing outcome — the first row is gone too.
    assert store.load_status_file(db_path)["batch_plan"] == []


def test_get_item_matches_the_full_load(db_path: Path) -> None:
    """P3: the single-row read returns exactly what the table-wide loader produced."""
    store.init_store(db_path)
    first = store.add_item(db_path, "batch_plan", {"content": "第一批", "natural_date": "2026-09-10"})
    second = store.add_item(db_path, "batch_plan", {"content": "第二批", "natural_date": "2026-09-11"})
    store.add_block_event(db_path, first, "等待上游数据", "2026-09-10T15:00:00")
    store.add_block_event(db_path, second, "已解决阻塞", "2026-09-11T10:00:00")
    store.close_open_block(db_path, second, "2026-09-11T11:00:00")

    loaded = {int(item["id"]): item for item in store.load_status_file(db_path)["batch_plan"]}
    for index in (first, second):
        assert store.get_item(db_path, "batch_plan", index) == loaded[index]

    progress = store.add_item(db_path, "progress", {"content": "用例评审", "owner": "张三"})
    assert store.get_item(db_path, "progress", progress) == {
        "id": str(progress),
        "content": "用例评审",
        "owner": "张三",
        "status": "",
        "category": "",
        "date": "",
    }

    task = store.add_item(
        db_path, "notification_tasks", {"title": "日报", "content": "正文", "doc_url": "https://a/x"}
    )
    store.add_scheduler_skip_date(db_path, "2026-09-12")
    fetched = store.get_item(db_path, "notification_tasks", task)
    assert fetched["doc_links"] and fetched["skip_dates"] == []
    with pytest.raises(IndexError):
        store.get_item(db_path, "batch_plan", 999999)


def test_schema_pass_runs_once_per_process(db_path: Path, tmp_path: Path) -> None:
    """P2: `_ensure_schema` is memoised per database path within the process."""
    store.reset_schema_cache()
    real_connect = sqlite3.connect
    counted: list[int] = []

    def counting_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)  # type: ignore[arg-return]
        counted.append(id(connection))
        return connection

    sqlite3.connect = counting_connect  # type: ignore[assignment]
    try:
        store._ensure_schema(db_path)
        after_first = len(counted)
        for _ in range(5):
            store._ensure_schema(db_path)
        assert len(counted) == after_first, "schema pass repeated for the same database"

        other = tmp_path / "second.sqlite3"
        store._ensure_schema(other)
        assert len(counted) > after_first, "a different database must still get its pass"
    finally:
        sqlite3.connect = real_connect  # type: ignore[assignment]
        store.reset_schema_cache()


def test_hot_query_indexes_exist(db_path: Path) -> None:
    """P4: the three indexes back the natural-date filter, the archive view and block events."""
    store.init_store(db_path)
    with sqlite3.connect(db_path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    assert {
        "idx_batch_plan_natural_date",
        "idx_batch_plan_archived",
        "idx_block_events_batch",
    } <= names

    # And the planner actually uses the block-events index instead of scanning.
    with sqlite3.connect(db_path) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM block_events WHERE batch_plan_id = ?", (1,)
        ).fetchall()
    assert any("idx_block_events_batch" in row[3] for row in plan), plan


def test_zip_bomb_is_refused_before_it_is_expanded(tmp_path: Path) -> None:
    """SEC-02: a payload past the budget fails from the directory alone — no expansion."""
    path = tmp_path / "bomb.xlsx"
    row = b'<row r="1"><c r="A1"><v>1</v></c></row>'
    sheet = (
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + row * 7_500_000  # ~280 MiB uncompressed, but it compresses to almost nothing
        + b"</worksheet>"
    )
    _write_minimal_workbook(path, sheet)
    assert path.stat().st_size < 2 * 1024 * 1024  # tiny file, enormous expansion

    started = time_module.perf_counter()
    with pytest.raises(XlsxError) as error:
        read_sheet(path, "S")
    # Rejected from the central directory, so it never streams or parses the payload.
    assert time_module.perf_counter() - started < 1.0
    assert "过大" in str(error.value) and "上限" in str(error.value)


def test_absurd_row_count_stops_mid_stream(tmp_path: Path) -> None:
    """SEC-02: more rows than allowed are refused while streaming, not after parsing."""
    path = tmp_path / "tall.xlsx"
    rows = [b'<row r="%d"><c r="A%d"><v>1</v></c></row>' % (i, i) for i in range(1, 250_001)]
    sheet = (
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        + b"".join(rows)
        + b"</sheetData></worksheet>"
    )
    _write_minimal_workbook(path, sheet)
    assert len(sheet) < xlsx.MAX_MEMBER_BYTES  # under the size budget: geometry does it

    with pytest.raises(XlsxError) as error:
        read_sheet(path, "S")
    assert "过高" in str(error.value)


def test_absurd_sheet_geometry_is_refused(tmp_path: Path) -> None:
    """SEC-02: row/column caps stop a sparse sheet from blowing up the grid build."""
    wide = tmp_path / "wide.xlsx"
    _write_minimal_workbook(
        wide,
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b"<sheetData><row r=\"1\"><c r=\"XFE1\"><v>1</v></c></row></sheetData></worksheet>",
    )
    with pytest.raises(XlsxError) as error:
        read_sheet(wide, "S")
    assert "过宽" in str(error.value)

    sparse = tmp_path / "sparse.xlsx"
    _write_minimal_workbook(
        sparse,
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<sheetData><row r="200001"><c r="A200001"><v>1</v></c></row></sheetData></worksheet>',
    )
    with pytest.raises(XlsxError) as error:
        read_sheet(sparse, "S")
    assert "第 200001 行超过行数上限" in str(error.value)


def test_normal_workbook_still_reads_after_the_limits(workbook: Path) -> None:
    """The budgets must not touch legitimate exports — same grid as before the guard."""
    grid = read_sheet(workbook, "长周期")
    assert grid[1][2] == "2026-09-10"
    assert list_sheets(workbook) == ["UAT2", "长周期"]


def _write_minimal_workbook(path: Path, sheet_xml: bytes) -> None:
    """Smallest workbook the reader accepts, with one caller-supplied sheet part."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            b' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            b'<sheets><sheet name="S" sheetId="1" r:id="rId2"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId2"'
            b' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"'
            b' Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


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
