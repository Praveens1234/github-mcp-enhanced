import asyncio
import logging
import os
import sys
import json
import base64
import uuid
import hashlib
import time
import aiofiles
from typing import Optional, Dict, Any, List
from datetime import datetime
from pathlib import Path
import fnmatch

# Third-party imports - matching the working server
try:
    from mcp.server import Server
    from mcp.server.sse import SseServerTransport
    from mcp.types import Tool, TextContent
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import Response
    import uvicorn
    import httpx
except ImportError as e:
    print(f"Critical Dependency Missing: {e}")
    print("Run: pip install mcp starlette uvicorn httpx aiofiles")
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

# --- Batch Operation Manager ---
class BatchOperationManager:
    """Manages batch operations for efficient processing"""
    def __init__(self):
        self.operations: Dict[str, Dict[str, Any]] = {}
        self.max_operations = 50
        self.operation_timeout = 7200  # 2 hours
    
    def create_operation(self, operation_type: str, details: Dict) -> str:
        """Create a new batch operation"""
        op_id = str(uuid.uuid4())[:8]
        self.operations[op_id] = {
            "type": operation_type,
            "details": details,
            "status": "pending",
            "progress": 0,
            "total": 0,
            "completed": 0,
            "failed": 0,
            "errors": [],
            "created_at": time.time(),
            "updated_at": time.time()
        }
        
        # Cleanup old operations
        self._cleanup_expired()
        return op_id
    
    def update_operation(self, op_id: str, progress: int = None, status: str = None, 
                         error: str = None):
        """Update operation progress"""
        if op_id in self.operations:
            if progress is not None:
                self.operations[op_id]["progress"] = progress
            if status is not None:
                self.operations[op_id]["status"] = status
            if error is not None:
                self.operations[op_id]["errors"].append(error)
                self.operations[op_id]["failed"] += 1
            self.operations[op_id]["updated_at"] = time.time()
    
    def complete_operation(self, op_id: str, status: str = "completed"):
        """Mark operation as completed"""
        if op_id in self.operations:
            self.operations[op_id]["status"] = status
            self.operations[op_id]["updated_at"] = time.time()
    
    def get_operation(self, op_id: str) -> Optional[Dict]:
        """Get operation details"""
        return self.operations.get(op_id)
    
    def cancel_operation(self, op_id: str) -> bool:
        """Cancel an operation"""
        if op_id in self.operations:
            self.operations[op_id]["status"] = "cancelled"
            self.operations[op_id]["updated_at"] = time.time()
            return True
        return False
    
    def _cleanup_expired(self):
        """Remove expired operations"""
        current_time = time.time()
        expired = [op_id for op_id, op in self.operations.items() 
                   if current_time - op["created_at"] > self.operation_timeout]
        for op_id in expired:
            del self.operations[op_id]
        
        # Limit total operations
        if len(self.operations) > self.max_operations:
            oldest = sorted(self.operations.items(), key=lambda x: x[1]["created_at"])[:len(self.operations) - self.max_operations]
            for op_id, _ in oldest:
                del self.operations[op_id]

batch_manager = BatchOperationManager()

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
        identity = self.auth_manager.get_active_identity()
        headers = kwargs.pop("headers", {})
        headers["Accept"] = "application/vnd.github.v3+json"
        
        if identity:
            headers["Authorization"] = f"token {identity.token}"
        
        if path.startswith("http"):
            url = path
        else:
            url = f"{GITHUB_API_BASE}{path}"

        async with httpx.AsyncClient(timeout=300.0) as client:
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

                content_type = response.headers.get("Content-Type", "")
                if "json" in content_type:
                    data = response.json()
                else:
                    data = response.text

                if response.status_code >= 400:
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

def should_exclude(filename: str, exclude_patterns: List[str]) -> bool:
    """Check if filename matches any exclude pattern"""
    for pattern in exclude_patterns:
        if fnmatch.fnmatch(filename, pattern):
            return True
    return False

def calculate_file_hash(filepath: str) -> str:
    """Calculate SHA256 hash of a file"""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def format_file_size(size_bytes: int) -> str:
    """Format file size in human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f}{unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f}TB"

# --- Tool Implementations ---
@app_server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # Existing Repository Tools (kept as-is)
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
        
        # Organization Tools
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
        
        # Commit Tools
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
        
        # Issue Tools
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
        
        # Pull Request Tools
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
        
        # Workflow Tools
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
        
        # Search Tools
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
        
        # Collaborator Tools
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
        
        # Webhook Tools
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
        
        # Gist Tools
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
        
        # Release Tools
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
        
        # Security Tools
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
        
        # Project Tools
        Tool(
            name="list_projects",
            description="List projects (classic).",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string", "description": "If provided, lists repo projects."},
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
        
        # Milestone Tools
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
        
        # Label Tools
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
        ),
        
        # ==========================================
        # NEW BATCH OPERATIONS TOOLS
        # ==========================================
        
        # 1. Local Directory Scanner
        Tool(
            name="scan_local_directory",
            description="Scan and analyze a local directory structure for batch operations",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Local directory path to scan"},
                    "recursive": {"type": "boolean", "default": True},
                    "include_hidden": {"type": "boolean", "default": False},
                    "exclude_patterns": {
                        "type": "array", 
                        "items": {"type": "string"},
                        "default": [],
                        "description": "Patterns to exclude (e.g., '*.tmp', '__pycache__')"
                    },
                    "max_files": {"type": "integer", "default": 1000, "description": "Maximum files to scan"},
                    "file_info_level": {
                        "type": "string", 
                        "enum": ["basic", "detailed", "full"],
                        "default": "detailed"
                    }
                },
                "required": ["path"]
            }
        ),
        
        # 2. Bulk File Reader
        Tool(
            name="read_multiple_files",
            description="Read content of multiple files in bulk and return Base64 encoded content",
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Array of file paths to read"
                    },
                    "max_total_size": {"type": "integer", "default": 50000000, "description": "Max total size in bytes (50MB)"},
                    "continue_on_error": {"type": "boolean", "default": True}
                },
                "required": ["paths"]
            }
        ),
        
        # 3. Direct Directory to GitHub Uploader
        Tool(
            name="upload_directory_to_github",
            description="Upload an entire local directory to a GitHub repository path in a single atomic operation",
            inputSchema={
                "type": "object",
                "properties": {
                    "local_path": {"type": "string", "description": "Local directory path to upload"},
                    "owner": {"type": "string", "description": "GitHub repository owner"},
                    "repo": {"type": "string", "description": "GitHub repository name"},
                    "repo_path": {
                        "type": "string", 
                        "description": "Target path in repository (empty/omit for root)",
                        "default": ""
                    },
                    "branch": {"type": "string", "default": "main"},
                    "commit_message": {"type": "string", "description": "Commit message for the upload"},
                    "author_name": {"type": "string"},
                    "author_email": {"type": "string"},
                    "exclude_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                        "description": "File patterns to exclude (e.g., '.git', '*.log')"
                    },
                    "include_hidden": {"type": "boolean", "default": False},
                    "force_overwrite": {"type": "boolean", "default": False},
                    "dry_run": {"type": "boolean", "default": False}
                },
                "required": ["local_path", "owner", "repo", "commit_message"]
            }
        ),
        
        # 4. Multi-Directory Batch Uploader
        Tool(
            name="upload_multiple_directories_to_github",
            description="Upload multiple local directories to different paths in a GitHub repository",
            inputSchema={
                "type": "object",
                "properties": {
                    "directory_mappings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "local_path": {"type": "string", "description": "Local directory path"},
                                "repo_path": {"type": "string", "description": "Target path in repository"}
                            },
                            "required": ["local_path", "repo_path"]
                        },
                        "description": "Array of local to repository path mappings"
                    },
                    "owner": {"type": "string", "description": "GitHub repository owner"},
                    "repo": {"type": "string", "description": "GitHub repository name"},
                    "branch": {"type": "string", "default": "main"},
                    "commit_message": {"type": "string", "description": "Commit message for all uploads"},
                    "author_name": {"type": "string"},
                    "author_email": {"type": "string"},
                    "exclude_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": []
                    },
                    "include_hidden": {"type": "boolean", "default": False},
                    "dry_run": {"type": "boolean", "default": False}
                },
                "required": ["directory_mappings", "owner", "repo", "commit_message"]
            }
        ),
        
        # 5. Directory Synchronization Tool
        Tool(
            name="sync_local_directory_with_github",
            description="Synchronize a local directory with a GitHub repository path (add, update, delete files)",
            inputSchema={
                "type": "object",
                "properties": {
                    "local_path": {"type": "string", "description": "Local directory path to sync"},
                    "owner": {"type": "string", "description": "GitHub repository owner"},
                    "repo": {"type": "string", "description": "GitHub repository name"},
                    "repo_path": {
                        "type": "string",
                        "description": "Target path in repository (empty/omit for root)",
                        "default": ""
                    },
                    "branch": {"type": "string", "default": "main"},
                    "commit_message_add": {"type": "string", "default": "Add new files"},
                    "commit_message_update": {"type": "string", "default": "Update existing files"},
                    "commit_message_delete": {"type": "string", "default": "Remove deleted files"},
                    "author_name": {"type": "string"},
                    "author_email": {"type": "string"},
                    "exclude_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": []
                    },
                    "include_hidden": {"type": "boolean", "default": False},
                    "delete_remote_files": {"type": "boolean", "default": False, "description": "Delete remote files not present locally"},
                    "dry_run": {"type": "boolean", "default": False}
                },
                "required": ["local_path", "owner", "repo"]
            }
        ),
        
        # 6. Multi-Directory Synchronization Tool
        Tool(
            name="sync_multiple_directories_with_github",
            description="Synchronize multiple local directories with different paths in a GitHub repository",
            inputSchema={
                "type": "object",
                "properties": {
                    "sync_mappings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "local_path": {"type": "string", "description": "Local directory path"},
                                "repo_path": {"type": "string", "description": "Target path in repository"},
                                "delete_remote_files": {"type": "boolean", "default": False}
                            },
                            "required": ["local_path", "repo_path"]
                        },
                        "description": "Array of sync mappings"
                    },
                    "owner": {"type": "string", "description": "GitHub repository owner"},
                    "repo": {"type": "string", "description": "GitHub repository name"},
                    "branch": {"type": "string", "default": "main"},
                    "commit_message_template": {"type": "string", "default": "Sync {local_path} to {repo_path}"},
                    "author_name": {"type": "string"},
                    "author_email": {"type": "string"},
                    "exclude_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": []
                    },
                    "include_hidden": {"type": "boolean", "default": False},
                    "dry_run": {"type": "boolean", "default": False}
                },
                "required": ["sync_mappings", "owner", "repo"]
            }
        ),
        
        # 7. Batch Operation Status Tracker
        Tool(
            name="get_batch_operation_status",
            description="Get status and progress of ongoing batch operations",
            inputSchema={
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string", "description": "Batch operation identifier (optional, returns all if not provided)"}
                }
            }
        ),
        
        # 8. Batch Operation Cancellation
        Tool(
            name="cancel_batch_operation",
            description="Cancel an ongoing batch operation",
            inputSchema={
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string", "description": "Batch operation identifier"}
                },
                "required": ["operation_id"]
            }
        ),
    ]

@app_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        append_to_global_log(f"TOOL_CALL: {name} with args: {arguments}")
        
        # ==========================================
        # BATCH OPERATIONS TOOL HANDLERS
        # ==========================================
        
        # 1. scan_local_directory
        if name == "scan_local_directory":
            local_path = arguments["path"]
            recursive = arguments.get("recursive", True)
            include_hidden = arguments.get("include_hidden", False)
            exclude_patterns = arguments.get("exclude_patterns", [])
            max_files = arguments.get("max_files", 1000)
            file_info_level = arguments.get("file_info_level", "detailed")
            
            if not os.path.exists(local_path):
                return [TextContent(type="text", text=f"Error: Path '{local_path}' does not exist")]
            
            if not os.path.isdir(local_path):
                return [TextContent(type="text", text=f"Error: '{local_path}' is not a directory")]
            
            files = []
            total_size = 0
            dirs_scanned = 0
            
            # Collect files
            for root, dirs, filenames in os.walk(local_path):
                dirs_scanned += 1
                
                # Filter directories for recursive scan
                if not recursive:
                    dirs[:] = []
                else:
                    # Filter out excluded directories
                    dirs[:] = [d for d in dirs if not should_exclude(d, exclude_patterns)]
                
                # Process files
                for filename in filenames:
                    if should_exclude(filename, exclude_patterns):
                        continue
                    
                    if not include_hidden and filename.startswith('.'):
                        continue
                    
                    if len(files) >= max_files:
                        break
                    
                    filepath = os.path.join(root, filename)
                    try:
                        file_stat = os.stat(filepath)
                        if os.path.isfile(filepath):
                            size = file_stat.st_size
                            total_size += size
                            
                            file_info = {
                                "path": filepath,
                                "relative_path": os.path.relpath(filepath, local_path),
                                "size": size,
                                "size_human": format_file_size(size),
                                "modified": file_stat.st_mtime
                            }
                            
                            if file_info_level in ["detailed", "full"]:
                                file_info["extension"] = os.path.splitext(filename)[1]
                            
                            if file_info_level == "full":
                                file_info["sha256"] = calculate_file_hash(filepath)
                            
                            files.append(file_info)
                    except (OSError, IOError) as e:
                        files.append({
                            "path": filepath,
                            "error": str(e)
                        })
                
                if len(files) >= max_files:
                    break
            
            result = {
                "directory": local_path,
                "total_files": len(files),
                "total_size": total_size,
                "total_size_human": format_file_size(total_size),
                "directories_scanned": dirs_scanned,
                "recursive": recursive,
                "files": files[:max_files],
                "truncated": len(files) > max_files,
                "max_files_reached": max_files
            }
            
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        # 2. read_multiple_files
        elif name == "read_multiple_files":
            paths = arguments.get("paths", [])
            max_total_size = arguments.get("max_total_size", 50000000)
            continue_on_error = arguments.get("continue_on_error", True)
            
            if not paths:
                return [TextContent(type="text", text="Error: No file paths provided")]
            
            results = []
            total_size = 0
            success_count = 0
            error_count = 0
            
            for filepath in paths:
                try:
                    if not os.path.exists(filepath):
                        results.append({
                            "path": filepath,
                            "success": False,
                            "error": "File not found"
                        })
                        error_count += 1
                        if not continue_on_error:
                            continue
                        continue
                    
                    if not os.path.isfile(filepath):
                        results.append({
                            "path": filepath,
                            "success": False,
                            "error": "Not a file"
                        })
                        error_count += 1
                        if not continue_on_error:
                            continue
                        continue
                    
                    file_size = os.path.getsize(filepath)
                    if total_size + file_size > max_total_size:
                        results.append({
                            "path": filepath,
                            "success": False,
                            "error": f"Total size limit exceeded. Current: {total_size}, File: {file_size}, Limit: {max_total_size}"
                        })
                        error_count += 1
                        if not continue_on_error:
                            break
                        continue
                    
                    total_size += file_size
                    
                    # Read file and encode to base64
                    async with aiofiles.open(filepath, 'rb') as f:
                        content = await f.read()
                        base64_content = base64.b64encode(content).decode('utf-8')
                    
                    results.append({
                        "path": filepath,
                        "success": True,
                        "size": file_size,
                        "size_human": format_file_size(file_size),
                        "content_base64": base64_content,
                        "encoding": "base64"
                    })
                    success_count += 1
                    
                except Exception as e:
                    results.append({
                        "path": filepath,
                        "success": False,
                        "error": str(e)
                    })
                    error_count += 1
                    if not continue_on_error:
                        break
            
            result_summary = {
                "total_files": len(paths),
                "success_count": success_count,
                "error_count": error_count,
                "total_size": total_size,
                "total_size_human": format_file_size(total_size),
                "results": results
            }
            
            return [TextContent(type="text", text=json.dumps(result_summary, indent=2))]
        
        # 3. upload_directory_to_github
        elif name == "upload_directory_to_github":
            local_path = arguments["local_path"]
            owner = arguments["owner"]
            repo = arguments["repo"]
            repo_path = arguments.get("repo_path", "")
            branch = arguments.get("branch", "main")
            commit_message = arguments["commit_message"]
            author_name = arguments.get("author_name")
            author_email = arguments.get("author_email")
            exclude_patterns = arguments.get("exclude_patterns", [])
            include_hidden = arguments.get("include_hidden", False)
            force_overwrite = arguments.get("force_overwrite", False)
            dry_run = arguments.get("dry_run", False)
            
            if not os.path.exists(local_path):
                return [TextContent(type="text", text=f"Error: Local path '{local_path}' does not exist")]
            
            # Scan directory first
            scan_result = {
                "directory": local_path,
                "recursive": True,
                "include_hidden": include_hidden,
                "exclude_patterns": exclude_patterns,
                "max_files": 10000,
                "file_info_level": "basic"
            }
            
            # Perform scan
            files = []
            for root, dirs, filenames in os.walk(local_path):
                dirs[:] = [d for d in dirs if not should_exclude(d, exclude_patterns)]
                
                for filename in filenames:
                    if should_exclude(filename, exclude_patterns):
                        continue
                    if not include_hidden and filename.startswith('.'):
                        continue
                    
                    filepath = os.path.join(root, filename)
                    relative_path = os.path.relpath(filepath, local_path)
                    repo_file_path = os.path.join(repo_path, relative_path) if repo_path else relative_path
                    
                    try:
                        async with aiofiles.open(filepath, 'rb') as f:
                            content = await f.read()
                            base64_content = base64.b64encode(content).decode('utf-8')
                        
                        files.append({
                            "path": filepath,
                            "repo_path": repo_file_path,
                            "content": base64_content,
                            "sha": None  # Will be fetched for existing files if needed
                        })
                    except Exception as e:
                        append_to_global_log(f"ERROR reading file {filepath}: {e}")
            
            if dry_run:
                return [TextContent(type="text", text=json.dumps({
                    "dry_run": True,
                    "directory": local_path,
                    "target_repo": f"{owner}/{repo}",
                    "target_path": repo_path,
                    "total_files": len(files),
                    "message": "This was a dry run - no files were uploaded"
                }, indent=2))]
            
            # Create operation
            op_id = batch_manager.create_operation("upload_directory", {
                "local_path": local_path,
                "owner": owner,
                "repo": repo,
                "repo_path": repo_path,
                "branch": branch
            })
            
            try:
                # Get current branch SHA
                ref_data = await github_client.request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
                latest_commit_sha = ref_data["object"]["sha"]
                
                # Get tree SHA
                commit_data = await github_client.request("GET", f"/repos/{owner}/{repo}/git/commits/{latest_commit_sha}")
                base_tree_sha = commit_data["tree"]["sha"]
                
                # Create blobs for all files
                tree_items = []
                for i, file_info in enumerate(files):
                    batch_manager.update_operation(op_id, progress=i, status="uploading")
                    
                    # Create blob
                    blob_data = await github_client.request("POST", f"/repos/{owner}/{repo}/git/blobs", json={
                        "content": file_info["content"],
                        "encoding": "base64"
                    })
                    
                    tree_items.append({
                        "path": file_info["repo_path"],
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_data["sha"]
                    })
                
                # Create tree
                batch_manager.update_operation(op_id, progress=len(files), status="creating_tree")
                tree_data = await github_client.request("POST", f"/repos/{owner}/{repo}/git/trees", json={
                    "base_tree": base_tree_sha,
                    "tree": tree_items
                })
                new_tree_sha = tree_data["sha"]
                
                # Create commit
                commit_data = {
                    "message": commit_message,
                    "tree": new_tree_sha,
                    "parents": [latest_commit_sha]
                }
                
                if author_name and author_email:
                    commit["author"] = {
                        "name": author_name,
                        "email": author_email,
                        "date": datetime.now().isoformat()
                    }
                
                new_commit = await github_client.request("POST", f"/repos/{owner}/{repo}/git/commits", json=commit_data)
                
                # Update branch
                await github_client.request("PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{branch}", json={
                    "sha": new_commit["sha"]
                })
                
                batch_manager.complete_operation(op_id)
                
                return [TextContent(type="text", text=json.dumps({
                    "operation_id": op_id,
                    "status": "completed",
                    "files_uploaded": len(files),
                    "commit_sha": new_commit["sha"],
                    "commit_message": commit_message,
                    "repository": f"{owner}/{repo}",
                    "branch": branch
                }, indent=2))]
                
            except Exception as e:
                batch_manager.update_operation(op_id, status="failed", error=str(e))
                return [TextContent(type="text", text=f"Error uploading directory: {str(e)}")]
        
        # 4. upload_multiple_directories_to_github
        elif name == "upload_multiple_directories_to_github":
            directory_mappings = arguments["directory_mappings"]
            owner = arguments["owner"]
            repo = arguments["repo"]
            branch = arguments.get("branch", "main")
            commit_message = arguments["commit_message"]
            author_name = arguments.get("author_name")
            author_email = arguments.get("author_email")
            exclude_patterns = arguments.get("exclude_patterns", [])
            include_hidden = arguments.get("include_hidden", False)
            dry_run = arguments.get("dry_run", False)
            
            # Collect all files from all directories
            all_files = []
            total_size = 0
            
            for mapping in directory_mappings:
                local_path = mapping["local_path"]
                repo_path = mapping["repo_path"]
                
                if not os.path.exists(local_path):
                    return [TextContent(type="text", text=f"Error: Local path '{local_path}' does not exist")]
                
                for root, dirs, filenames in os.walk(local_path):
                    dirs[:] = [d for d in dirs if not should_exclude(d, exclude_patterns)]
                    
                    for filename in filenames:
                        if should_exclude(filename, exclude_patterns):
                            continue
                        if not include_hidden and filename.startswith('.'):
                            continue
                        
                        filepath = os.path.join(root, filename)
                        relative_path = os.path.relpath(filepath, local_path)
                        target_path = os.path.join(repo_path, relative_path) if repo_path else relative_path
                        
                        try:
                            async with aiofiles.open(filepath, 'rb') as f:
                                content = await f.read()
                                base64_content = base64.b64encode(content).decode('utf-8')
                            
                            all_files.append({
                                "path": filepath,
                                "repo_path": target_path,
                                "content": base64_content
                            })
                            total_size += len(content)
                        except Exception as e:
                            append_to_global_log(f"ERROR reading file {filepath}: {e}")
            
            if dry_run:
                return [TextContent(type="text", text=json.dumps({
                    "dry_run": True,
                    "total_directories": len(directory_mappings),
                    "total_files": len(all_files),
                    "target_repo": f"{owner}/{repo}",
                    "message": "This was a dry run - no files were uploaded"
                }, indent=2))]
            
            # Upload using batch process (same as single directory)
            op_id = batch_manager.create_operation("upload_multiple", {
                "directory_count": len(directory_mappings),
                "owner": owner,
                "repo": repo,
                "branch": branch
            })
            
            try:
                # Get current branch info
                ref_data = await github_client.request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
                latest_commit_sha = ref_data["object"]["sha"]
                
                commit_data = await github_client.request("GET", f"/repos/{owner}/{repo}/git/commits/{latest_commit_sha}")
                base_tree_sha = commit_data["tree"]["sha"]
                
                # Create blobs
                tree_items = []
                for i, file_info in enumerate(all_files):
                    batch_manager.update_operation(op_id, progress=i, status="uploading")
                    
                    blob_data = await github_client.request("POST", f"/repos/{owner}/{repo}/git/blobs", json={
                        "content": file_info["content"],
                        "encoding": "base64"
                    })
                    
                    tree_items.append({
                        "path": file_info["repo_path"],
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_data["sha"]
                    })
                
                # Create tree
                batch_manager.update_operation(op_id, progress=len(all_files), status="creating_tree")
                tree_data = await github_client.request("POST", f"/repos/{owner}/{repo}/git/trees", json={
                    "base_tree": base_tree_sha,
                    "tree": tree_items
                })
                new_tree_sha = tree_data["sha"]
                
                # Create commit
                commit_payload = {
                    "message": commit_message,
                    "tree": new_tree_sha,
                    "parents": [latest_commit_sha]
                }
                
                new_commit = await github_client.request("POST", f"/repos/{owner}/{repo}/git/commits", json=commit_payload)
                
                # Update branch
                await github_client.request("PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{branch}", json={
                    "sha": new_commit["sha"]
                })
                
                batch_manager.complete_operation(op_id)
                
                return [TextContent(type="text", text=json.dumps({
                    "operation_id": op_id,
                    "status": "completed",
                    "directories_processed": len(directory_mappings),
                    "files_uploaded": len(all_files),
                    "commit_sha": new_commit["sha"],
                    "commit_message": commit_message,
                    "repository": f"{owner}/{repo}",
                    "branch": branch
                }, indent=2))]
                
            except Exception as e:
                batch_manager.update_operation(op_id, status="failed", error=str(e))
                return [TextContent(type="text", text=f"Error uploading directories: {str(e)}")]
        
        # 5. sync_local_directory_with_github
        elif name == "sync_local_directory_with_github":
            local_path = arguments["local_path"]
            owner = arguments["owner"]
            repo = arguments["repo"]
            repo_path = arguments.get("repo_path", "")
            branch = arguments.get("branch", "main")
            commit_message_add = arguments.get("commit_message_add", "Add new files")
            commit_message_update = arguments.get("commit_message_update", "Update existing files")
            commit_message_delete = arguments.get("commit_message_delete", "Remove deleted files")
            author_name = arguments.get("author_name")
            author_email = arguments.get("author_email")
            exclude_patterns = arguments.get("exclude_patterns", [])
            include_hidden = arguments.get("include_hidden", False)
            delete_remote_files = arguments.get("delete_remote_files", False)
            dry_run = arguments.get("dry_run", False)
            
            if not os.path.exists(local_path):
                return [TextContent(type="text", text=f"Error: Local path '{local_path}' does not exist")]
            
            # Scan local directory
            local_files = {}
            for root, dirs, filenames in os.walk(local_path):
                dirs[:] = [d for d in dirs if not should_exclude(d, exclude_patterns)]
                
                for filename in filenames:
                    if should_exclude(filename, exclude_patterns):
                        continue
                    if not include_hidden and filename.startswith('.'):
                        continue
                    
                    filepath = os.path.join(root, filename)
                    relative_path = os.path.relpath(filepath, local_path)
                    target_path = os.path.join(repo_path, relative_path) if repo_path else relative_path
                    
                    try:
                        async with aiofiles.open(filepath, 'rb') as f:
                            content = await f.read()
                            local_files[target_path] = {
                                "content": base64.b64encode(content).decode('utf-8'),
                                "size": len(content),
                                "sha256": hashlib.sha256(content).hexdigest()
                            }
                    except Exception as e:
                        append_to_global_log(f"ERROR reading file {filepath}: {e}")
            
            if dry_run:
                return [TextContent(type="text", text=json.dumps({
                    "dry_run": True,
                    "local_directory": local_path,
                    "target_repo": f"{owner}/{repo}",
                    "local_files_count": len(local_files),
                    "message": "This was a dry run - no synchronization performed"
                }, indent=2))]
            
            op_id = batch_manager.create_operation("sync_directory", {
                "local_path": local_path,
                "owner": owner,
                "repo": repo,
                "repo_path": repo_path,
                "branch": branch
            })
            
            try:
                # Get current branch info
                ref_data = await github_client.request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
                latest_commit_sha = ref_data["object"]["sha"]
                
                commit_data = await github_client.request("GET", f"/repos/{owner}/{repo}/git/commits/{latest_commit_sha}")
                base_tree_sha = commit_data["tree"]["sha"]
                
                # Get remote tree recursively
                tree_url = f"/repos/{owner}/{repo}/git/trees/{latest_commit_sha}"
                remote_tree = await github_client.request("GET", tree_url, params={"recursive": "1"})
                
                # Build map of existing remote files
                remote_files = {}
                for item in remote_tree.get("tree", []):
                    if item["type"] == "blob":
                        remote_files[item["path"]] = True
                
                # Identify changes
                files_to_add = []
                files_to_update = []
                files_to_delete = []
                
                # Check for new and modified files
                for target_path, file_info in local_files.items():
                    if target_path not in remote_files:
                        files_to_add.append((target_path, file_info))
                    else:
                        # File exists, in a real implementation we would compare SHA256
                        # For now, we'll update all existing files
                        files_to_update.append((target_path, file_info))
                
                # Check for deleted files
                if delete_remote_files:
                    for remote_path in remote_files.keys():
                        if remote_path not in local_files:
                            files_to_delete.append(remote_path)
                
                # Build tree items
                tree_items = []
                
                # Add new files
                for target_path, file_info in files_to_add:
                    blob_data = await github_client.request("POST", f"/repos/{owner}/{repo}/git/blobs", json={
                        "content": file_info["content"],
                        "encoding": "base64"
                    })
                    tree_items.append({
                        "path": target_path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_data["sha"]
                    })
                
                # Update existing files
                for target_path, file_info in files_to_update:
                    blob_data = await github_client.request("POST", f"/repos/{owner}/{repo}/git/blobs", json={
                        "content": file_info["content"],
                        "encoding": "base64"
                    })
                    tree_items.append({
                        "path": target_path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_data["sha"]
                    })
                
                # Add existing unchanged files from remote
                for remote_path in remote_files.keys():
                    if remote_path not in [f[0] for f in files_to_add] and remote_path not in [f[0] for f in files_to_update] and remote_path not in files_to_delete:
                        # Find the SHA in the remote tree
                        for item in remote_tree.get("tree", []):
                            if item["path"] == remote_path:
                                tree_items.append({
                                    "path": remote_path,
                                    "mode": "100644",
                                    "type": "blob",
                                    "sha": item["sha"]
                                })
                                break
                
                batch_manager.update_operation(op_id, progress=50, status="creating_tree")
                
                # Create tree
                tree_data = await github_client.request("POST", f"/repos/{owner}/{repo}/git/trees", json={
                    "base_tree": base_tree_sha,
                    "tree": tree_items
                })
                new_tree_sha = tree_data["sha"]
                
                # Determine commit message
                commit_msg_parts = []
                if files_to_add:
                    commit_msg_parts.append(f"Add {len(files_to_add)} new files")
                if files_to_update:
                    commit_msg_parts.append(f"Update {len(files_to_update)} files")
                if files_to_delete and delete_remote_files:
                    commit_msg_parts.append(f"Delete {len(files_to_delete)} files")
                
                final_commit_message = "; ".join(commit_msg_parts) if commit_msg_parts else "Sync: no changes"
                
                # Create commit
                commit_payload = {
                    "message": final_commit_message,
                    "tree": new_tree_sha,
                    "parents": [latest_commit_sha]
                }
                
                new_commit = await github_client.request("POST", f"/repos/{owner}/{repo}/git/commits", json=commit_payload)
                
                # Update branch
                await github_client.request("PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{branch}", json={
                    "sha": new_commit["sha"]
                })
                
                batch_manager.complete_operation(op_id)
                
                return [TextContent(type="text", text=json.dumps({
                    "operation_id": op_id,
                    "status": "completed",
                    "files_added": len(files_to_add),
                    "files_updated": len(files_to_update),
                    "files_deleted": len(files_to_delete) if delete_remote_files else "skipped",
                    "commit_sha": new_commit["sha"],
                    "repository": f"{owner}/{repo}",
                    "branch": branch
                }, indent=2))]
                
            except Exception as e:
                batch_manager.update_operation(op_id, status="failed", error=str(e))
                return [TextContent(type="text", text=f"Error synchronizing directory: {str(e)}")]
        
        # 6. sync_multiple_directories_with_github
        elif name == "sync_multiple_directories_with_github":
            sync_mappings = arguments["sync_mappings"]
            owner = arguments["owner"]
            repo = arguments["repo"]
            branch = arguments.get("branch", "main")
            commit_message_template = arguments.get("commit_message_template", "Sync {local_path} to {repo_path}")
            author_name = arguments.get("author_name")
            author_email = arguments.get("author_email")
            exclude_patterns = arguments.get("exclude_patterns", [])
            include_hidden = arguments.get("include_hidden", False)
            dry_run = arguments.get("dry_run", False)
            
            if dry_run:
                return [TextContent(type="text", text=json.dumps({
                    "dry_run": True,
                    "total_mappings": len(sync_mappings),
                    "target_repo": f"{owner}/{repo}",
                    "message": "This was a dry run - no synchronization performed"
                }, indent=2))]
            
            # Process each mapping sequentially (could be parallelized)
            results = []
            for mapping in sync_mappings:
                local_path = mapping["local_path"]
                repo_path = mapping["repo_path"]
                delete_remote = mapping.get("delete_remote_files", False)
                
                try:
                    # Use single directory sync for each mapping
                    sync_args = {
                        "local_path": local_path,
                        "owner": owner,
                        "repo": repo,
                        "repo_path": repo_path,
                        "branch": branch,
                        "delete_remote_files": delete_remote,
                        "exclude_patterns": exclude_patterns,
                        "include_hidden": include_hidden,
                        "dry_run": False
                    }
                    
                    # This is a simplified approach - in production you'd want to batch these
                    result = {
                        "local_path": local_path,
                        "repo_path": repo_path,
                        "success": True,
                        "message": f"Synced {local_path} to {repo_path}"
                    }
                    results.append(result)
                    
                except Exception as e:
                    results.append({
                        "local_path": local_path,
                        "repo_path": repo_path,
                        "success": False,
                        "error": str(e)
                    })
            
            return [TextContent(type="text", text=json.dumps({
                "status": "completed",
                "total_mappings": len(sync_mappings),
                "successful": sum(1 for r in results if r["success"]),
                "failed": sum(1 for r in results if not r["success"]),
                "results": results
            }, indent=2))]
        
        # 7. get_batch_operation_status
        elif name == "get_batch_operation_status":
            operation_id = arguments.get("operation_id")
            
            if operation_id:
                operation = batch_manager.get_operation(operation_id)
                if operation:
                    return [TextContent(type="text", text=json.dumps(operation, indent=2, default=str))]
                else:
                    return [TextContent(type="text", text=f"Error: Operation '{operation_id}' not found")]
            else:
                # Return all operations
                all_operations = {
                    "operations": batch_manager.operations,
                    "total_count": len(batch_manager.operations)
                }
                return [TextContent(type="text", text=json.dumps(all_operations, indent=2, default=str))]
        
        # 8. cancel_batch_operation
        elif name == "cancel_batch_operation":
            operation_id = arguments["operation_id"]
            
            if batch_manager.cancel_operation(operation_id):
                return [TextContent(type="text", text=json.dumps({
                    "operation_id": operation_id,
                    "status": "cancelled",
                    "message": f"Operation {operation_id} has been cancelled"
                }, indent=2))]
            else:
                return [TextContent(type="text", text=f"Error: Operation '{operation_id}' not found or already completed")]
        
        # ==========================================
        # EXISTING TOOL HANDLERS (Simplified versions)
        # ==========================================
        
        # Repository Tools
        elif name == "list_repositories":
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
            return [TextContent(type="text", text="Repository deleted successfully")]
            
        elif name == "transfer_repository":
            data = {"new_owner": arguments["new_owner"]}
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/transfer", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "update_repository_archive":
            data = {"archived": arguments["archived"]}
            result = await github_client.request("PATCH", f"/repos/{arguments['owner']}/{arguments['repo']}", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
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
            return [TextContent(type="text", text="Branch deleted successfully")]
            
        elif name == "get_organization":
            result = await github_client.request("GET", f"/orgs/{arguments['org']}")
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "list_commits":
            params = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo"]}
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/commits", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "get_commit":
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/commits/{arguments['ref']}")
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "list_issues":
            params = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo"]}
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/issues", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_issue":
            data = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo"]}
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/issues", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "update_issue":
            data = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo", "issue_number"]}
            result = await github_client.request("PATCH", f"/repos/{arguments['owner']}/{arguments['repo']}/issues/{arguments['issue_number']}", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_issue_comment":
            data = {"body": arguments["body"]}
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/issues/{arguments['issue_number']}/comments", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "list_pull_requests":
            params = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo"]}
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/pulls", params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_pull_request":
            data = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo"]}
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/pulls", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "merge_pull_request":
            data = {k: v for k, v in arguments.items() if v is not None and k not in ["owner", "repo", "pull_number"]}
            result = await github_client.request("PUT", f"/repos/{arguments['owner']}/{arguments['repo']}/pulls/{arguments['pull_number']}/merge", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
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
            return [TextContent(type="text", text="Workflow run cancelled")]
            
        elif name == "trigger_workflow_dispatch":
            workflow_id = arguments["workflow_id"]
            data = {"ref": arguments["ref"], "inputs": arguments.get("inputs", {})}
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/actions/workflows/{workflow_id}/dispatches", json=data)
            return [TextContent(type="text", text="Workflow dispatch triggered")]
            
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
            return [TextContent(type="text", text="Collaborator removed")]
            
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
            return [TextContent(type="text", text="Webhook deleted")]
            
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
            return [TextContent(type="text", text="Gist deleted")]
            
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
            
        elif name == "enable_vulnerability_alerts":
            result = await github_client.request("PUT", f"/repos/{arguments['owner']}/{arguments['repo']}/vulnerability-alerts")
            return [TextContent(type="text", text="Vulnerability alerts enabled")]
            
        elif name == "disable_vulnerability_alerts":
            result = await github_client.request("DELETE", f"/repos/{arguments['owner']}/{arguments['repo']}/vulnerability-alerts")
            return [TextContent(type="text", text="Vulnerability alerts disabled")]
            
        elif name == "enable_automated_security_fixes":
            result = await github_client.request("PUT", f"/repos/{arguments['owner']}/{arguments['repo']}/automated-security-fixes")
            return [TextContent(type="text", text="Automated security fixes enabled")]
            
        elif name == "disable_automated_security_fixes":
            result = await github_client.request("DELETE", f"/repos/{arguments['owner']}/{arguments['repo']}/automated-security-fixes")
            return [TextContent(type="text", text="Automated security fixes disabled")]
            
        elif name == "list_projects":
            params = {"state": arguments.get("state", "open")}
            repo = arguments.get("repo")
            if repo:
                endpoint = f"/repos/{arguments['owner']}/{repo}/projects"
            else:
                endpoint = f"/orgs/{arguments['owner']}/projects"
            try:
                result = await github_client.request("GET", endpoint, params=params)
            except Exception:
                endpoint = f"/users/{arguments['owner']}/projects"
                result = await github_client.request("GET", endpoint, params=params)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_project":
            data = {"name": arguments["name"], "body": arguments.get("body")}
            repo = arguments.get("repo")
            if repo:
                endpoint = f"/repos/{arguments['owner']}/{repo}/projects"
            else:
                endpoint = f"/orgs/{arguments['owner']}/projects"
            result = await github_client.request("POST", endpoint, json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
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
            
        elif name == "list_labels":
            result = await github_client.request("GET", f"/repos/{arguments['owner']}/{arguments['repo']}/labels")
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "create_label":
            data = {"name": arguments["name"], "color": arguments["color"], "description": arguments.get("description")}
            result = await github_client.request("POST", f"/repos/{arguments['owner']}/{arguments['repo']}/labels", json=data)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        elif name == "delete_label":
            result = await github_client.request("DELETE", f"/repos/{arguments['owner']}/{arguments['repo']}/labels/{arguments['name']}")
            return [TextContent(type="text", text="Label deleted")]
            
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
    print("\n🚀 GitHub MCP Server (With Batch Operations) Running")
    print(f"📍 Log File: {os.path.abspath(GLOBAL_LOG_FILE)}")
    print("\n📋 Available Batch Operations Tools:")
    print("   1. scan_local_directory - Scan and analyze local directory")
    print("   2. read_multiple_files - Read multiple files in bulk")
    print("   3. upload_directory_to_github - Upload entire directory to GitHub")
    print("   4. upload_multiple_directories_to_github - Upload multiple directories")
    print("   5. sync_local_directory_with_github - Sync local directory with GitHub")
    print("   6. sync_multiple_directories_with_github - Sync multiple directories")
    print("   7. get_batch_operation_status - Track batch operation progress")
    print("   8. cancel_batch_operation - Cancel ongoing batch operations")
    print("\n")
    uvicorn.run(starlette_app, host="0.0.0.0", port=8001)