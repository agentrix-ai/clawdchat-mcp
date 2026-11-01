"""MCP_EVENTS_WEBHOOK_PROXY: egress proxy for webhook POSTs only."""

import logging
import pathlib

import httpx
import pytest

from clawdchat_mcp.config import settings
from clawdchat_mcp.events import delivery, safety

URL = "https://receiver.example.com/mcp-events/cb_1"
PROXY = "socks5h://user:s3cr3tpass@127.0.0.1:40000"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    async def fake_resolve(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(safety, "resolve_ips", fake_resolve)


def _capture(monkeypatch, handler):
    built = []

    def fake_build(proxy):
        built.append(proxy)
        return httpx.MockTransport(handler)

    monkeypatch.setattr(delivery, "build_proxy_transport", fake_build)
    return built


def test_default_off():
    assert type(settings).model_fields["mcp_events_webhook_proxy"].default == ""


@pytest.mark.asyncio
async def test_off_uses_pinned_ip_not_proxy(monkeypatch):
    monkeypatch.setattr(settings, "mcp_events_webhook_proxy", "")
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200)

    built = _capture(monkeypatch, handler)
    res = await delivery.safe_post(URL, b"{}", {}, inner_transport=httpx.MockTransport(handler))
    assert res.status == 200 and built == []
    assert seen[0].url.host == "93.184.216.34"  # pinned


@pytest.mark.asyncio
async def test_on_sends_by_hostname_via_proxy(monkeypatch):
    monkeypatch.setattr(settings, "mcp_events_webhook_proxy", PROXY)
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200)

    built = _capture(monkeypatch, handler)
    res = await delivery.safe_post(URL, b"{}", {"User-Agent": delivery.USER_AGENT})
    assert res.status == 200 and built == [PROXY]
    assert seen[0].url.host == "receiver.example.com"
    assert seen[0].headers["user-agent"] == delivery.USER_AGENT


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "::1"])
async def test_ssrf_still_rejected_with_proxy(monkeypatch, ip):
    monkeypatch.setattr(settings, "mcp_events_webhook_proxy", PROXY)

    async def bad_resolve(host, port):
        return [ip]

    monkeypatch.setattr(safety, "resolve_ips", bad_resolve)
    built = _capture(monkeypatch, lambda r: httpx.Response(200))
    res = await delivery.safe_post(URL, b"{}", {})
    assert res.status is None and res.error.startswith("unsafe_url") and built == []


@pytest.mark.asyncio
async def test_ssrf_rejects_any_private_in_mixed_answer(monkeypatch):
    monkeypatch.setattr(settings, "mcp_events_webhook_proxy", PROXY)

    async def mixed(host, port):
        return ["93.184.216.34", "10.1.2.3"]

    monkeypatch.setattr(safety, "resolve_ips", mixed)
    built = _capture(monkeypatch, lambda r: httpx.Response(200))
    res = await delivery.safe_post(URL, b"{}", {})
    assert res.status is None and built == []


@pytest.mark.asyncio
async def test_no_redirect_with_proxy(monkeypatch):
    monkeypatch.setattr(settings, "mcp_events_webhook_proxy", PROXY)
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(302, headers={"location": "https://evil.example.com/"})

    _capture(monkeypatch, handler)
    res = await delivery.safe_post(URL, b"{}", {})
    assert res.status == 302 and len(calls) == 1


@pytest.mark.asyncio
async def test_proxy_error_does_not_leak_url(monkeypatch, caplog):
    monkeypatch.setattr(settings, "mcp_events_webhook_proxy", PROXY)

    def handler(req):
        raise httpx.ProxyError(f"cannot connect to {PROXY}")

    _capture(monkeypatch, handler)
    caplog.set_level(logging.DEBUG)
    res = await delivery.safe_post(URL, b"{}", {})
    assert res.status is None and res.error == "ProxyError"
    assert "s3cr3tpass" not in res.error and "s3cr3tpass" not in caplog.text


def test_mask_proxy_url():
    assert delivery.mask_proxy_url(PROXY) == "socks5h://127.0.0.1:40000"
    assert "s3cr3t" not in delivery.mask_proxy_url("http://a:s3cr3t@proxy.example:8080/x?k=s3cr3t")


def test_real_proxy_transport_builds_for_socks():
    t = delivery.build_proxy_transport("socks5h://127.0.0.1:40000")  # needs httpx[socks]
    assert isinstance(t, httpx.AsyncHTTPTransport)


def test_proxy_setting_only_used_by_webhook_client():
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "clawdchat_mcp"
    users = sorted(
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if "mcp_events_webhook_proxy" in p.read_text(encoding="utf-8")
    )
    assert users == ["config.py", "events/delivery.py"]
