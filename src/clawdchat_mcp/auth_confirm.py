"""P0 authorization gates shared by hosted HTTP MCP and stdio login.

Only JSON / Python boolean ``True`` counts as confirm_reset. Strings such as
``"true"``, ``"1"``, or ``"yes"`` never rotate or deliver a key.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

CONFIRM_RESET_MUST_BE_BOOL = "confirm_reset must be a boolean (true/false)"
CONFIRM_RESET_FIELD_DESCRIPTION = (
    "Set to true to generate and bind a new API key when the target agent "
    "does not have a stored key."
)
AGENT_NOT_OWNED = "Agent is not in the owned agent list"


class ConfirmResetTypeError(ValueError):
    """Raised when confirm_reset is present but is not a JSON/Python bool."""


def confirm_reset_is_true(value: Any) -> bool:
    """Return True only for the boolean True. Numbers and strings never confirm."""
    return value is True


def parse_confirm_reset(value: Any, *, present: bool) -> bool:
    """Parse a confirm_reset field from JSON or a tool argument.

    Missing / null / False → False (do not rotate).
    True → True.
    Any other type (str, int, list, ...) → ConfirmResetTypeError (HTTP 422).
    """
    if not present or value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ConfirmResetTypeError(CONFIRM_RESET_MUST_BE_BOOL)


def strict_bool_confirm_reset(value: Any) -> bool:
    """Pydantic BeforeValidator: reject string/number truthy coercion."""
    return parse_confirm_reset(value, present=value is not None)


def confirm_reset_from_mapping(body: Mapping[str, Any] | None) -> bool:
    """Read confirm_reset from a JSON object. Raises ConfirmResetTypeError on tamper."""
    if not isinstance(body, Mapping):
        return False
    return parse_confirm_reset(body.get("confirm_reset"), present="confirm_reset" in body)


def find_owned_agent(agents: list[Any] | None, agent_id: str) -> Optional[dict[str, Any]]:
    """Return the owned agent matching id (or name) or None."""
    if not agent_id or not agents:
        return None
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if agent.get("id") == agent_id or agent.get("name") == agent_id:
            return agent
    return None
