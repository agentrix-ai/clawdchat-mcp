from clawdchat_mcp.events import delivery


def test_signed_headers_set_explicit_user_agent():
    h = delivery.signed_headers("msg_1", b"{}", ["whsec_" + "A" * 32], "sub_1")
    assert h["User-Agent"] == delivery.USER_AGENT
    assert not h["User-Agent"].lower().startswith("python-httpx")
    for k in ("webhook-id", "webhook-timestamp", "webhook-signature", "X-MCP-Subscription-Id"):
        assert k in h
