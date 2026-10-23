#!/usr/bin/env python3
"""Mock WeCom group-robot webhook used for local self-testing.

Endpoints
---------
POST /cgi-bin/webhook/send?key=<key>
    Behaves like the real WeCom robot API and records every payload.
    Special keys for failure-path testing:
      - key starting with ``FAIL``      -> ``{"errcode": 93000, "errmsg": "invalid webhook url"}``
      - key starting with ``HTTP500``   -> HTTP 500
GET  /__messages
    JSON list of everything received so far (oldest first).
GET  /__messages/<key>
    JSON list filtered by webhook key prefix.
POST /__reset
    Drop all recorded messages.
GET  /__health
    Liveness probe.

Usage
-----
    python3 scripts/mock-webhook.py [--host 0.0.0.0] [--port 8089]
"""

from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_LOCK = threading.Lock()
MESSAGES: list[dict[str, object]] = []


def _record(entry: dict[str, object]) -> None:
    with _LOCK:
        MESSAGES.append(entry)


def _snapshot(prefix: str | None = None) -> list[dict[str, object]]:
    with _LOCK:
        items = list(MESSAGES)
    if prefix is None:
        return items
    return [item for item in items if str(item.get("key", "")).startswith(prefix)]


class MockWebhookHandler(BaseHTTPRequestHandler):
    server_version = "QWBotMockWebhook/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        if parsed.path == "/__health":
            self._json(200, {"status": "ok", "count": len(_snapshot())})
            return
        if parsed.path == "/__messages":
            self._json(200, {"messages": _snapshot()})
            return
        if parsed.path.startswith("/__messages/"):
            prefix = parsed.path[len("/__messages/"):]
            self._json(200, {"messages": _snapshot(prefix)})
            return
        self._json(404, {"errcode": 404, "errmsg": f"unknown path {parsed.path}"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        body = self._read_body()

        if parsed.path == "/__reset":
            with _LOCK:
                MESSAGES.clear()
            self._json(200, {"status": "reset"})
            return

        if parsed.path != "/cgi-bin/webhook/send":
            self._json(404, {"errcode": 404, "errmsg": f"unknown path {parsed.path}"})
            return

        key = (parse_qs(parsed.query).get("key") or [""])[0]
        try:
            payload = json.loads(body) if body else None
        except json.JSONDecodeError:
            payload = {"raw": body}

        _record(
            {
                "received_at": datetime.now().isoformat(timespec="seconds"),
                "key": key,
                "msgtype": (payload or {}).get("msgtype"),
                "content": _extract_content(payload),
                "payload": payload,
            }
        )
        # SEC-04: never echo a full key into the console/log, even in this local mock.
        masked = f"{key[:4]}****" if len(key) > 8 else ("****" if key else "(none)")
        print(f"[mock-webhook] key={masked} content={_extract_content(payload)!r}")

        if key.startswith("HTTP500"):
            self._json(500, {"errcode": 500, "errmsg": "simulated upstream failure"})
            return
        if key.startswith("FAIL"):
            self._json(200, {"errcode": 93000, "errmsg": "invalid webhook url"})
            return
        self._json(200, {"errcode": 0, "errmsg": "ok"})

    def _read_body(self) -> str:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return ""
        return self.rfile.read(length).decode("utf-8", errors="replace")

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003 - http.server API
        # Keep the console readable; payload logging happens in do_POST.
        return


def _extract_content(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("markdown", "text"):
        section = payload.get(key)
        if isinstance(section, dict) and section.get("content"):
            return str(section["content"])
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock WeCom webhook for QWBot self-testing")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8089)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MockWebhookHandler)
    print(f"[mock-webhook] listening on http://{args.host}:{args.port}/cgi-bin/webhook/send")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock-webhook] stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
