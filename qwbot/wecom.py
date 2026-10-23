from __future__ import annotations

from urllib.parse import urlparse

import requests

from qwbot.security import normalize_host, validate_webhook_url


class WebhookTargetRefused(RuntimeError):
    """SEC-03: the outbound target failed the whitelist, so nothing was sent.

    The message is built from `validate_webhook_url`'s own category text plus a masked
    URL — it never carries the webhook key, so callers may print or log it directly.
    """


def guard_webhook_target(
    url: str,
    *,
    extra_hosts: tuple[str, ...] | list[str] | set[str] | None = None,
    allow_local: bool = False,
) -> str:
    """H-1: enforce SEC-03 at the send boundary instead of only in the web form.

    Every row that reaches this point already lives in the database (seeded from `.env`,
    restored from a backup, or written by an older build), and the CLI/scheduler send
    straight from those rows without ever going through the web routes. Validating here is
    what makes the whitelist a property of *what we actually request* rather than only of
    what the panel lets an operator type.
    """
    valid, message = validate_webhook_url(url, extra_hosts=extra_hosts, allow_local=allow_local)
    if not valid:
        raise WebhookTargetRefused(f"{message}（地址：{host_for_error(url)}）")
    return url


def host_for_error(url: str) -> str:
    """Report only the host of a refused target.

    Deliberately not `mask_webhook_url`: that helper clips the middle of a string, which on
    a path-bearing URL produces mangled text like `169.254.1****-data/` and misleads the
    operator who is reading it to fix a configuration. The host is never secret, and the key
    stays out because we simply do not include the query string.
    """
    try:
        parsed = urlparse(str(url or ""))
        host = normalize_host(parsed.hostname) or "(无法解析主机名)"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{host}{port}"
    except Exception:  # noqa: BLE001 - an error path must never raise a second error
        return "****"


class WeComWebhookClient:
    def __init__(
        self,
        webhook_url: str,
        timeout_seconds: int = 10,
        *,
        allowed_hosts: tuple[str, ...] | list[str] | set[str] | None = None,
        allow_local: bool = False,
        enforce_allowlist: bool = True,
    ) -> None:
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds
        self.allowed_hosts = allowed_hosts
        self.allow_local = allow_local
        self.enforce_allowlist = enforce_allowlist

    def send_markdown(self, content: str) -> None:
        self._send({"msgtype": "markdown", "markdown": {"content": content}})

    def send_text(
        self,
        content: str,
        mentioned_list: list[str] | None = None,
        mentioned_mobile_list: list[str] | None = None,
    ) -> None:
        payload = {"msgtype": "text", "text": {"content": content}}
        if mentioned_list:
            payload["text"]["mentioned_list"] = mentioned_list
        if mentioned_mobile_list:
            payload["text"]["mentioned_mobile_list"] = mentioned_mobile_list
        self._send(payload)

    def _send(self, payload: dict) -> None:
        if self.enforce_allowlist:
            guard_webhook_target(
                self.webhook_url,
                extra_hosts=self.allowed_hosts,
                allow_local=self.allow_local,
            )
        response = requests.post(
            self.webhook_url,
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

        result = response.json()
        if result.get("errcode") != 0:
            raise RuntimeError(f"WeCom webhook failed: {result}")
