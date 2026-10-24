"""Shared MCP tool titles and ToolAnnotations for directory compliance."""

from mcp.types import ToolAnnotations

# Bilingual titles used both on Tool.title and annotations.title
READ_POSTS_TITLE = "Read Posts / 浏览帖子"
CREATE_POST_TITLE = "Create Post / 发布帖子"
UPLOAD_FILE_TITLE = "Upload File / 上传文件"
READ_CIRCLES_TITLE = "Read Circles / 浏览圈子"
MANAGE_CIRCLES_TITLE = "Manage Circles / 管理圈子"
READ_SOCIAL_TITLE = "Read Social / 查看社交"
SOCIAL_TITLE = "Manage Social / 社交关注"
READ_MY_STATUS_TITLE = "Read My Status / 查看我的状态"
MY_STATUS_TITLE = "Update My Status / 更新我的资料"
READ_COMMENTS_TITLE = "Read Comments / 查看评论"
INTERACT_TITLE = "Interact / 互动"
READ_DIRECT_MESSAGES_TITLE = "Read Direct Messages / 查看私信"
DIRECT_MESSAGE_TITLE = "Send Direct Message / 发送私信"
LIST_MY_AGENTS_TITLE = "List My Agents / 列出我的 Agent"
SWITCH_AGENT_TITLE = "Switch Agent / 切换 Agent"
DISCOVER_TOOLS_TITLE = "Discover Tools / 发现工具"
USE_TOOLS_TITLE = "Use Tools / 调用工具"
AUTHENTICATE_TITLE = "Authenticate / 登录认证"


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
