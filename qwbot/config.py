from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    webhook_url: str
    bot_title: str
    system_date_format: str
    timezone: str
    reminder_hour: int
    reminder_minute: int
    progress_source_url: str | None
    batch_plan_source_url: str | None
    progress_doc_url: str | None
    batch_register_doc_url: str | None
    environment_stats_doc_url: str | None
    agenda_doc_url: str | None
    frontend_url: str | None
    local_status_file: Path


def load_settings(env_file: str | Path = ".env") -> Settings:
    load_dotenv(env_file)

    webhook_url = _required("WECOM_WEBHOOK_URL")
    return Settings(
        webhook_url=webhook_url,
        bot_title=os.getenv("BOT_TITLE", "贷款核算测试日报提醒"),
        system_date_format=os.getenv("SYSTEM_DATE_FORMAT", "%Y-%m-%d"),
        timezone=os.getenv("TIMEZONE", "Asia/Shanghai"),
        reminder_hour=_int_env("REMINDER_HOUR", 9),
        reminder_minute=_int_env("REMINDER_MINUTE", 0),
        progress_source_url=_optional("PROGRESS_SOURCE_URL"),
        batch_plan_source_url=_optional("BATCH_PLAN_SOURCE_URL"),
        progress_doc_url=_optional("PROGRESS_DOC_URL"),
        batch_register_doc_url=_optional("BATCH_REGISTER_DOC_URL"),
        environment_stats_doc_url=_optional("ENVIRONMENT_STATS_DOC_URL"),
        agenda_doc_url=_optional("AGENDA_DOC_URL"),
        frontend_url=_optional("FRONTEND_URL"),
        local_status_file=Path(os.getenv("LOCAL_STATUS_FILE", "data/sample_status.json")),
    )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str) -> str | None:
    value = os.getenv(name)
    return value if value else None


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc
