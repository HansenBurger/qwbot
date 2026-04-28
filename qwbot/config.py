from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    webhook_url: str
    webhook_url_prod: str | None
    webhook_url_test: str | None
    webhook_target: str
    bot_title: str
    system_date_format: str
    timezone: str
    reminder_hour: int
    reminder_minute: int
    progress_source_url: str | None
    batch_plan_source_url: str | None
    progress_doc_url: str | None
    batch_register_doc_url: str | None
    case_assignment_doc_url: str | None
    agenda_doc_url: str | None
    frontend_url: str | None
    local_status_file: Path
    db_path: Path

    def with_webhook_target(self, target: str) -> "Settings":
        return replace(
            self,
            webhook_target=target,
            webhook_url=_select_webhook_url(
                target=target,
                prod_url=self.webhook_url_prod,
                test_url=self.webhook_url_test,
                legacy_url=self.webhook_url,
            ),
        )


def load_settings(env_file: str | Path = ".env") -> Settings:
    load_dotenv(env_file)

    webhook_url_prod = _optional("WECOM_WEBHOOK_URL_PROD") or _optional("WECOM_WEBHOOK_URL")
    webhook_url_test = _optional("WECOM_WEBHOOK_URL_TEST")
    webhook_target = os.getenv("WECOM_WEBHOOK_TARGET", "prod").strip().lower()
    webhook_url = _select_webhook_url(
        target=webhook_target,
        prod_url=webhook_url_prod,
        test_url=webhook_url_test,
        legacy_url=webhook_url_prod,
    )
    return Settings(
        webhook_url=webhook_url,
        webhook_url_prod=webhook_url_prod,
        webhook_url_test=webhook_url_test,
        webhook_target=webhook_target,
        bot_title=os.getenv("BOT_TITLE", "贷款核算测试日报提醒"),
        system_date_format=os.getenv("SYSTEM_DATE_FORMAT", "%Y-%m-%d"),
        timezone=os.getenv("TIMEZONE", "Asia/Shanghai"),
        reminder_hour=_int_env("REMINDER_HOUR", 9),
        reminder_minute=_int_env("REMINDER_MINUTE", 0),
        progress_source_url=_optional("PROGRESS_SOURCE_URL"),
        batch_plan_source_url=_optional("BATCH_PLAN_SOURCE_URL"),
        progress_doc_url=_optional("PROGRESS_DOC_URL"),
        batch_register_doc_url=_optional("BATCH_REGISTER_DOC_URL"),
        case_assignment_doc_url=(
            _optional("CASE_ASSIGNMENT_DOC_URL") or _optional("ENVIRONMENT_STATS_DOC_URL")
        ),
        agenda_doc_url=_optional("AGENDA_DOC_URL"),
        frontend_url=_optional("FRONTEND_URL"),
        local_status_file=Path(os.getenv("LOCAL_STATUS_FILE", "data/sample_status.json")),
        db_path=Path(os.getenv("QWBOT_DB_PATH", "data/qwbot.sqlite3")),
    )


def _optional(name: str) -> str | None:
    value = os.getenv(name)
    return value if value else None


def _select_webhook_url(
    *,
    target: str,
    prod_url: str | None,
    test_url: str | None,
    legacy_url: str | None,
) -> str:
    target = target.strip().lower()
    if target not in {"prod", "test"}:
        raise RuntimeError("Environment variable WECOM_WEBHOOK_TARGET must be prod or test")

    selected = test_url if target == "test" else prod_url
    if selected:
        return selected
    if legacy_url:
        return legacy_url
    missing_name = "WECOM_WEBHOOK_URL_TEST" if target == "test" else "WECOM_WEBHOOK_URL_PROD"
    raise RuntimeError(f"Missing required environment variable: {missing_name}")


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc
