from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from qwbot.config import Settings
from qwbot.planner import batch_title
from qwbot.sources import ReminderData

SETTINGS_URL_VARS = (
    ("progress_doc_link", "progress_doc_url"),
    ("case_assignment_doc_link", "case_assignment_doc_url"),
    ("batch_register_doc_link", "batch_register_doc_url"),
    ("agenda_doc_link", "agenda_doc_url"),
    ("frontend_link", "frontend_url"),
)


def settings_url_map(settings: Settings | None) -> dict[str, str]:
    """`.env` fallback values for the well-known document link variables."""
    if settings is None:
        return {}
    return {var_name: (getattr(settings, attr) or "") for var_name, attr in SETTINGS_URL_VARS}


def build_template_variables(
    settings: Settings,
    next_batch: dict[str, str] | None,
    template_vars: list[dict[str, str]] | None = None,
) -> dict[str, str]:
    """System variables merged with DB-managed document link variables."""
    from qwbot.store import DEFAULT_TEMPLATE_VARS

    now = datetime.now(ZoneInfo(settings.timezone))
    variables = {
        "title_date": now.strftime("%m月%d日"),
        "current_trading_date": _batch_current_date(next_batch),
        "next_trading_date": _batch_next_date(next_batch),
    }
    fallback_urls = settings_url_map(settings)
    if template_vars:
        for var in template_vars:
            var_name = var.get("var_name", "")
            if not var_name:
                continue
            var_value = (var.get("var_value") or "").strip()
            variables[var_name] = var_value or fallback_urls.get(var_name, "")
    else:
        for var_name in DEFAULT_TEMPLATE_VARS:
            variables[var_name] = fallback_urls.get(var_name, "")
    return variables


def render_message_template(
    settings: Settings,
    template: dict[str, object] | str | None,
    template_vars: list[dict[str, str]] | None = None,
    next_batch: dict[str, str] | None = None,
) -> str:
    """Render one webhook message template.

    Accepts either a template dict from `message_templates` (with `content` and
    `at_all`) or a raw template string. `{var_name}` placeholders are replaced with
    system variables and DB-managed document links; `<@all>` is appended when the
    template asks for it and the body does not already contain it.
    """
    from qwbot.store import DEFAULT_REMINDER_TEMPLATE

    if isinstance(template, dict):
        content = str(template.get("content") or "")
        at_all = _is_truthy(template.get("at_all"))
    else:
        content = str(template or "")
        at_all = False

    if not content.strip():
        content = DEFAULT_REMINDER_TEMPLATE

    variables = build_template_variables(settings, next_batch, template_vars)
    rendered = content
    for key, value in variables.items():
        rendered = rendered.replace("{" + key + "}", value)
    if at_all and "<@all>" not in rendered:
        rendered = f"{rendered}\n\n<@all>"
    return rendered


def _is_truthy(value: object) -> bool:
    """Accept both the DB string flag ("true") and the decoded boolean."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes", "y", "on"}


def build_scheduled_reminder(
    settings: Settings,
    next_batch: dict[str, str] | None,
    template: str | None = None,
    template_vars: list[dict[str, str]] | None = None,
) -> str:
    """Compatibility entry point kept for the CLI and existing callers."""
    return render_message_template(settings, template, template_vars, next_batch)


def build_daily_reminder(settings: Settings, data: ReminderData) -> str:
    # 保留旧入口，供 CLI preview/send-now 兼容使用。
    now = datetime.now(ZoneInfo(settings.timezone))
    system_date = now.strftime(settings.system_date_format)
    return "\n".join(
        [
            f"## {settings.bot_title}",
            f"> 当前系统日期：<font color=\"info\">{system_date}</font>",
            "",
            "### 今日跑批计划",
            _format_list(data.batch_plan, "暂无跑批计划，请在在线文档中补充。"),
            "",
            "请相关同事按计划执行，并在执行进度在线文档中更新当天进度。",
            _format_doc_line("跑批登记簿", settings.batch_register_doc_url),
            _format_doc_line("执行进度在线文档", settings.progress_doc_url),
            "",
            "<@all>",
        ]
    )


def build_batch_start_notice(item: dict[str, str]) -> str:
    return "\n".join(
        [
            "【批量执行通知】",
            f"当前批量：{batch_title(item)}",
            "批量开始执行，在环境中不要测试。",
        ]
    )


def build_batch_complete_notice(
    completed_item: dict[str, str],
    next_item: dict[str, str] | None,
) -> str:
    lines = [
        "【批量完成通知】",
        f"已完成批量：{_batch_description(completed_item)}",
        f"待执行批量：{batch_title(next_item)}",
    ]
    requester_mentions = _format_requester_mentions(completed_item)
    if requester_mentions:
        lines.append(requester_mentions)
    return "\n".join(lines)


def build_custom_notification(
    task: dict[str, str],
    template_vars: list[dict[str, str]] | None = None,
    settings: Settings | None = None,
) -> str:
    title = task.get("title") or "消息通知"
    lines = [
        f"【{title}】",
        "",
        task.get("content") or "",
    ]
    doc_links = task.get("doc_links") or []
    if doc_links:
        lines.extend(["", "相关文档："])
        for link in doc_links:
            label = link.get("label") or "在线文档"
            url = link.get("url") or ""
            url = _resolve_doc_url(url, template_vars, settings)
            lines.append(f"- [{label}]({url})")
    elif task.get("doc_url"):
        url = _resolve_doc_url(task["doc_url"], template_vars, settings)
        lines.extend(["", "相关文档：", f"- [在线文档]({url})"])
    if task.get("at_all") == "true":
        lines.extend(["", "<@all>"])
    return "\n".join(line for line in lines if line is not None)


def _resolve_doc_url(
    url: str,
    template_vars: list[dict[str, str]] | None,
    settings: Settings | None,
) -> str:
    """Resolve {var_name} references in a doc URL."""
    if not url or not url.startswith("{") or not template_vars:
        return url

    fallback_urls = settings_url_map(settings)
    var_map = {}
    for var in template_vars:
        var_name = var.get("var_name", "")
        var_value = (var.get("var_value") or "").strip()
        if not var_value:
            var_value = fallback_urls.get(var_name, "")
        var_map[var_name] = var_value

    def replace_var(match):
        var_name = match.group(1)
        return var_map.get(var_name, match.group(0))

    return re.sub(r"\{([^}]+)\}", replace_var, url)


def _format_list(items: list[str], empty_text: str) -> str:
    if not items:
        return f"- {empty_text}"
    return "\n".join(f"- {item}" for item in items)


def _format_doc_line(label: str, url: str | None) -> str:
    if not url:
        return f"{label}：未配置"
    return f"{label}：[点击打开]({url})"


def _format_doc_link(url: str | None) -> str:
    if not url:
        return "未配置"
    return f"[点击]({url})"


def _batch_description(item: dict[str, str] | None) -> str:
    if not item:
        return "无"
    return item.get("description") or item.get("content") or "未填写说明"


def _batch_current_date(item: dict[str, str] | None) -> str:
    if not item:
        return "无"
    return item.get("current_accounting_date") or "-"


def _batch_next_date(item: dict[str, str] | None) -> str:
    if not item:
        return "无"
    return item.get("next_accounting_date") or "-"


def _format_requester_mentions(item: dict[str, str] | None) -> str:
    if not item:
        return ""

    requester = item.get("requester") or item.get("owner") or ""
    names = [
        name.strip()
        for part in requester.replace("，", "、").replace(",", "、").split("、")
        for name in part.split()
        if name.strip()
    ]
    return " ".join(f"@{name}" for name in names)
