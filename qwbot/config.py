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
    # SEC-01: credentials guarding every mutating route and the notification panel.
    web_username: str = "admin"
    web_password: str | None = None
    secret_key: str | None = None
    # SEC-03: hosts a webhook URL may point at, beyond the built-in official one.
    allowed_webhook_hosts: tuple[str, ...] = ()
    allow_insecure_local_webhooks: bool = False
    # SEC-01: mark the session cookie Secure when the panel is reached over TLS (or an
    # operator says so explicitly). Plain-HTTP local development must stay usable.
    secure_cookies: bool = False

    def with_webhook_target(self, target: str) -> "Settings":
        return replace(
            self,
            webhook_target=target,
            webhook_url=_select_webhook_url(
                target=target,
                prod_url=self.webhook_url_prod,
                test_url=self.webhook_url_test,
                legacy_url=self.webhook_url,
                required=False,
            ),
        )

    def env_webhook_url(self, target: str) -> str:
        """Resolve a webhook URL from environment variables only.

        Used as the fallback when a database-managed webhook has an empty URL.
        Only the `prod` and `test` targets exist in `.env`; anything else (notably
        `custom`) has no environment counterpart and resolves to an empty string so
        the caller skips sending instead of leaking into the wrong group.
        """
        wanted = (target or "").strip().lower()
        if wanted not in {"prod", "test"}:
            return ""
        return _select_webhook_url(
            target=wanted,
            prod_url=self.webhook_url_prod,
            test_url=self.webhook_url_test,
            legacy_url=self.webhook_url_prod or self.webhook_url,
            required=False,
        )

    def webhook_seeds(self) -> list[dict[str, str]]:
        """Webhook records seeded into the database on first run.

        Environment variables are only a bootstrap source: after the first seed the
        database is authoritative and later `.env` edits are ignored.
        """
        seeds: list[dict[str, str]] = []
        if self.webhook_url_prod:
            seeds.append(
                {
                    "name": "生产群机器人",
                    "url": self.webhook_url_prod,
                    "target": "prod",
                    "remark": "由 .env WECOM_WEBHOOK_URL_PROD 初始化",
                }
            )
        if self.webhook_url_test:
            seeds.append(
                {
                    "name": "自测群机器人",
                    "url": self.webhook_url_test,
                    "target": "test",
                    "remark": "由 .env WECOM_WEBHOOK_URL_TEST 初始化",
                }
            )
        return seeds

    def default_seed_target(self) -> str:
        return self.webhook_target if self.webhook_target in {"prod", "test"} else "prod"


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
        required=False,
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
        web_username=os.getenv("QWBOT_WEB_USERNAME", "admin").strip() or "admin",
        web_password=_optional("QWBOT_WEB_PASSWORD"),
        secret_key=_optional("QWBOT_SECRET_KEY"),
        allowed_webhook_hosts=_csv_env("QWBOT_ALLOWED_WEBHOOK_HOSTS"),
        allow_insecure_local_webhooks=_bool_env("QWBOT_ALLOW_INSECURE_LOCAL_WEBHOOKS"),
        secure_cookies=_bool_env("QWBOT_SECURE_COOKIES"),
    )


def _csv_env(name: str) -> tuple[str, ...]:
    """Parse a comma separated host list into lowercase names without duplicates."""
    raw = os.getenv(name) or ""
    hosts: list[str] = []
    for item in raw.split(","):
        host = item.strip().lower().rstrip(".")
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def _bool_env(name: str) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _optional(name: str) -> str | None:
    value = os.getenv(name)
    return value if value else None


def _select_webhook_url(
    *,
    target: str,
    prod_url: str | None,
    test_url: str | None,
    legacy_url: str | None,
    required: bool = True,
) -> str:
    target = target.strip().lower()
    if target not in {"prod", "test"}:
        if required:
            raise RuntimeError("Environment variable WECOM_WEBHOOK_TARGET must be prod or test")
        target = "prod"

    selected = test_url if target == "test" else prod_url
    if selected:
        return selected
    if legacy_url:
        return legacy_url
    if not required:
        # Webhooks are managed in the database now; an empty env value is not fatal.
        return ""
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
