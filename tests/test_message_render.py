"""Template rendering: variables, `.env` fallback, @all handling."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from qwbot import store
from qwbot.message import build_scheduled_reminder, render_message_template
from tests.conftest import init_db, make_settings

NEXT_BATCH = {
    "description": "跑批计划一",
    "current_accounting_date": "2026-09-08",
    "next_accounting_date": "2026-09-09",
}


def test_system_variables_are_substituted(db_path: Path) -> None:
    settings = make_settings(db_path)
    template = {
        "content": "{title_date} | {current_trading_date} | {next_trading_date}",
        "at_all": "",
    }

    rendered = render_message_template(settings, template, [], NEXT_BATCH)
    expected_date = datetime.now(ZoneInfo(settings.timezone)).strftime("%m月%d日")
    assert rendered == f"{expected_date} | 2026-09-08 | 2026-09-09"


def test_missing_batch_renders_placeholder(db_path: Path) -> None:
    settings = make_settings(db_path)
    template = {"content": "{current_trading_date}/{next_trading_date}", "at_all": ""}

    assert render_message_template(settings, template, [], None) == "无/无"


def test_db_variable_wins_over_env_fallback(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    template_vars = store.get_template_vars(db_path)
    progress_var = next(item for item in template_vars if item["var_name"] == "progress_doc_link")
    store.update_template_var(
        db_path, int(progress_var["id"]), "progress_doc_link", "进度统计文档链接", "https://db.example/p"
    )

    template = {"content": "[点击]({progress_doc_link})", "at_all": ""}
    rendered = render_message_template(
        settings, template, store.get_template_vars(db_path), None
    )
    assert rendered == "[点击](https://db.example/p)"


def test_empty_variable_falls_back_to_env_url(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)

    template = {"content": "[点击]({agenda_doc_link}) · {frontend_link}", "at_all": ""}
    rendered = render_message_template(
        settings, template, store.get_template_vars(db_path), None
    )
    assert rendered == "[点击](https://doc.example/agenda) · http://127.0.0.1:5000"


def test_at_all_appends_mention_once(db_path: Path) -> None:
    settings = make_settings(db_path)

    assert render_message_template(settings, {"content": "正文", "at_all": "true"}, [], None) == (
        "正文\n\n<@all>"
    )
    assert render_message_template(
        settings, {"content": "正文\n\n<@all>", "at_all": "true"}, [], None
    ) == ("正文\n\n<@all>")
    assert render_message_template(settings, {"content": "正文", "at_all": ""}, [], None) == "正文"


def test_empty_content_falls_back_to_default_template(db_path: Path) -> None:
    settings = make_settings(db_path)

    rendered = render_message_template(settings, {"content": "   ", "at_all": ""}, [], None)
    assert rendered.startswith("## ")
    assert "组内重点工作" in rendered

    assert render_message_template(settings, None, [], None) == rendered
    assert render_message_template(settings, "", [], None) == rendered


def test_unknown_placeholder_is_left_verbatim(db_path: Path) -> None:
    settings = make_settings(db_path)

    rendered = render_message_template(
        settings, {"content": "{not_a_variable}", "at_all": ""}, [], None
    )
    assert rendered == "{not_a_variable}"


def test_plain_string_template_is_still_supported(db_path: Path) -> None:
    settings = make_settings(db_path)

    assert render_message_template(settings, "静态内容", [], None) == "静态内容"


def test_legacy_entry_point_matches_new_renderer(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)
    template_vars = store.get_template_vars(db_path)

    assert build_scheduled_reminder(
        settings, NEXT_BATCH, store.DEFAULT_REMINDER_TEMPLATE, template_vars
    ) == render_message_template(
        settings,
        {"content": store.DEFAULT_REMINDER_TEMPLATE, "at_all": ""},
        template_vars,
        NEXT_BATCH,
    )


def test_default_template_renders_all_document_links(db_path: Path) -> None:
    settings = make_settings(db_path)
    init_db(db_path, settings)

    rendered = render_message_template(
        settings,
        {"content": store.DEFAULT_REMINDER_TEMPLATE, "at_all": ""},
        store.get_template_vars(db_path),
        NEXT_BATCH,
    )
    assert "{title_date}" not in rendered
    assert "{progress_doc_link}" not in rendered
    for url in (
        "https://doc.example/progress",
        "https://doc.example/cases",
        "https://doc.example/batch",
        "https://doc.example/agenda",
        "http://127.0.0.1:5000",
    ):
        assert url in rendered
    assert "2026-09-08" in rendered and "2026-09-09" in rendered
