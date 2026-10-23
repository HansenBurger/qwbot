"""Shared security helpers behind the P0 fixes (SEC-04, SEC-03).

Kept free of Flask and store imports so the web layer, the CLI and the deploy scripts
can all reuse exactly one copy of the masking and classification rules.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import unquote, urlparse

# The official WeCom group-robot endpoint. Anything else needs an explicit opt-in
# through `QWBOT_ALLOWED_WEBHOOK_HOSTS`.
DEFAULT_WEBHOOK_HOSTS: tuple[str, ...] = ("qyapi.weixin.qq.com",)

# Loopback names are only reachable when the operator deliberately turns on local mode.
LOCAL_HOST_NAMES: frozenset[str] = frozenset({"localhost", "localhost.", "*.localhost"})

# The cloud-provider metadata endpoint. A mock webhook never needs it and no legitimate
# robot lives there, so `allow_local` does not unlock it either.
METADATA_ADDRESS_LITERALS: frozenset[str] = frozenset(
    {"169.254.169.254", "169.254.170.2", "fd00:ec2::254", "metadata.google.internal"}
)

_KEY_QUERY_RE = re.compile(r"([?&]key=)([^&#]*)", re.IGNORECASE)
_BEARER_RE = re.compile(r"(authorization[:=\s]+)(?:bearer|basic)\s+\S+", re.IGNORECASE)
_CREDENTIAL_USERINFO_RE = re.compile(r"//([^/\s:@]+):([^/\s@]+)@", re.IGNORECASE)
# Any 32 hex characters (with optional dashes) is a WeCom key shape; errcode payloads
# and exception texts may carry it outside a `?key=` parameter.
_HEX_SECRET_RE = re.compile(r"\b[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\b", re.IGNORECASE)


def normalize_host(host: str | None) -> str:
    """Lowercase, strip brackets/punycoded dots; IDNA errors become ''."""
    if not host:
        return ""
    value = str(host).strip().rstrip(".").lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value


def allowed_hosts(extra: tuple[str, ...] | list[str] | set[str] | None = None) -> tuple[str, ...]:
    """Built-in official hosts plus whatever `.env` adds, normalized."""
    hosts = list(DEFAULT_WEBHOOK_HOSTS)
    for item in extra or ():
        host = normalize_host(item)
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def is_local_host(host: str) -> bool:
    """True for loopback/link-local/private-address literals and local name patterns."""
    value = normalize_host(host)
    if not value:
        return False
    if value in LOCAL_HOST_NAMES or value.endswith(".localhost"):
        return True
    literal = _ip_literal(value)
    if literal is None:
        return False
    return literal.is_loopback or literal.is_link_local or literal.is_private or literal.is_reserved


def _ip_literal(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _dotted_decimal_ipv4(value: str) -> ipaddress.IPv4Address | None:
    """Catch `http://2130706433/` style encodings that are valid IPv4 but not dotted."""
    parts = value.split(".")
    if len(parts) != 4:
        return None
    numbers: list[int] = []
    for part in parts:
        if not part.isdigit() or len(part) > 3:
            return None
        numbers.append(int(part))
    if any(number > 255 for number in numbers):
        return None
    return ipaddress.IPv4Address(".".join(str(number) for number in numbers))


def is_private_host(host: str) -> bool:
    """Reject anything that resolves to a non-public address before the whitelist runs."""
    value = normalize_host(host)
    if not value:
        return False
    if is_local_host(value):
        return True
    literal = _ip_literal(value)
    if literal is not None:
        # Every other IP-shaped literal (RFC1918, carrier-grade NAT, documentation
        # ranges, ...) is refused; only globally routable addresses survive.
        return not literal.is_global
    decimal = _dotted_decimal_ipv4(value)
    if decimal is not None:
        return not decimal.is_global
    # Domain-style names that only exist inside a private network.
    return value.endswith((".local", ".internal", ".lan", ".home.arpa")) or "." not in value


def mask_webhook_url(url: str) -> str:
    """Hide the middle of the webhook key so it is safe to show on screen."""
    value = (url or "").strip()
    if not value:
        return "未配置"
    match = _KEY_QUERY_RE.search(value)
    if match:
        key = match.group(2)
        masked = "****" if len(key) <= 8 else f"{key[:4]}****{key[-4:]}"
        return f"{value[: match.start(2)]}{masked}{value[match.end(2):]}"
    if len(value) <= 24:
        return value
    return f"{value[:16]}****{value[-6:]}"


def redact(text: object) -> str:
    """Scrub every secret-shaped fragment from a message destined for a log line."""
    value = "" if text is None else str(text)
    if not value:
        return value
    # Percent-encoded URLs arrive from `Location` headers and access logs.
    decoded_candidates = [value]
    if "%" in value:
        decoded_candidates.append(unquote(value, errors="replace"))
    seen: list[str] = []
    for candidate in decoded_candidates:
        if candidate not in seen:
            seen.append(candidate)
    out = seen[0]
    for candidate in seen[1:]:
        # Keep the original encoding, but make sure a decoded key cannot hide behind it.
        keys = {match.group(2) for match in _KEY_QUERY_RE.finditer(candidate) if match.group(2)}
        keys |= {f"{match.group(1)}:{match.group(2)}" for match in _CREDENTIAL_USERINFO_RE.finditer(candidate)}
        for secret in sorted(keys, key=len, reverse=True):
            if secret and secret in out:
                out = out.replace(secret, "****")
    out = _KEY_QUERY_RE.sub(lambda match: f"{match.group(1)}****", out)
    out = _CREDENTIAL_USERINFO_RE.sub(lambda match: f"//{match.group(1)}:****@", out)
    out = _BEARER_RE.sub(lambda match: f"{match.group(1)}****", out)
    out = _HEX_SECRET_RE.sub("****", out)
    return out


def validate_webhook_url(
    url: str,
    *,
    extra_hosts: tuple[str, ...] | list[str] | set[str] | None = None,
    allow_local: bool = False,
) -> tuple[bool, str]:
    """SEC-03: scheme + host whitelist + private-address refusal. Returns (ok, message).

    `allow_local` is the self-test escape hatch and it stays deliberately narrow: it
    exempts loopback and link-local targets only. Site-local ranges (RFC1918, CGNAT,
    `.internal`) are refused no matter what, so turning on local mode can never hand an
    operator an intranet probe.
    """
    value = (url or "").strip()
    if not value:
        return False, "请填写机器人 webhook 地址"
    try:
        parsed = urlparse(value)
    except ValueError:
        return False, "webhook 地址格式无效，请检查是否填写完整"
    if parsed.scheme.lower() not in {"http", "https"}:
        return False, "webhook 地址必须以 http:// 或 https:// 开头"
    host = normalize_host(parsed.hostname)
    if not host:
        return False, "webhook 地址缺少主机名"
    permitted = allowed_hosts(extra_hosts)
    whitelisted = host in permitted
    if _is_site_local(host):
        return False, "webhook 地址不允许指向内网地址（如 10.*、172.16-31.*、192.168.*）"
    local = is_local_host(host)
    if local and not (allow_local or whitelisted):
        return False, "webhook 地址不允许指向本机或链路本地地址（如 127.0.0.1、localhost、169.254.*）"
    if not whitelisted and not local:
        listed = ", ".join((*permitted, "本地测试主机")) if allow_local else ", ".join(permitted)
        return False, (
            f"webhook 主机 {host} 不在允许列表中。"
            f"可联系管理员通过 QWBOT_ALLOWED_WEBHOOK_HOSTS 添加（当前：{listed}）"
        )
    return True, ""


def _is_site_local(host: str) -> bool:
    """RFC1918 / CGNAT / reserved literals, metadata IPs and private-only names."""
    value = normalize_host(host)
    if not value:
        return False
    if value in METADATA_ADDRESS_LITERALS:
        return True
    if value.endswith((".local", ".internal", ".lan", ".home.arpa")) or "." not in value:
        return True
    literal = _ip_literal(value) or _dotted_decimal_ipv4(value)
    if literal is None:
        return False
    if literal.is_loopback:
        # Handled by `is_local_host`, which `allow_local` may relax.
        return False
    if literal.is_link_local:
        # Link-local covers the cloud metadata service, so it stays shut even in local mode.
        return True
    return not literal.is_global


def classify_send_error(exc: BaseException, known_secrets: list[str] | None = None) -> str:
    """SEC-04: turn an outbound-send failure into a category hint with no secret in it.

    Never returns the exception text; the full detail belongs in the server log (already
    passed through `redact`).
    """
    # Imported lazily so this module stays importable without requests installed here.
    timeout_types: tuple[type[BaseException], ...] = ()
    connection_types: tuple[type[BaseException], ...] = ()
    http_error_type: type[BaseException] | None = None
    decode_error_type: type[BaseException] | None = None
    try:  # pragma: no cover - requests is optional when this runs as a pure unit
        import requests

        timeout_types = (requests.Timeout,)
        connection_types = (requests.ConnectionError,)
        http_error_type = requests.HTTPError
        decode_error_type = requests.exceptions.JSONDecodeError
    except ImportError:
        timeout_types = connection_types = ()
        http_error_type = decode_error_type = None

    blob = " ".join(
        part for part in (type(exc).__module__, type(exc).__name__, str(exc)) if part
    ).lower()
    for secret in known_secrets or ():
        if secret and secret in blob:
            blob = blob.replace(secret, " ")

    status = _http_status(blob, exc, http_error_type)
    if status is not None:
        if status == 404:
            return "目标地址不存在（HTTP 404），请检查 webhook 路径是否完整"
        if status in {401, 403}:
            return f"访问被拒绝（HTTP {status}），请确认 webhook key 仍然有效"
        if 400 <= status < 500:
            return f"请求被目标服务拒绝（HTTP {status}），请检查 webhook 配置"
        if status in {500, 502, 503, 504}:
            return f"目标服务暂时不可用（HTTP {status}），请稍后重试"
        return f"目标服务返回错误状态码 HTTP {status}"

    wecom_code = _wecom_errcode(blob)
    if wecom_code is not None:
        if wecom_code in {93000, 93001, 301002, 301023, 4001, 44002}:
            return f"企业微信拒绝了该 webhook（errcode {wecom_code}），请确认 key 正确且机器人在群里"
        return f"企业微信返回错误 errcode {wecom_code}，请查看服务端日志中的完整记录"

    if isinstance(exc, timeout_types) or "timeout" in blob or "timed out" in blob:
        return "连接超时，请检查网络或目标服务器状态"
    if isinstance(exc, connection_types) or _looks_like_connection_failure(blob):
        if "name or service not known" in blob or "nodename" in blob or "getaddrinfo" in blob or "dns" in blob:
            return "DNS 解析失败，请检查域名是否正确"
        if "connection refused" in blob or "refused" in blob:
            return "连接被拒绝，请检查目标地址和端口是否正确"
        if "ssl" in blob or "certificate" in blob or "handshake" in blob:
            return "SSL/TLS 证书校验失败，请检查目标服务器的证书配置"
        return "无法连接到目标地址，请检查 webhook 地址和网络连接"
    if "ssl" in blob or "certificate" in blob:
        return "SSL/TLS 证书校验失败，请检查目标服务器的证书配置"
    if decode_error_type is not None and isinstance(exc, decode_error_type):
        return "目标服务返回了无法解析的内容，请确认地址是企业微信机器人 webhook"
    if '"errcode"' in blob or "errcode" in blob:
        return "企业微信返回错误，请查看服务端日志中的完整记录"
    return "发送失败，请检查 webhook 地址和网络连接；完整原因已写入服务端日志"


def _looks_like_connection_failure(blob: str) -> bool:
    markers = (
        "max retries exceeded",
        "failed to establish a new connection",
        "connection refused",
        "connection reset",
        "connection aborted",
        "remote end closed",
        "no route to host",
        "network is unreachable",
        "getaddrinfo",
        "name or service not known",
    )
    return any(marker in blob for marker in markers)


def _http_status(blob: str, exc: BaseException, http_error_type: type[BaseException] | None) -> int | None:
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    if http_error_type is not None and isinstance(exc, http_error_type):
        # `HTTPError` text starts with the status ("404 Client Error ...") even when no
        # response object was attached.
        match = re.search(r"(?<!\d)([45]\d\d)\s+(?:client|server)\s+error", blob)
        if match:
            return int(match.group(1))
    match = re.search(r"http\s*(?:status)?[^\d]{0,6}(\d{3})", blob)
    if match:
        return int(match.group(1))
    match = re.search(r"status\s*code[^\d]{0,6}(\d{3})", blob)
    if match:
        return int(match.group(1))
    return None


def _wecom_errcode(blob: str) -> int | None:
    match = re.search(r"errcode['\"=:\s]{1,6}(-?\d{1,8})", blob)
    return int(match.group(1)) if match else None
