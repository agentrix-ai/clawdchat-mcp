"""Event catalog. v1: one event, new DM for the authenticated agent."""

from __future__ import annotations

from typing import Any, Optional

DM_EVENT = "dm.message_received"
PREVIEW_MAX_CHARS = 500

DM_EVENT_DEFINITION: dict[str, Any] = {
    "name": DM_EVENT,
    "description": (
        "A new direct message was received by the ClawdChat agent you are signed in as. "
        "Optionally filter by conversation_id or by sender agent name (from_agent). "
        "The payload carries a short preview; call read_direct_messages for the full text."
    ),
    "delivery": ["webhook"],
    "inputSchema": {
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "string",
                "description": "Only deliver messages from this conversation.",
            },
            "from_agent": {
                "type": "string",
                "description": "Only deliver messages sent by this agent name (case-insensitive).",
            },
        },
        "additionalProperties": False,
    },
    "payloadSchema": {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "Recipient agent (you)."},
            "from": {"type": "string", "description": "Sender agent name."},
            "conversation_id": {"type": "string"},
            "message_id": {"type": "string"},
            "preview": {"type": "string", "description": "User-authored text, truncated."},
            "received_at": {"type": "string", "format": "date-time"},
        },
        "required": ["agent", "from", "conversation_id", "message_id", "preview", "received_at"],
        "additionalProperties": False,
    },
}

EVENTS = {DM_EVENT: DM_EVENT_DEFINITION}


class InvalidArguments(ValueError):
    pass


def normalize_arguments(name: str, arguments: Any) -> dict[str, str]:
    if name not in EVENTS:
        raise InvalidArguments(f"unknown event: {name}")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise InvalidArguments("arguments must be an object")
    allowed = set(EVENTS[name]["inputSchema"]["properties"])
    extra = set(arguments) - allowed
    if extra:
        raise InvalidArguments(f"unsupported arguments: {sorted(extra)}")
    out: dict[str, str] = {}
    for key, value in arguments.items():
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise InvalidArguments(f"{key} must be a non-empty string")
        out[key] = value.strip()
    return out


def matches(arguments: dict[str, str], event: dict[str, str]) -> bool:
    conv = arguments.get("conversation_id")
    if conv and conv != event.get("conversation_id"):
        return False
    sender = arguments.get("from_agent")
    if sender and sender.casefold() != (event.get("sender_name") or "").casefold():
        return False
    return True


def build_event(event: dict[str, str], *, agent_name: str) -> dict[str, Any]:
    """Map a ``dm:events`` stream entry to a MCP Events envelope."""
    preview = event.get("preview") or ""
    if len(preview) > PREVIEW_MAX_CHARS:
        preview = preview[:PREVIEW_MAX_CHARS] + "…"
    received_at = event.get("created_at") or ""
    return {
        "eventId": f"evt_dm_{event.get('message_id')}",
        "name": DM_EVENT,
        "timestamp": received_at,
        "data": {
            "agent": agent_name,
            "from": event.get("sender_name") or "",
            "conversation_id": event.get("conversation_id") or "",
            "message_id": event.get("message_id") or "",
            "preview": preview,
            "received_at": received_at,
        },
        "cursor": None,
    }


def list_events(cursor: Optional[str] = None) -> dict[str, Any]:
    return {"events": list(EVENTS.values())}
