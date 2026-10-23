"""P0 security regressions: SEC-04 (key leak), SEC-01 (auth + CSRF), SEC-03 (SSRF).

The end-to-end assertions here are deliberately written against the *raw* response —
body bytes and `Location` header — because that is exactly where the original defect put
the webhook key. Unit-level checks cover the helpers so a future refactor cannot quietly
re-open the same path.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

import pytest
import requests

from qwbot import store, wecom
from qwbot import web as web_module
from qwbot.security import (
    allowed_hosts,
    classify_send_error,
    is_private_host,
    mask_webhook_url,
    redact,
    validate_webhook_url,
)
from tests.conftest import (
    MOCK_CUSTOM_URL,
    MOCK_PROD_URL,
    MOCK_TEST_URL,
    TEST_WEB_PASSWORD,
    PanelClient,
    init_db,
    login,
    make_settings,
)

REAL_SHAPE_KEY = "fbc143bc-e192-42d2-ad3b-bbc5df059e5f"
SECRET_MARKERS = (REAL_SHAPE_KEY, "REALSECRETKEY1234567890", "MOCKPRODKEY0001")


class ExplodingClient:
    """A webhook client whose failure text carries the full URL, like `requests` does."""

    def __init__(
        self,
        webhook_url: str,
        timeout_seconds: int = 10,
        *,
        allowed_hosts: Any = None,
        allow_local: bool = False,
        enforce_allowlist: bool = True,
    ) -> None:
        self.webhook_url = webhook_url

    def _boom(self) -> None:
        raise requests.ConnectionError(
            f"HTTPConnectionPool(host='qyapi.weixin.qq.com', port=443): Max retries exceeded "
            f"with url: {self.webhook_url} "
            "(Caused by NewConnectionError('Failed to establish a new connection'))"
        )

    def send_text(self, content: str, **kwargs: Any) -> None:
        self._boom()

    def send_markdown(self, content: str) -> None:
        self._boom()


@pytest.fixture()
def client(db_path: Path):
    """Signed-in panel client with the mock host whitelisted (same setup as route tests)."""
    from qwbot.web import create_app

    settings = make_settings(db_path)
    init_db(db_path, settings)
    app = create_app(settings)
    app.config["TESTING"] = True
    with app.test_client() as raw:
        assert login(raw), "the test login should succeed"
        yield PanelClient(raw, db_path)


@pytest.fixture()
def failing_client(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Panel client bound to a database whose prod robot holds a real-shaped key."""
    settings = make_settings(
        db_path,
        webhook_url_prod=f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={REAL_SHAPE_KEY}",
        webhook_url_test=MOCK_TEST_URL,
    )
    init_db(db_path, settings)
    from qwbot.web import create_app

    app = create_app(settings)
    app.config["TESTING"] = True
    monkeypatch.setattr(web_module, "WeComWebhookClient", ExplodingClient)
    with app.test_client() as raw:
        assert login(raw), "the test login should succeed"
        yield PanelClient(raw, db_path)


def location(response) -> str:
    return unquote_plus(response.headers.get("Location", ""))


# ---------------------------------------------------------------------------
# SEC-04 — no webhook key may reach the browser or the redirect target
# ---------------------------------------------------------------------------


def test_connectivity_failure_leaks_no_key_in_body_or_location(failing_client) -> None:
    webhook_id = _prod_webhook_id(failing_client)

    response = failing_client.post(f"/webhooks/{webhook_id}/test")

    body = response.get_data(as_text=True)
    target = location(response)
    for marker in SECRET_MARKERS:
        assert marker not in body, "the rendered page must not contain the raw key"
        assert marker not in target, "the Location header must not contain the raw key"
    # The operator still gets an actionable category instead of a stack trace.
    assert "连通性测试失败" in target
    assert any(word in target for word in ("无法连接", "连接被拒绝", "连接超时", "DNS"))


def test_template_test_send_failure_leaks_no_key(failing_client) -> None:
    from qwbot import store

    webhook_id = _prod_webhook_id(failing_client)
    template_id = store.add_message_template(
        failing_client.db_path,
        webhook_id,
        "生产模板",
        "内容",
        send_times=["09:00"],
    )
    response = failing_client.post(f"/templates/{template_id}/test-send", data={})
    body = response.get_data(as_text=True)
    target = location(response)
    for marker in SECRET_MARKERS:
        assert marker not in body
        assert marker not in target
    assert "测试发送失败" in target


def _prod_webhook_id(client) -> int:
    from qwbot import store

    for webhook in store.list_webhooks(client.db_path):
        if webhook["target"] == "prod":
            return int(webhook["id"])
    raise AssertionError("the seeded production webhook is missing")


def test_classify_send_error_never_repeats_exception_text() -> None:
    cases = [
        requests.ConnectionError(
            "Failed to establish a new connection: https://x/send?key=REALSECRETKEY1234567890"
        ),
        requests.Timeout("HTTPSConnectionPool: Read timed out. (read timeout=10)"),
        requests.exceptions.HTTPError("404 Client Error for url: https://x/send?key=REALSECRETKEY1234567890"),
        RuntimeError("WeCom webhook failed: {'errcode': 93000, 'errmsg': 'invalid webhook url'}"),
        ValueError("some unexpected internal detail"),
    ]
    for exc in cases:
        message = classify_send_error(exc, ["REALSECRETKEY1234567890"])
        assert "REALSECRETKEY1234567890" not in message
        assert message, "every failure needs a usable hint"
    assert "超时" in classify_send_error(requests.Timeout("read timed out"))
    assert "DNS" in classify_send_error(
        requests.ConnectionError("name or service not known: nope.invalid")
    )
    assert "404" in classify_send_error(requests.HTTPError("404 Client Error"))
    assert "errcode 93000" in classify_send_error(
        RuntimeError("WeCom webhook failed: {'errcode': 93000}")
    )


def test_redact_scrubs_keys_from_log_bound_text() -> None:
    leaked = f"connectivity test failed: {MOCK_PROD_URL}"
    scrubbed = redact(leaked)
    assert "MOCKPRODKEY0001" not in scrubbed
    assert "key=****" in scrubbed
    # Percent-encoded form (as it appears in a Location header) is caught too.
    assert "MOCKPRODKEY0001" not in redact(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send%3Fkey%3DMOCKPRODKEY0001"
    )
    # A bare uuid-shaped secret with no `key=` marker is masked as well.
    assert REAL_SHAPE_KEY not in redact(f"payload {{'url': '{REAL_SHAPE_KEY}'}}")
    assert "user:pass@" not in redact("failed at http://user:pass@host/x")


def test_server_log_line_is_masked_before_writing(failing_client, caplog) -> None:
    webhook_id = _prod_webhook_id(failing_client)
    with caplog.at_level("WARNING", logger="qwbot.web"):
        failing_client.post(f"/webhooks/{webhook_id}/test")
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert text, "the full failure belongs in the server log"
    for marker in SECRET_MARKERS:
        assert marker not in text


# ---------------------------------------------------------------------------
# SEC-01 — authentication on the panel + CSRF on every mutating route
# ---------------------------------------------------------------------------


MUTATING_ROUTES: list[tuple[str, dict[str, Any]]] = [
    ("/items/batch_plan", {"natural_date": "2026-10-01", "content": "x"}),
    ("/items/batch_plan/1", {"content": "x"}),
    ("/items/batch_plan/1/delete", {}),
    ("/items/batch_plan/1/archive", {}),
    ("/items/batch_plan/1/start", {}),
    ("/items/batch_plan/1/complete", {}),
    ("/items/batch_plan/1/close-block", {}),
    ("/batch-event-notify", {"enabled": "true"}),
    ("/webhooks", {"name": "x", "url": MOCK_CUSTOM_URL}),
    ("/webhooks/import-env", {}),
    ("/webhooks/1", {"name": "x"}),
    ("/webhooks/1/delete", {}),
    ("/webhooks/1/toggle", {}),
    ("/webhooks/1/set-default", {}),
    ("/webhooks/1/test", {}),
    ("/webhooks/1/templates", {"name": "t"}),
    ("/templates/1", {"name": "t"}),
    ("/templates/1/delete", {}),
    ("/templates/1/toggle", {}),
    ("/templates/1/duplicate", {}),
    ("/templates/1/test-send", {}),
    ("/templates/1/schedules", {"send_time": "10:00"}),
    ("/schedules/1", {"send_time": "10:00"}),
    ("/schedules/1/delete", {}),
    ("/schedules/1/toggle", {}),
    ("/templates/1/skip-date", {"target_date": "2026-10-01"}),
    ("/templates/1/force-date", {"target_date": "2026-10-01"}),
    ("/templates/1/date-overrides/delete", {"target_date": "2026-10-01"}),
    ("/scheduler-skip-dates", {"skip_date": "2026-10-01"}),
    ("/scheduler-skip-dates/delete", {"skip_date": "2026-10-01"}),
    ("/scheduler-force-dates", {"force_date": "2026-10-01"}),
    ("/scheduler-force-dates/delete", {"force_date": "2026-10-01"}),
    ("/template-vars", {"var_name": "v", "var_value": "x"}),
    ("/template-vars/1", {"var_name": "v", "var_value": "x"}),
    ("/template-vars/1/delete", {}),
]


@pytest.mark.parametrize("route,data", MUTATING_ROUTES)
def test_anonymous_post_is_refused_on_every_mutating_route(
    db_path: Path, route: str, data: dict[str, Any]
) -> None:
    from qwbot.web import create_app

    settings = make_settings(db_path)
    init_db(db_path, settings)
    app = create_app(settings)
    app.config["TESTING"] = True
    with app.test_client() as raw:
        response = raw.post(route, data=data)
        assert response.status_code == 401, route
        assert "登录" in response.get_data(as_text=True), route


def test_notification_panel_requires_login_but_index_stays_public(
    db_path: Path,
) -> None:
    from qwbot.web import create_app

    settings = make_settings(db_path)
    init_db(db_path, settings)
    app = create_app(settings)
    app.config["TESTING"] = True
    with app.test_client() as raw:
        anonymous = raw.get("/notifications")
        assert anonymous.status_code == 302
        assert "/login" in location(anonymous)
        # The batch board stays readable; only its mutating controls disappear.
        page = raw.get("/")
        assert page.status_code == 200
        html = page.get_data(as_text=True)
        assert "跑批计划维护" in html
        assert "退出登录" not in html
        assert 'href="/login' in html, "anonymous visitors get a way to log in"


def test_anonymous_index_is_read_only(db_path: Path) -> None:
    """SEC-01: the batch board stays readable, but offers no mutating controls."""
    from qwbot import store
    from qwbot.web import create_app

    settings = make_settings(db_path)
    init_db(db_path, settings)
    store.add_item(
        db_path,
        "batch_plan",
        {
            "natural_date": web_module._today_iso(settings.timezone),
            "content": "只读检查",
            "execution_status": "待执行",
            "status": "待执行",
        },
    )
    app = create_app(settings)
    app.config["TESTING"] = True
    with app.test_client() as raw:
        html = raw.get("/").get_data(as_text=True)
        assert "跑批计划维护" in html
        assert "登录后可维护跑批计划" in html
        assert 'action="/items/batch_plan' not in html
        assert "退出登录" not in html


def test_wrong_password_is_rejected_and_token_cannot_be_forced(
    db_path: Path,
) -> None:
    from qwbot.web import create_app

    settings = make_settings(db_path)
    init_db(db_path, settings)
    app = create_app(settings)
    app.config["TESTING"] = True
    with app.test_client() as raw:
        page = raw.get("/login")
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page.get_data(as_text=True))
        assert token
        bad = raw.post(
            "/login",
            data={"username": "admin", "password": "guess", "csrf_token": token.group(1)},
        )
        assert bad.status_code == 200
        assert "用户名或口令不正确" in bad.get_data(as_text=True)
        assert raw.get("/notifications").status_code == 302
        good = raw.post(
            "/login",
            data={"username": "admin", "password": TEST_WEB_PASSWORD, "csrf_token": token.group(1)},
        )
        assert good.status_code == 302
        assert raw.get("/notifications").status_code == 200


def test_cross_site_post_without_csrf_token_is_blocked(client) -> None:
    """A logged-in browser posting a forged form must not be able to change anything."""
    from qwbot import store

    before = store.count_webhooks(client.db_path)
    response = client.post("/webhooks", data={"name": "入侵", "url": MOCK_CUSTOM_URL}, csrf=False)
    assert response.status_code == 200
    assert "页面已过期" in response.get_data(as_text=True)
    assert store.count_webhooks(client.db_path) == before


def test_csrf_token_header_is_accepted(client) -> None:
    from qwbot import store

    before = store.count_webhooks(client.db_path)
    # Bypass the wrapper on purpose: this checks the header channel, not the form one.
    response = client.raw().post(
        "/webhooks",
        data={"name": "带令牌", "url": MOCK_CUSTOM_URL},
        headers={"X-CSRF-Token": client.csrf_token()},
    )
    assert response.status_code == 302
    assert store.count_webhooks(client.db_path) == before + 1


def test_panel_forms_all_carry_a_csrf_token(client) -> None:
    html = client.get("/notifications").get_data(as_text=True)
    forms = re.findall(r"<form[^>]*method=\"post\"[^>]*>", html)
    assert forms, "the panel renders POST forms"
    tokens = re.findall(r'name="csrf_token"', html)
    assert len(tokens) >= len(forms)


def test_panel_is_locked_when_no_password_is_configured(db_path: Path) -> None:
    """Fail closed: without `QWBOT_WEB_PASSWORD` nothing mutating is reachable."""
    from qwbot.web import create_app

    settings = make_settings(db_path, web_password=None)
    init_db(db_path, settings)
    app = create_app(settings)
    app.config["TESTING"] = True
    with app.test_client() as raw:
        assert raw.get("/notifications").status_code == 302
        response = raw.post(
            "/webhooks",
            data={"name": "x", "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=z"},
        )
        assert response.status_code == 401
        assert "QWBOT_WEB_PASSWORD" in response.get_data(as_text=True)
        # The read-only board still works so the plan stays visible.
        assert raw.get("/").status_code == 200


def test_login_page_has_no_webhook_data(db_path: Path) -> None:
    from qwbot.web import create_app

    settings = make_settings(db_path)
    init_db(db_path, settings)
    app = create_app(settings)
    app.config["TESTING"] = True
    with app.test_client() as raw:
        html = raw.get("/login").get_data(as_text=True)
        assert "生产群机器人" not in html
        assert MOCK_PROD_URL not in html


# ---------------------------------------------------------------------------
# SEC-03 — host whitelist + private-address refusal
# ---------------------------------------------------------------------------


OFFICIAL = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={REAL_SHAPE_KEY}"


@pytest.mark.parametrize(
    "url,expected_ok",
    [
        (OFFICIAL, True),
        ("http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", True),
        ("ftp://qyapi.weixin.qq.com/send", False),
        ("not-a-url", False),
        ("", False),
        ("https://evil.example.com/cgi-bin/webhook/send?key=x", False),
        ("https://qyapi.weixin.qq.com.evil.example.com/send", False),
        ("http://127.0.0.1:6379/", False),
        ("http://localhost:6379/", False),
        ("http://0.0.0.0:80/", False),
        ("http://169.254.169.254/latest/meta-data/", False),
        ("http://10.1.2.3/", False),
        ("http://172.16.0.9/", False),
        ("http://192.168.1.10/admin", False),
        ("http://[::1]/", False),
        ("http://2130706433/", False),
        ("http://intranet.internal/", False),
        ("http://metadata.local/", False),
        ("http://100.64.0.1/", False),
    ],
)
def test_validate_webhook_url(url: str, expected_ok: bool) -> None:
    ok, message = validate_webhook_url(url)
    assert ok is expected_ok, message
    if not ok:
        assert message


def test_whitelist_extends_through_configuration() -> None:
    ok, _ = validate_webhook_url("https://hooks.example.com/send?key=x")
    assert ok is False
    ok, message = validate_webhook_url(
        "https://hooks.example.com/send?key=x", extra_hosts=("Hooks.Example.COM",)
    )
    assert ok is True, message
    assert allowed_hosts(("a.example",)) == ("qyapi.weixin.qq.com", "a.example")
    assert allowed_hosts(None) == ("qyapi.weixin.qq.com",)


def test_local_mode_lets_the_mock_service_through() -> None:
    mock = "http://127.0.0.1:8089/cgi-bin/webhook/send?key=MOCKTEST0001"
    assert validate_webhook_url(mock)[0] is False
    ok, message = validate_webhook_url(mock, extra_hosts=("127.0.0.1",), allow_local=True)
    assert ok is True, message
    # ...but the metadata endpoint stays closed in local mode too.
    assert validate_webhook_url("http://169.254.169.254/", allow_local=True)[0] is False
    assert validate_webhook_url("http://metadata.google.internal/", allow_local=True)[0] is False


def test_is_private_host_covers_rfc1918_linklocal_loopback() -> None:
    for host in (
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "10.0.0.1",
        "172.16.9.9",
        "192.168.0.1",
        "localhost",
        "intranet.internal",
        "db.local",
    ):
        assert is_private_host(host), host
    for host in ("qyapi.weixin.qq.com", "8.8.8.8", "example.com"):
        assert not is_private_host(host), host


def test_submitting_an_internal_webhook_is_rejected_by_the_route(client) -> None:
    from qwbot import store

    before = store.count_webhooks(client.db_path)
    response = client.post(
        "/webhooks",
        data={"name": "探针", "url": "http://10.1.2.3/latest/meta-data/"},
    )
    assert "内网" in location(response)
    # The cloud metadata address is refused even harder: link-local is never unlocked.
    response = client.post(
        "/webhooks",
        data={"name": "元数据", "url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert "内网" in location(response)
    assert store.count_webhooks(client.db_path) == before


def test_local_mode_still_refuses_rfc1918_targets(db_path: Path) -> None:
    """`QWBOT_ALLOW_INSECURE_LOCAL_WEBHOOKS=1` must not open the intranet up."""
    ok, message = validate_webhook_url("http://192.168.1.10/admin", allow_local=True)
    assert ok is False
    assert "内网" in message
    assert validate_webhook_url("http://127.0.0.1:8089/send", allow_local=True)[0] is True


def test_submitting_an_unknown_external_host_is_rejected_by_the_route(client) -> None:
    from qwbot import store

    before = store.count_webhooks(client.db_path)
    response = client.post(
        "/webhooks",
        data={"name": "外部", "url": "https://attacker.example.com/send?key=x"},
    )
    assert "不在允许列表" in location(response)
    assert store.count_webhooks(client.db_path) == before


def test_official_weixin_url_is_accepted_by_the_route(client) -> None:
    from qwbot import store

    response = client.post(
        "/webhooks",
        data={"name": "官方", "url": OFFICIAL, "is_enabled": "true"},
    )
    assert "已新增机器人" in location(response)
    created = next(
        item for item in store.list_webhooks(client.db_path) if item["name"] == "官方"
    )
    assert created["url"] == OFFICIAL


def test_mask_webhook_url_still_masks_short_keys() -> None:
    assert mask_webhook_url("https://x/send?key=abc") == "https://x/send?key=****"
    assert mask_webhook_url(OFFICIAL) == (
        f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={REAL_SHAPE_KEY[:4]}****{REAL_SHAPE_KEY[-4:]}"
    )


# ---------------------------------------------------------------------------
# H-1 — SEC-03 must hold at the send boundary, not only in the web form
# ---------------------------------------------------------------------------


def test_client_refuses_a_non_whitelisted_target_without_sending(monkeypatch) -> None:
    """A stored row pointing anywhere off-whitelist is refused before any socket opens."""
    posts: list[str] = []
    monkeypatch.setattr(
        wecom.requests, "post", lambda url, **kwargs: posts.append(url) or _FakeResponse()
    )

    client = wecom.WeComWebhookClient("http://10.1.2.3/latest/meta-data/")
    with pytest.raises(wecom.WebhookTargetRefused):
        client.send_text("x")
    assert posts == [], "the guard must run before requests.post"


def test_client_refuses_the_metadata_address_even_in_local_mode(monkeypatch) -> None:
    posts: list[str] = []
    monkeypatch.setattr(
        wecom.requests, "post", lambda url, **kwargs: posts.append(url) or _FakeResponse()
    )

    client = wecom.WeComWebhookClient(
        "http://169.254.169.254/latest/meta-data/", allow_local=True
    )
    with pytest.raises(wecom.WebhookTargetRefused):
        client.send_text("x")
    assert posts == []


def test_client_allows_the_official_host_and_a_configured_extra_host(monkeypatch) -> None:
    posts: list[str] = []
    monkeypatch.setattr(
        wecom.requests, "post", lambda url, **kwargs: posts.append(url) or _FakeResponse()
    )

    wecom.WeComWebhookClient(OFFICIAL).send_text("x")
    wecom.WeComWebhookClient(
        "https://hooks.example.com/send?key=x", allowed_hosts=("Hooks.Example.COM",)
    ).send_markdown("x")
    assert len(posts) == 2


def test_guard_error_message_never_carries_the_key() -> None:
    """SEC-04 still holds on this new path: a refusal may be printed or logged verbatim.

    The message reports the *host* rather than a masked URL, because `mask_webhook_url`
    clips the middle of whatever string it gets and turns a path like
    `/latest/meta-data/` into misleading text (`169.254.1****-data/`). Nothing past the
    host is echoed at all, so the query string — where the key lives — cannot appear.
    """
    leaky = f"https://attacker.example.com/cgi-bin/webhook/send?key={REAL_SHAPE_KEY}"
    with pytest.raises(wecom.WebhookTargetRefused) as error:
        wecom.guard_webhook_target(leaky)
    message = str(error.value)
    assert REAL_SHAPE_KEY not in message
    assert "key" not in message.lower().replace("webhook key", "")
    # Only the host survives into the operator-visible part.
    assert "attacker.example.com" in message
    assert "/cgi-bin/webhook/send" not in message


def test_guard_error_message_names_the_host_for_a_local_target() -> None:
    """The refused address must stay readable — an operator acts on this text."""
    with pytest.raises(wecom.WebhookTargetRefused) as error:
        wecom.guard_webhook_target("http://169.254.169.254/latest/meta-data/")
    assert "169.254.169.254" in str(error.value)
    assert "-data/" not in str(error.value), "the path must not be half-masked"


def test_cli_send_path_blocks_a_poisoned_stored_row(db_path: Path, monkeypatch) -> None:
    """The real H-1 hole: a row that never passed the web form must not reach the network.

    Inserted straight through `store.add_webhook` (bypassing the route validation, exactly
    like an `.env` seed, a restored backup or an older build would), then sent via the CLI
    dispatch helper that the scheduler also uses.
    """
    from qwbot import cli

    settings = make_settings(db_path)
    init_db(db_path, settings)
    poison = "http://127.0.0.1:9/leak"
    store.add_webhook(db_path, "投毒行", poison, target="custom", is_enabled=True)

    posts: list[str] = []
    monkeypatch.setattr(
        wecom.requests, "post", lambda url, **kwargs: posts.append(url) or _FakeResponse()
    )
    monkeypatch.setattr(cli, "WeComWebhookClient", wecom.WeComWebhookClient, raising=False)

    entry = {
        "webhook_url": poison,
        "webhook_target": "custom",
        "webhook_name": "投毒行",
        "template_name": "模板",
        "send_time": "09:00",
    }
    sent = cli._send_markdown(settings, entry, "正文")

    assert sent is False, "a refused target must count as a failed send, not a success"
    assert posts == [], "nothing may leave the process for a non-whitelisted host"


def test_env_seed_with_an_internal_url_is_not_imported(db_path: Path) -> None:
    """`import-env` cannot be used to smuggle an intranet probe into the table."""
    settings = make_settings(db_path)
    init_db(db_path, settings)
    before = store.count_webhooks(db_path)

    seeds = [
        {"name": "内网探针", "url": "http://192.168.1.10/admin", "target": "custom"},
        {"name": "官方", "url": OFFICIAL, "target": "prod"},
    ]
    added = store.import_env_webhooks(
        db_path, seeds, "prod", allowed_hosts=settings.allowed_webhook_hosts
    )

    assert added == 1, "the good seed still imports while the bad one is refused"
    urls = {item["url"] for item in store.list_webhooks(db_path)}
    assert "http://192.168.1.10/admin" not in urls
    assert store.count_webhooks(db_path) == before + 1


class _FakeResponse:
    """Minimal stand-in for a successful WeCom reply."""

    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"errcode": 0, "errmsg": "ok"}
