#!/usr/bin/env python3
"""End-to-end self-test for the QWB-002 webhook model.

Drives a running QWBot web service and CLI against a mock WeCom webhook
(`scripts/mock-webhook.py`) and asserts the whole webhook -> message template ->
notification time chain works: seeding, CRUD, masking, per-robot delivery,
failure isolation, enable/disable, scheduling, preview and cascade delete.

Normally invoked through `scripts/selftest.sh`, which starts the mock webhook and
the web service first. It can also be run manually:

    python3 scripts/selftest.py --base-url http://127.0.0.1:5000 \
        --mock-url http://127.0.0.1:8089 --db data/qwbot.sqlite3
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from qwbot import store  # noqa: E402

DEV_KEY = "MOCKDEV0001"
PROD_KEY = "MOCKPROD0001"
TEST_KEY = "MOCKTEST0001"
BROKEN_KEY = "FAILDEV0001"
BATCH_KEY = "MOCKBATCH001"
SEND_TIME = "23:58"
EXTRA_TIME = "23:59"

PASSED: list[str] = []


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
        return None


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label} {detail}".strip())
    PASSED.append(label)
    print(f"  [ok] {label}")


def step(title: str) -> None:
    print(f"\n=== {title} ===")


class Harness:
    def __init__(
        self,
        base_url: str,
        mock_url: str,
        webhook_base: str,
        db_path: Path,
        cli_command: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.mock_url = mock_url.rstrip("/")
        # Where QWBot itself must point its webhooks. Inside a container that is
        # `host.docker.internal`, while the harness still talks to 127.0.0.1.
        self.webhook_base = webhook_base.rstrip("/")
        self.db_path = db_path
        self.cli_command = shlex.split(cli_command)

    # -- HTTP helpers ----------------------------------------------------
    def get(self, path: str) -> str:
        with urllib.request.urlopen(f"{self.base_url}{path}") as response:
            return response.read().decode("utf-8")

    def post(self, path: str, data: dict[str, str] | None = None) -> tuple[int, str]:
        body = urllib.parse.urlencode(data or {}).encode()
        request = urllib.request.Request(f"{self.base_url}{path}", data=body, method="POST")
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request) as response:
                return response.status, urllib.parse.unquote_plus(
                    response.headers.get("Location", "")
                )
        except urllib.error.HTTPError as exc:
            return exc.code, urllib.parse.unquote_plus(exc.headers.get("Location", ""))

    # -- mock webhook helpers --------------------------------------------
    def mock_reset(self) -> None:
        request = urllib.request.Request(f"{self.mock_url}/__reset", data=b"", method="POST")
        with urllib.request.urlopen(request) as response:
            response.read()

    def mock_messages(self, key: str | None = None) -> list[dict]:
        suffix = f"/{key}" if key else ""
        with urllib.request.urlopen(f"{self.mock_url}/__messages{suffix}") as response:
            return json.loads(response.read())["messages"]

    def mock_webhook_url(self, key: str) -> str:
        return f"{self.webhook_base}/cgi-bin/webhook/send?key={key}"

    # -- CLI helper -------------------------------------------------------
    def cli(self, *args: str) -> tuple[int, str]:
        """Run the CLI. `--cli-command` allows running it inside a container."""
        result = subprocess.run(
            [*self.cli_command, *args],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        return result.returncode, (result.stdout + result.stderr).strip()


def run(h: Harness) -> None:
    step("0. preflight: the database must not contain leftovers from a previous run")
    leftovers = [
        item["name"]
        for item in store.list_webhooks(h.db_path)
        if item["name"] in {"联调群", "失效群"}
    ]
    if leftovers:
        raise AssertionError(
            f"{h.db_path} still contains {leftovers} from an earlier run; "
            "delete the database file and restart the service first"
        )
    check("the database is clean", True)

    step("1. webhooks are seeded from .env and the page masks every key")
    tree = store.load_webhook_tree(h.db_path)
    names = {webhook["name"] for webhook in tree}
    check("both .env webhooks were seeded", {"生产群机器人", "自测群机器人"} <= names, str(names))
    check("a default webhook exists", store.get_default_webhook(h.db_path) is not None)

    html = h.get("/notifications")
    check("notification page renders", "群机器人与消息通知" in html)
    check("webhook keys are masked", "MOCK****0001" in html)
    check("raw prod key is never rendered", PROD_KEY not in html)
    check("raw test key is never rendered", TEST_KEY not in html)
    check(
        "the built-in reminder was migrated with its times",
        any(
            template["name"] == store.MIGRATED_REMINDER_TEMPLATE_NAME
            and {"09:00", "18:00"} <= {item["send_time"] for item in template["schedules"]}
            for webhook in tree
            for template in webhook["templates"]
        ),
    )

    step("2. create a third webhook with its own template and two times")
    status, location = h.post(
        "/webhooks",
        {
            "name": "联调群",
            "url": h.mock_webhook_url(DEV_KEY),
            "target": "custom",
            "is_enabled": "true",
        },
    )
    check("POST /webhooks redirects", status == 302, location)
    check("success notice is returned", "已新增机器人" in location, location)

    dev = next(item for item in store.list_webhooks(h.db_path) if item["name"] == "联调群")
    dev_id = int(dev["id"])

    status, location = h.post(
        f"/webhooks/{dev_id}/templates",
        {
            "name": "联调提醒",
            "content": "## 联调提醒\n进度：[点击]({progress_doc_link})",
            "date_rule": "all",
            "at_all": "true",
            "is_enabled": "true",
            "send_time": SEND_TIME,
        },
    )
    check("POST template redirects", status == 302, location)
    template = next(
        item
        for item in store.list_message_templates(h.db_path, dev_id)
        if item["name"] == "联调提醒"
    )
    template_id = int(template["id"])
    check("@所有人 was stored", template["at_all"] is True)

    status, location = h.post(
        f"/templates/{template_id}/schedules", {"send_time": EXTRA_TIME, "date_rule": ""}
    )
    check("POST schedule redirects", status == 302, location)
    schedules = store.list_template_schedules(h.db_path, template_id)
    check(
        "the template now has two times",
        [item["send_time"] for item in schedules] == [SEND_TIME, EXTRA_TIME],
        str(schedules),
    )
    check(
        "a schedule created without an explicit flag is enabled",
        all(item["is_enabled"] for item in schedules),
    )

    step("3. duplicate and invalid input are rejected")
    _, location = h.post("/webhooks", {"name": "", "url": h.mock_webhook_url("OTHER")})
    check("empty name is rejected", "请填写机器人名称" in location, location)
    _, location = h.post("/webhooks", {"name": "x", "url": "ftp://nope"})
    check("non-http url is rejected", "http" in location, location)
    _, location = h.post("/webhooks", {"name": "x", "url": h.mock_webhook_url(DEV_KEY)})
    check("duplicate url is rejected", "已存在于" in location, location)

    step("4. connectivity test and template test-send only hit their own robot")
    h.mock_reset()
    _, location = h.post(f"/webhooks/{dev_id}/test")
    check("connectivity test reports success", "连通性测试成功" in location, location)
    messages = h.mock_messages(DEV_KEY)
    check("exactly one message arrived", len(messages) == 1, str(messages))
    check("it is a text message", messages[0]["msgtype"] == "text")

    h.mock_reset()
    _, location = h.post(f"/templates/{template_id}/test-send", {"content": "联调测试内容"})
    check("test-send reports success", "已测试发送到「联调群」" in location, location)
    messages = h.mock_messages(DEV_KEY)
    check("one markdown message arrived", len(messages) == 1, str(messages))
    check("unsaved form content was used", messages[0]["content"] == "联调测试内容\n\n<@all>")
    check("the prod robot received nothing", h.mock_messages(PROD_KEY) == [])
    check("the test robot received nothing", h.mock_messages(TEST_KEY) == [])

    step("5. variables are rendered from .env when the DB value is empty")
    h.mock_reset()
    h.post(f"/templates/{template_id}/test-send", data={})
    content = h.mock_messages(DEV_KEY)[0]["content"]
    check("no placeholder is left", "{progress_doc_link}" not in content, content)
    check("@all is appended once", content.count("<@all>") == 1, content)

    step(f"6. the scheduler dispatches at {SEND_TIME}")
    h.mock_reset()
    code, out = h.cli("run-scheduled-once", "--at", SEND_TIME)
    print("     " + out.replace("\n", "\n     "))
    check("CLI exits cleanly", code == 0, out)
    check("the dev robot received the message", len(h.mock_messages(DEV_KEY)) == 1)
    check(
        "the dispatch log names the robot and the template",
        f"Sent: 联调群 / 联调提醒 at {SEND_TIME}." in out,
        out,
    )

    step("7. a broken webhook does not stop the others")
    h.post(
        "/webhooks",
        {"name": "失效群", "url": h.mock_webhook_url(BROKEN_KEY), "target": "custom",
         "is_enabled": "true"},
    )
    broken_id = int(
        next(item for item in store.list_webhooks(h.db_path) if item["name"] == "失效群")["id"]
    )
    h.post(
        f"/webhooks/{broken_id}/templates",
        {"name": "失效模板", "content": "坏内容", "date_rule": "all", "is_enabled": "true",
         "send_time": SEND_TIME},
    )
    h.mock_reset()
    code, out = h.cli("run-scheduled-once", "--at", SEND_TIME)
    print("     " + out.replace("\n", "\n     "))
    check("CLI still exits cleanly", code == 0, out)
    check("the failure is logged", "Failed: 失效群 / 失效模板" in out, out)
    check("the healthy robot still received its message", len(h.mock_messages(DEV_KEY)) == 1)

    step("8. disabling a template stops the dispatch")
    h.post(f"/templates/{template_id}/toggle")
    check(
        "the template is disabled",
        store.get_message_template(h.db_path, template_id)["is_enabled"] is False,
    )
    h.mock_reset()
    h.cli("run-scheduled-once", "--at", SEND_TIME)
    check("nothing reached the dev robot", h.mock_messages(DEV_KEY) == [])
    h.post(f"/templates/{template_id}/toggle")
    h.post(f"/webhooks/{broken_id}/delete")
    check("the broken webhook was removed", store.get_webhook(h.db_path, broken_id) is None)

    step("9. disabling a single time keeps the other one alive")
    schedules = store.list_template_schedules(h.db_path, template_id)
    extra_id = next(int(item["id"]) for item in schedules if item["send_time"] == EXTRA_TIME)
    h.post(f"/schedules/{extra_id}/toggle")
    plan = store.load_dispatch_plan(h.db_path)
    check(
        "only the enabled time stays in the plan",
        [entry["send_time"] for entry in plan if entry["template_id"] == template_id]
        == [SEND_TIME],
    )
    h.post(f"/schedules/{extra_id}/toggle")
    plan = store.load_dispatch_plan(h.db_path)
    check(
        "re-enabling restores the time",
        sorted(entry["send_time"] for entry in plan if entry["template_id"] == template_id)
        == sorted([SEND_TIME, EXTRA_TIME]),
    )

    step("10. CLI preview / send-now / test-webhook honour the selection")
    code, out = h.cli("preview", "--webhook-id", str(dev_id), "--template", str(template_id))
    print("     " + out.replace("\n", "\n     "))
    check("preview exits cleanly", code == 0, out)
    check("preview shows both times", f"{SEND_TIME}/{EXTRA_TIME}" in out, out)

    h.mock_reset()
    code, out = h.cli("send-now", "--webhook-id", str(dev_id), "--template", str(template_id))
    check("send-now exits cleanly", code == 0, out)
    check("send-now delivered once", len(h.mock_messages(DEV_KEY)) == 1)

    h.mock_reset()
    code, out = h.cli("test-webhook", "--webhook", "prod")
    check("test-webhook exits cleanly", code == 0, out)
    check(
        "the prod robot was selected",
        len(h.mock_messages(PROD_KEY)) == 1,
        str([item["key"] for item in h.mock_messages()]),
    )

    step("11. deleting the webhook cascades to templates and times")
    h.post(f"/webhooks/{dev_id}/delete")
    check("the webhook is gone", store.get_webhook(h.db_path, dev_id) is None)
    check("its template is gone", store.get_message_template(h.db_path, template_id) is None)
    check("its times are gone", store.list_template_schedules(h.db_path, template_id) == [])

    step("12. the last webhook cannot be deleted")
    for webhook in store.list_webhooks(h.db_path)[1:]:
        h.post(f"/webhooks/{int(webhook['id'])}/delete")
    only = store.list_webhooks(h.db_path)[0]
    _, location = h.post(f"/webhooks/{int(only['id'])}/delete")
    check("deletion is refused", "至少需要保留一个群机器人" in location, location)
    check("one webhook is still there", store.count_webhooks(h.db_path) == 1)

    step("13. one day with several batches is announced once, per batch only on demand")
    # “今天”以服务端为准，跨时区/跨零点都不会写死日期。
    match = re.search(r'name="natural_date" value="(\d{4}-\d{2}-\d{2})"', h.get("/"))
    check("the plan page reports today", match is not None)
    today = match.group(1) if match else ""

    h.post(
        "/webhooks",
        {
            "name": "跑批群",
            "url": h.mock_webhook_url(BATCH_KEY),
            "target": "custom",
            "is_enabled": "true",
        },
    )
    batch_webhook_id = int(
        next(item for item in store.list_webhooks(h.db_path) if item["name"] == "跑批群")["id"]
    )
    h.post(f"/webhooks/{batch_webhook_id}/set-default")
    check(
        "the batch robot is the default one",
        int(store.get_default_webhook(h.db_path)["id"]) == batch_webhook_id,
    )

    for start_time, current_date, next_date, description in (
        ("10:30", "2026-01-14", "2026-01-15", "日终计提"),
        ("15:30", "2026-01-15", "2026-01-31", "月末"),
    ):
        h.post(
            "/items/batch_plan",
            {
                "natural_date": today,
                "batch_start_time": start_time,
                "current_accounting_date": current_date,
                "next_accounting_date": next_date,
                "description": description,
                "holiday_flag": "N",
                "execution_status": "待执行",
            },
        )
    batches = sorted(
        store.load_status_file(h.db_path)["batch_plan"],
        key=lambda item: item["batch_start_time"],
    )
    check("two batches were registered for today", len(batches) == 2, str(batches))

    h.post(
        f"/webhooks/{batch_webhook_id}/templates",
        {
            "name": "当日批量",
            "content": (
                "当日批开始时间：{today_batch_start_times}\n"
                "当日批后会计日期：{today_post_batch_date}\n"
                "当前交易日：{current_trading_date}\n"
                "明细：\n{today_batch_detail}"
            ),
            "date_rule": "all",
            "is_enabled": "true",
            "send_time": SEND_TIME,
        },
    )
    daily_template_id = int(
        next(
            item
            for item in store.list_message_templates(h.db_path, batch_webhook_id)
            if item["name"] == "当日批量"
        )["id"]
    )

    h.mock_reset()
    h.post(f"/templates/{daily_template_id}/test-send", data={})
    content = h.mock_messages(BATCH_KEY)[0]["content"]
    check("both start times are announced", "10:30、15:30" in content, content)
    check("the post-batch date is the last batch's", "当日批后会计日期：2026-01-31" in content, content)
    check("the current trading date is the first batch's", "当前交易日：2026-01-14" in content, content)
    check(
        "every batch is listed with its own dates",
        "15:30 月末（2026-01-15 → 2026-01-31）" in content,
        content,
    )

    first_index = int(batches[0]["id"])
    h.mock_reset()
    h.post(f"/items/batch_plan/{first_index}/start")
    check("starting a batch does not push by default", h.mock_messages(BATCH_KEY) == [])
    check(
        "the status still moves on",
        store.get_item(h.db_path, "batch_plan", first_index)["execution_status"] == "进行中",
    )
    check("the plan page says so", "不会推送消息" in h.get("/"))

    _, location = h.post("/batch-event-notify", {"enabled": "true"})
    check("the switch reports the new state", "已开启" in location, location)
    check("it is stored in the database", store.batch_event_notify_enabled(h.db_path) is True)

    h.mock_reset()
    h.post(f"/items/batch_plan/{first_index}/complete")
    messages = h.mock_messages(BATCH_KEY)
    check("completing a batch pushes once enabled", len(messages) == 1, str(messages))
    check("it is the completion notice", "批量完成通知" in messages[0]["content"], str(messages))
    check("it names the next batch of the day", "月末" in messages[0]["content"], str(messages))

    h.post("/batch-event-notify", {"enabled": ""})
    check(
        "the switch can be turned off again",
        store.batch_event_notify_enabled(h.db_path) is False,
    )
    h.mock_reset()
    h.post(f"/items/batch_plan/{int(batches[1]['id'])}/start")
    check("starting the second batch stays silent", h.mock_messages(BATCH_KEY) == [])

    h.mock_reset()
    code, out = h.cli("run-scheduled-once", "--at", SEND_TIME)
    check("the daily template is dispatched", code == 0, out)
    dispatched = h.mock_messages(BATCH_KEY)
    check("exactly one daily message went out", len(dispatched) == 1, str(dispatched))
    check(
        "it carries the day's batch times",
        "10:30、15:30" in dispatched[0]["content"],
        str(dispatched),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="QWBot webhook model self-test")
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--mock-url", default="http://127.0.0.1:8089")
    parser.add_argument(
        "--webhook-base",
        default=None,
        help="base URL QWBot uses to reach the mock webhook "
        "(e.g. http://host.docker.internal:8089 when QWBot runs in Docker)",
    )
    parser.add_argument("--db", default="data/qwbot.sqlite3")
    parser.add_argument(
        "--cli-command",
        default=f"{sys.executable} -m qwbot.cli",
        help="command used to invoke the CLI, e.g. 'docker compose exec -T scheduler python -m qwbot.cli'",
    )
    args = parser.parse_args()

    harness = Harness(
        args.base_url,
        args.mock_url,
        args.webhook_base or args.mock_url,
        Path(args.db),
        args.cli_command,
    )
    try:
        run(harness)
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    print(f"\nAll {len(PASSED)} self-test assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
