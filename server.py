import asyncio
import logging
import os
import sys
import json
import base64
import uuid
from typing import Optional, Dict, Any, List
from datetime import datetime

# Third-party imports - matching the working server
try:
    from mcp.server import Server
    from mcp.server.sse import SseServerTransport
    from mcp.types import Tool, TextContent
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import Response
    import uvicorn
except ImportError as e:
    print(f"Critical Dependency Missing: {e}")
    print("Run: pip install mcp starlette uvicorn")
    exit(1)

# --- Configuration & Logging ---
GLOBAL_LOG_FILE = "github_server.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("github_server_debug.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("GitHub-MCP-Server")

app_server = Server("GitHub-MCP-Server")

# --- GitHub API Configuration ---
GITHUB_API_BASE = "https://api.github.com"

# --- Authentication Manager ---
class AuthIdentity:
    def __init__(self, id: str, token: str, metadata: Dict[str, Any] = None):
        self.id = id
        self.token = token
        self.metadata = metadata or {}
        self.rate_limit_remaining = None
        self.rate_limit_reset = None

class AuthManager:
    def __init__(self):
        self.identities: Dict[str, AuthIdentity] = {}
        self.active_identity_id: Optional[str] = None
        self._load_from_file()

    def _load_from_file(self):
        try:
            if os.path.exists("credentials.json"):
                with open("credentials.json", "r") as f:
                    data = json.load(f)
                    for ident in data.get("identities", []):
                        self.add_identity(
                            ident["id"],
                            ident["token"],
                            ident.get("metadata")
                        )
                        if not self.active_identity_id:
                            self.switch_identity(ident["id"])
        except Exception as e:
            logger.error(f"Failed to load credentials from file: {e}")

    def add_identity(self, id: str, token: str, metadata: Dict[str, Any] = None):
        self.identities[id] = AuthIdentity(id, token, metadata)
        logger.info(f"Added identity: {id}")

    def switch_identity(self, id: str):
        if id in self.identities:
            self.active_identity_id = id
            logger.info(f"Switched to identity: {id}")
        else:
            logger.error(f"Identity not found: {id}")

    def get_active_identity(self) -> Optional[AuthIdentity]:
        if self.active_identity_id:
            return self.identities.get(self.active_identity_id)
        return None

    def remove_identity(self, id: str):
        if id in self.identities:
            del self.identities[id]
            if self.active_identity_id == id:
                self.active_identity_id = None
                # Try to switch to another if available
                if self.identities:
                    self.active_identity_id = next(iter(self.identities))
            logger.info(f"Removed identity: {id}")

auth_manager = AuthManager()

# --- GitHub Client ---
class GitHubError(Exception):
    def __init__(self, status: int, message: str, data: Any = None):
        super().__init__(f"GitHub API Error {status}: {message}")
        self.status = status
        self.data = data

class GitHubClient:
    def __init__(self, auth_manager: AuthManager):
        self.auth_manager = auth_manager

    async def request(self, method: str, path: str, **kwargs) -> Any:
        import httpx
        
        identity = self.auth_manager.get_active_identity()
        headers = kwargs.pop("headers", {})
        headers["Accept"] = "application/vnd.github.v3+json"
        
        if identity:
            headers["Authorization"] = f"token {identity.token}"
        
        # Determine URL
        if path.startswith("http"):
            url = path
        else:
            url = f"{GITHUB_API_BASE}{path}"

        async with httpx.AsyncClient() as client:
            try:
                response = await client.request(method, url, headers=headers, **kwargs)
                
                # Capture Rate Limit
                if identity:
                    limit = response.headers.get("X-RateLimit-Remaining")
                    reset = response.headers.get("X-RateLimit-Reset")
                    if limit:
                        identity.rate_limit_remaining = int(limit)
                    if reset:
                        identity.rate_limit_reset = int(reset)

                # Read body
                content_type = response.headers.get("Content-Type", "")
                if "json" in content_type:
                    data = response.json()
                else:
                    data = response.text

                if response.status_code >= 400:
                    # Specific error handling
                    msg = f"GitHub API {response.status_code}"
                    if isinstance(data, dict) and "message" in data:
                        msg = data["message"]
                    logger.error(f"GitHub Request Failed: {method} {url} -> {response.status_code} {msg}")
                    raise GitHubError(response.status_code, msg, data)
                
                return data

            except httpx.RequestError as e:
                logger.error(f"Network error: {e}")
                raise Exception(f"Network error: {str(e)}")
            except asyncio.TimeoutError:
                logger.error("Request timed out")
                raise Exception("Request timed out")

github_client = GitHubClient(auth_manager)

# --- Helper Functions ---
def append_to_global_log(text: str):
    """Writes to the permanent full history log."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(GLOBAL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {text}\n")
    except Exception as e:
        logger.error(f"Global Log Error: {e}")

# --- Tool Implementations ---
@app_server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_repositories",
            description="List repositories for the authenticated user or an organization.",
            inputSchema={
                "type": "object",
                "properties": {
                    "visibility": {"type": "string", "enum": ["all", "public", "private"], "default": "all"},
                    "sort": {"type": "string", "default": "updated"},
                    "org": {"type": "string", "description": "Organization name (optional)"}
                }
            }
        ),
        Tool(
            name="create_repository",
            description="Create a new repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "private": {"type": "boolean", "default": False},
                    "auto_init": {"type": "boolean", "default": True},
                    "org": {"type": "string", "description": "Organization to create in (optional)"}
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="get_repository",
            description="Get details of a specific repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="delete_repository",
            description="Delete a repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="transfer_repository",
            description="Transfer a repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "new_owner": {"type": "string"}
                },
                "required": ["owner", "repo", "new_owner"]
            }
        ),
        Tool(
            name="update_repository_archive",
            description="Archive or unarchive a repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "archived": {"type": "boolean"}
                },
                "required": ["owner", "repo", "archived"]
            }
        ),
        Tool(
            name="get_file_contents",
            description="Get the contents of a file (Base64 encoded).",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "path": {"type": "string"},
                    "ref": {"type": "string", "description": "Branch, tag, or commit SHA"}
                },
                "required": ["owner", "repo", "path"]
            }
        ),
        Tool(
            name="create_or_update_file",
            description="Create or update a file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "path": {"type": "string"},
                    "message": {"type": "string", "description": "Commit message"},
                    "content": {"type": "string", "description": "Base64 encoded content"},
                    "sha": {"type": "string", "description": "Blob SHA if updating"},
                    "branch": {"type": "string"}
                },
                "required": ["owner", "repo", "path", "message", "content"]
            }
        ),
        Tool(
            name="delete_file",
            description="Delete a file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "path": {"type": "string"},
                    "message": {"type": "string"},
                    "sha": {"type": "string"},
                    "branch": {"type": "string"}
                },
                "required": ["owner", "repo", "path", "message", "sha"]
            }
        ),
        Tool(
            name="create_branch",
            description="Create a branch (ref).",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "ref": {"type": "string", "description": "The name of the fully qualified reference (ie: refs/heads/master)"},
                    "sha": {"type": "string", "description": "The SHA1 value for this reference."}
                },
                "required": ["owner", "repo", "ref", "sha"]
            }
        ),
        Tool(
            name="delete_branch",
            description="Delete a branch (ref).",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "ref": {"type": "string", "description": "The name of the fully qualified reference (ie: heads/master)"}
                },
                "required": ["owner", "repo", "ref"]
            }
        ),
        Tool(
            name="get_organization",
            description="Get organization information.",
            inputSchema={
                "type": "object",
                "properties": {
                    "org": {"type": "string"}
                },
                "required": ["org"]
            }
        ),
        Tool(
            name="list_commits",
            description="List commits on a repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "sha": {"type": "string", "description": "SHA or branch to start listing from"},
                    "path": {"type": "string", "description": "Filter by file path"},
                    "author": {"type": "string"},
                    "since": {"type": "string"},
                    "until": {"type": "string"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="get_commit",
            description="Get a specific commit.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "ref": {"type": "string", "description": "Commit SHA"}
                },
                "required": ["owner", "repo", "ref"]
            }
        ),
        Tool(
            name="list_issues",
            description="List issues in a repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    "labels": {"type": "string"},
                    "sort": {"type": "string", "default": "created"},
                    "direction": {"type": "string", "default": "desc"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="create_issue",
            description="Create an issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "assignees": {"type": "array", "items": {"type": "string"}},
                    "labels": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["owner", "repo", "title"]
            }
        ),
        Tool(
            name="update_issue",
            description="Update an issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "issue_number": {"type": "integer"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "state": {"type": "string", "enum": ["open", "closed"]},
                    "labels": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["owner", "repo", "issue_number"]
            }
        ),
        Tool(
            name="create_issue_comment",
            description="Create a comment on an issue or PR.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "issue_number": {"type": "integer"},
                    "body": {"type": "string"}
                },
                "required": ["owner", "repo", "issue_number", "body"]
            }
        ),
        Tool(
            name="list_pull_requests",
            description="List pull requests.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    "head": {"type": "string"},
                    "base": {"type": "string"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="create_pull_request",
            description="Create a pull request.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "title": {"type": "string"},
                    "head": {"type": "string"},
                    "base": {"type": "string"},
                    "body": {"type": "string"},
                    "draft": {"type": "boolean"}
                },
                "required": ["owner", "repo", "title", "head", "base"]
            }
        ),
        Tool(
            name="merge_pull_request",
            description="Merge a pull request.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "pull_number": {"type": "integer"},
                    "commit_title": {"type": "string"},
                    "commit_message": {"type": "string"},
                    "merge_method": {"type": "string", "enum": ["merge", "squash", "rebase"], "default": "merge"}
                },
                "required": ["owner", "repo", "pull_number"]
            }
        ),
        Tool(
            name="list_workflows",
            description="List GitHub Actions workflows.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="list_workflow_runs",
            description="List workflow runs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "workflow_id": {"type": "string", "description": "Workflow ID or filename"},
                    "status": {"type": "string"},
                    "event": {"type": "string"}
                },
                "required": ["owner", "repo", "workflow_id"]
            }
        ),
        Tool(
            name="get_workflow_run",
            description="Get a specific workflow run.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "run_id": {"type": "integer"}
                },
                "required": ["owner", "repo", "run_id"]
            }
        ),
        Tool(
            name="cancel_workflow_run",
            description="Cancel a workflow run.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "run_id": {"type": "integer"}
                },
                "required": ["owner", "repo", "run_id"]
            }
        ),
        Tool(
            name="trigger_workflow_dispatch",
            description="Trigger a workflow dispatch event.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "workflow_id": {"type": "string", "description": "Workflow ID or filename"},
                    "ref": {"type": "string"},
                    "inputs": {"type": "object"}
                },
                "required": ["owner", "repo", "workflow_id", "ref"]
            }
        ),
        Tool(
            name="search_code",
            description="Search for code.",
            inputSchema={
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Search query"},
                    "sort": {"type": "string"},
                    "order": {"type": "string"}
                },
                "required": ["q"]
            }
        ),
        Tool(
            name="search_issues",
            description="Search for issues and pull requests.",
            inputSchema={
                "type": "object",
                "properties": {
                    "q": {"type": "string"},
                    "sort": {"type": "string"},
                    "order": {"type": "string"}
                },
                "required": ["q"]
            }
        ),
        Tool(
            name="search_repositories",
            description="Search for repositories.",
            inputSchema={
                "type": "object",
                "properties": {
                    "q": {"type": "string"},
                    "sort": {"type": "string"},
                    "order": {"type": "string"}
                },
                "required": ["q"]
            }
        ),
        Tool(
            name="search_users",
            description="Search for users.",
            inputSchema={
                "type": "object",
                "properties": {
                    "q": {"type": "string"},
                    "sort": {"type": "string"},
                    "order": {"type": "string"}
                },
                "required": ["q"]
            }
        ),
        Tool(
            name="get_user",
            description="Get user information.",
            inputSchema={
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "Username (optional, defaults to auth user)"}
                }
            }
        ),
        Tool(
            name="list_collaborators",
            description="List collaborators on a repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "affiliation": {"type": "string", "default": "all"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="add_collaborator",
            description="Add a collaborator.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "username": {"type": "string"},
                    "permission": {"type": "string", "enum": ["pull", "push", "admin", "maintain", "triage"], "default": "push"}
                },
                "required": ["owner", "repo", "username"]
            }
        ),
        Tool(
            name="remove_collaborator",
            description="Remove a collaborator.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "username": {"type": "string"}
                },
                "required": ["owner", "repo", "username"]
            }
        ),
        Tool(
            name="list_webhooks",
            description="List webhooks for a repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="create_webhook",
            description="Create a webhook.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "url": {"type": "string"},
                    "content_type": {"type": "string", "enum": ["json", "form"], "default": "json"},
                    "events": {"type": "array", "items": {"type": "string"}, "default": ["push"]},
                    "active": {"type": "boolean", "default": True},
                    "secret": {"type": "string"}
                },
                "required": ["owner", "repo", "url"]
            }
        ),
        Tool(
            name="delete_webhook",
            description="Delete a webhook.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "hook_id": {"type": "integer"}
                },
                "required": ["owner", "repo", "hook_id"]
            }
        ),
        Tool(
            name="list_gists",
            description="List gists.",
            inputSchema={
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "Optional: list user's gists"}
                }
            }
        ),
        Tool(
            name="create_gist",
            description="Create a gist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "files": {"type": "object", "description": "Map of filename to content"},
                    "public": {"type": "boolean", "default": False}
                },
                "required": ["files"]
            }
        ),
        Tool(
            name="delete_gist",
            description="Delete a gist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "gist_id": {"type": "string"}
                },
                "required": ["gist_id"]
            }
        ),
        Tool(
            name="create_release",
            description="Create a release.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "tag_name": {"type": "string"},
                    "name": {"type": "string"},
                    "body": {"type": "string"},
                    "draft": {"type": "boolean", "default": False},
                    "prerelease": {"type": "boolean", "default": False}
                },
                "required": ["owner", "repo", "tag_name"]
            }
        ),
        Tool(
            name="upload_release_asset",
            description="Upload a binary asset to a release.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "release_id": {"type": "integer"},
                    "name": {"type": "string", "description": "Filename"},
                    "label": {"type": "string", "description": "Display label"},
                    "content": {"type": "string", "description": "Base64 encoded content"},
                    "content_type": {"type": "string", "default": "application/octet-stream"}
                },
                "required": ["owner", "repo", "release_id", "name", "content"]
            }
        ),
        Tool(
            name="enable_vulnerability_alerts",
            description="Enable vulnerability alerts for a repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="disable_vulnerability_alerts",
            description="Disable vulnerability alerts for a repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="enable_automated_security_fixes",
            description="Enable automated security fixes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="disable_automated_security_fixes",
            description="Disable automated security fixes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="list_projects",
            description="List projects (classic).",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string", "description": "If provided, lists repo projects. Otherwise lists org/user projects."},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"}
                },
                "required": ["owner"]
            }
        ),
        Tool(
            name="create_project",
            description="Create a project (classic).",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string", "description": "If provided, creates repo project."},
                    "name": {"type": "string"},
                    "body": {"type": "string"}
                },
                "required": ["owner", "name"]
            }
        ),
        Tool(
            name="list_milestones",
            description="List milestones.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    "sort": {"type": "string", "default": "due_date"},
                    "direction": {"type": "string", "default": "asc"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="create_milestone",
            description="Create a milestone.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "title": {"type": "string"},
                    "state": {"type": "string", "default": "open"},
                    "description": {"type": "string"},
                    "due_on": {"type": "string", "description": "ISO 8601 timestamp"}
                },
                "required": ["owner", "repo", "title"]
            }
        ),
        Tool(
            name="list_labels",
            description="List labels.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"}
                },
                "required": ["owner", "repo"]
            }
        ),
        Tool(
            name="create_label",
            description="Create a label.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "name": {"type": "string"},
                    "color": {"type": "string", "description": "6 character hex code, without #"},
                    "description": {"type": "string"}
                },
                "required": ["owner", "repo", "name", "color"]
            }
        ),
        Tool(
            name="delete_label",
            description="Delete a label.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "name": {"type": "string"}
                },
                "required": ["owner", "repo", "name"]
            }
        )
    ]

# --- Tool Implementations ---
@app_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        append_to_global_log(f"TOOL_CALL: {name} with args: {arguments}")
        
        # Repository Tools
        if name == "list_repositories":
            params = {"sort": arguments.get("sort", "updated")}
            org = arguments.get("org")
            if org:
                params["type"] = arguments.get("visibility", "all")
                endpoint = f"/orgs/{org}/repos"
            else:
                params["visibility"] = arguments.get("visibility", "all")
                endpoint = "/user/repos"
            result = await github_client.request("GET", endpoint, params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_repository":
            data = {
                "name": arguments["name"],
                "description": arguments.get("description"),
                "private": arguments.get("private", False),
                "auto_init": arguments.get("auto_init", True)
            }
            org = arguments.get("org")
            endpoint = f"/orgs/{org}/repos" if org else "/user/repos"
            result = await github_client.request("POST", endpoint, json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "get_repository":
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}")
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "delete_repository":
            result = await github_client.request("DELETE", f"/repos/{arguments['owner']}/{arguments['repo']}")
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "Repository deleted")]
            
        elif name == "transfer_repository":
            data = {"new_owner": arguments["new_owner"]}
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/transfer", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "update_repository_archive":
            data = {"archived": arguments["archived"]}
            result = await github_client.request("PATCH", f"/repos/{arguments['owner']}/{arguments['repo']}", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        # File Tools
        elif name == "get_file_contents":
            params = {"ref": arguments["ref"]} if arguments.get("ref") else {}
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/contents/{arguments['path']}", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_or_update_file":
            data = {
                "message": arguments["message"],
                "content": arguments["content"],
                "sha": arguments.get("sha"),
                "branch": arguments.get("branch")
            }
            # Remove None values
            data = {k: v for k, v in data.items() if v is not None}
            result = await github_client.request("PUT", f"/repos/{arguments['owner']}/{arguments['repo']}/contents/{arguments['path']}", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "delete_file":
            data = {
                "message": arguments["message"],
                "sha": arguments["sha"],
                "branch": arguments.get("branch")
            }
            data = {k: v for k, v in data.items() if v is not None}
            result = await github_client.request("DELETE", f"/repos/{arguments['owner']}/{arguments['repo']}/contents/{arguments['path']}", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "File deleted")]
            
        elif name == "create_branch":
            ref = arguments["ref"]
            if not ref.startswith("refs/"):
                ref = f"refs/heads/{ref}"
            data = {"ref": ref, "sha": arguments["sha"]}
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/git/refs", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "delete_branch":
            ref = arguments["ref"]
            if ref.startswith("refs/"):
                ref = ref[5:]
            result = await github_client.request("DELETE", f"/repos/{arguments['owner']}/{arguments['repo']}/git/refs/{ref}")
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "Branch deleted")]
            
        # Organization Tools
        elif name == "get_organization":
            result = await github_client.request("GET", f"/orgs/{arguments['org']}")
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        # Commit Tools
        elif name == "list_commits":
            params = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo"]}
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/commits", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "get_commit":
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/commits/{arguments['ref']}")
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        # Issue Tools
        elif name == "list_issues":
            params = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo"]}
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/issues", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_issue":
            data = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo"]}
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/issues", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "update_issue":
            issue_number = arguments["issue_number"]
            data = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo", "issue_number"]}
            result = await github_client.request("PATCH", f"/repos/{arguments['owner']}/{arguments['repo']}/issues/{issue_number}", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_issue_comment":
            data = {"body": arguments["body"]}
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/issues/{arguments['issue_number']}/comments", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        # Pull Request Tools
        elif name == "list_pull_requests":
            params = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo"]}
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/pulls", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_pull_request":
            data = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo"]}
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/pulls", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "merge_pull_request":
            pull_number = arguments["pull_number"]
            data = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo", "pull_number"]}
            result = await github_client.request("PUT", f"/repos/{arguments['owner']}/{arguments['repo']}/pulls/{pull_number}/merge", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        # Workflow Tools
        elif name == "list_workflows":
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/actions/workflows")
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "list_workflow_runs":
            workflow_id = arguments["workflow_id"]
            params = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo", "workflow_id"]}
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/actions/workflows/{workflow_id}/runs", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "get_workflow_run":
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/actions/runs/{arguments['run_id']}")
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "cancel_workflow_run":
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/actions/runs/{arguments['run_id']}/cancel")
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "Workflow run cancelled")]
            
        elif name == "trigger_workflow_dispatch":
            workflow_id = arguments["workflow_id"]
            data = {
                "ref": arguments["ref"],
                "inputs": arguments.get("inputs", {})
            }
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/actions/workflows/{workflow_id}/dispatches", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "Workflow dispatch triggered")]
            
        # Search Tools
        elif name == "search_code":
            params = {k: v for k, v in arguments.items() if v is not None}
            result = await github_client.request("GET", "/search/code", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "search_issues":
            params = {k: v for k, v in arguments.items() if v is not None}
            result = await github_client.request("GET", "/search/issues", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "search_repositories":
            params = {k: v for k, v in arguments.items() if v is not None}
            result = await github_client.request("GET", "/search/repositories", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "search_users":
            params = {k: v for k, v in arguments.items() if v is not None}
            result = await github_client.request("GET", "/search/users", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "get_user":
            username = arguments.get("username")
            endpoint = f"/users/{username}" if username else "/user"
            result = await github_client.request("GET", endpoint)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        # Collaborator Tools
        elif name == "list_collaborators":
            params = {"affiliation": arguments.get("affiliation", "all")}
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/collaborators", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "add_collaborator":
            data = {"permission": arguments.get("permission", "push")}
            result = await github_client.request("PUT", f"/repos/{arguments['owner']}/{arguments['repo']}/collaborators/{arguments['username']}", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "remove_collaborator":
            result = await github_client.request("DELETE", f"/repos/{arguments['owner']}/{arguments['repo']}/collaborators/{arguments['username']}")
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "Collaborator removed")]
            
        # Webhook Tools
        elif name == "list_webhooks":
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/hooks")
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_webhook":
            config = {"url": arguments["url"], "content_type": arguments.get("content_type", "json")}
            if arguments.get("secret"):
                config["secret"] = arguments["secret"]
            data = {
                "config": config,
                "events": arguments.get("events", ["push"]),
                "active": arguments.get("active", True)
            }
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/hooks", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "delete_webhook":
            result = await github_client.request("DELETE", f"/repos/{arguments['owner']}/{arguments['repo']}/hooks/{arguments['hook_id']}")
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "Webhook deleted")]
            
        # Gist Tools
        elif name == "list_gists":
            username = arguments.get("username")
            endpoint = f"/users/{username}/gists" if username else "/gists"
            result = await github_client.request("GET", endpoint)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_gist":
            formatted_files = {k: {"content": v} for k, v in arguments["files"].items()}
            data = {
                "files": formatted_files,
                "description": arguments.get("description"),
                "public": arguments.get("public", False)
            }
            result = await github_client.request("POST", "/gists", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "delete_gist":
            result = await github_client.request("DELETE", f"/gists/{arguments['gist_id']}")
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "Gist deleted")]
            
        # Release Tools
        elif name == "create_release":
            data = {
                "tag_name": arguments["tag_name"],
                "name": arguments.get("name"),
                "body": arguments.get("body", ""),
                "draft": arguments.get("draft", False),
                "prerelease": arguments.get("prerelease", False)
            }
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/releases", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "upload_release_asset":
            # First get release to find upload_url
            release = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/releases/{arguments['release_id']}")
            upload_url_template = release.get("upload_url")
            if not upload_url_template:
                return [TextContent(type="text", text="Error: Release does not have an upload_url")]
            
            # Remove template params {?name,label}
            upload_url = upload_url_template.split("{")[0]
            
            # Decode content
            try:
                file_data = base64.b64decode(arguments["content"])
            except Exception:
                return [TextContent(type="text", text="Error: Invalid Base64 content")]
                
            params = {"name": arguments["name"]}
            if arguments.get("label"):
                params["label"] = arguments["label"]
                
            headers = {"Content-Type": arguments.get("content_type", "application/octet-stream")}
            
            result = await github_client.request("POST", upload_url, params=params, content=file_data, headers=headers)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        # Security Tools
        elif name == "enable_vulnerability_alerts":
            result = await github_client.request("PUT", f"/repos/{arguments['owner']}/{arguments['repo']}/vulnerability-alerts")
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "Vulnerability alerts enabled")]
            
        elif name == "disable_vulnerability_alerts":
            result = await github_client.request("DELETE", f"/repos/{arguments['owner']}/{arguments['repo']}/vulnerability-alerts")
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "Vulnerability alerts disabled")]
            
        elif name == "enable_automated_security_fixes":
            result = await github_client.request("PUT", f"/repos/{arguments['owner']}/{arguments['repo']}/automated-security-fixes")
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "Automated security fixes enabled")]
            
        elif name == "disable_automated_security_fixes":
            result = await github_client.request("DELETE", f"/repos/{arguments['owner']}/{arguments['repo']}/automated-security-fixes")
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "Automated security fixes disabled")]
            
        # Project Tools
        elif name == "list_projects":
            params = {"state": arguments.get("state", "open")}
            repo = arguments.get("repo")
            if repo:
                endpoint = f"/repos/{arguments['owner']}/{repo}/projects"
            else:
                endpoint = f"/orgs/{arguments['owner']}/projects"
            try:
                result = await github_client.request("GET", endpoint, params=params)
            except GitHubError as e:
                if e.status == 404 and not repo:
                    # Fallback to user projects
                    endpoint = f"/users/{arguments['owner']}/projects"
                    result = await github_client.request("GET", endpoint, params=params)
                else:
                    raise e
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_project":
            data = {
                "name": arguments["name"],
                "body": arguments.get("body")
            }
            repo = arguments.get("repo")
            if repo:
                endpoint = f"/repos/{arguments['owner']}/{repo}/projects"
            else:
                endpoint = f"/orgs/{arguments['owner']}/projects"
            result = await github_client.request("POST", endpoint, json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        # Milestone Tools
        elif name == "list_milestones":
            params = {
                "state": arguments.get("state", "open"),
                "sort": arguments.get("sort", "due_date"),
                "direction": arguments.get("direction", "asc")
            }
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/milestones", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_milestone":
            data = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo"]}
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/milestones", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        # Label Tools
        elif name == "list_labels":
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/labels")
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_label":
            data = {
                "name": arguments["name"],
                "color": arguments["color"],
                "description": arguments.get("description")
            }
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/labels", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "delete_label":
            result = await github_client.request("DELETE", f"/repos/{arguments['owner']}/{arguments['repo']}/labels/{arguments['name']}")
            return [TextContent(type="text", text=json.dumps(result, indent=2) if result else "Label deleted")]
            
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except GitHubError as e:
        logger.error(f"GitHub API Error in tool {name}: {e}")
        return [TextContent(type="text", text=f"GitHub API Error: {str(e)}")]
    except Exception as e:
        logger.error(f"Error executing tool {name}: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error executing tool {name}: {str(e)}")]

# --- Server Start ---
sse = SseServerTransport("/messages")

async def handle_sse(request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await app_server.run(streams[0], streams[1], app_server.create_initialization_options())

async def handle_messages(request):
    await sse.handle_post_message(request.scope, request.receive, request._send)
    return Response(status_code=202)

async def handle_health(request):
    identity = auth_manager.get_active_identity()
    return Response(
        content=json.dumps({
            "status": "healthy",
            "service": "github-mcp-server",
            "auth_status": {
                "active": identity.id if identity else None,
                "rate_limit_remaining": identity.rate_limit_remaining if identity else None
            }
        }),
        media_type="application/json"
    )

starlette_app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),
        Route("/messages", endpoint=handle_messages, methods=["POST"]),
        Route("/health", endpoint=handle_health, methods=["GET"])
    ]
)

if __name__ == "__main__":
    print("\n🚀 GitHub MCP Server (Using Official MCP Framework) Running")
    print(f"📍 Log File: {os.path.abspath(GLOBAL_LOG_FILE)}")
    uvicorn.run(starlette_app, host="0.0.0.0", port=8001)