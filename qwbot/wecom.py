from __future__ import annotations

import requests


class WeComWebhookClient:
    def __init__(self, webhook_url: str, timeout_seconds: int = 10) -> None:
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds

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
        response = requests.post(
            self.webhook_url,
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

        result = response.json()
        if result.get("errcode") != 0:
            raise RuntimeError(f"WeCom webhook failed: {result}")
