"""Shared MCP tool titles and ToolAnnotations for directory compliance."""

from mcp.types import ToolAnnotations

READ_POSTS_TITLE = "Read Posts"
CREATE_POST_TITLE = "Create Post"
UPLOAD_FILE_TITLE = "Upload File"
READ_CIRCLES_TITLE = "Read Circles"
MANAGE_CIRCLES_TITLE = "Manage Circles"
READ_SOCIAL_TITLE = "Read Profiles"
SOCIAL_TITLE = "Follow Agents"
READ_MY_STATUS_TITLE = "Read My Status"
MY_STATUS_TITLE = "Update My Status"
READ_COMMENTS_TITLE = "Read Comments"
INTERACT_TITLE = "Write Interactions"
READ_DIRECT_MESSAGES_TITLE = "Read Direct Messages"
DIRECT_MESSAGE_TITLE = "Send Direct Message"
LIST_MY_AGENTS_TITLE = "List My Agents"
SWITCH_AGENT_TITLE = "Switch Agent"
DISCOVER_TOOLS_TITLE = "Discover Tools"
USE_TOOLS_TITLE = "Use Tools"
AUTHENTICATE_TITLE = "Sign In"


def read_annotations(title: str) -> ToolAnnotations:
    """Annotations for read-only tools (Claude / OpenAI directory requirement)."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=True,
    )


def write_annotations(title: str) -> ToolAnnotations:
    """Annotations for write / mutating tools (Claude / OpenAI directory requirement)."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=True,
        openWorldHint=True,
    )
