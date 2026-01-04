import asyncio
import aiohttp
from aiohttp import web
import logging
import json
import os
import sys
import base64
import datetime
import enum
import argparse
import signal
import uuid
from typing import Dict, Any, List, Optional, Callable, Awaitable, Union, cast
from functools import wraps

# --- Configuration & Logging ---

LOG_FILE = "server.log"
GITHUB_API_BASE = "https://api.github.com"
MCP_VERSION = "0.1.0"

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE)
        ]
    )

logger = logging.getLogger("mcp-server")

# --- Models & Exceptions ---

class MCPError(Exception):
    pass

class ToolError(MCPError):
    pass

class GitHubError(MCPError):
    def __init__(self, status: int, message: str, data: Any = None):
        super().__init__(f"GitHub API Error {status}: {message}")
        self.status = status
        self.data = data

class AuthType(enum.Enum):
    PAT = "pat"
    OAUTH = "oauth"
    APP = "app"

# --- Authentication Manager ---

class AuthIdentity:
    def __init__(self, id: str, type: AuthType, token: str, metadata: Dict[str, Any] = None):
        self.id = id
        self.type = type
        self.token = token
        self.metadata = metadata or {}
        self.rate_limit_remaining = None
        self.rate_limit_reset = None

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type.value,
            "metadata": self.metadata,
            "rate_limit_remaining": self.rate_limit_remaining,
            "rate_limit_reset": self.rate_limit_reset
        }

class AuthManager:
    def __init__(self):
        self.identities: Dict[str, AuthIdentity] = {}
        self.active_identity_id: Optional[str] = None
        self._load_from_env()
        self._load_from_file()

    def _load_from_env(self):
        pat = os.environ.get("GITHUB_PAT")
        if pat:
            self.add_identity("env_pat", AuthType.PAT, pat, {"source": "env"})
            if not self.active_identity_id:
                self.switch_identity("env_pat")

    def _load_from_file(self):
        try:
            if os.path.exists("credentials.json"):
                with open("credentials.json", "r") as f:
                    data = json.load(f)
                    for ident in data.get("identities", []):
                        self.add_identity(
                            ident["id"],
                            AuthType(ident["type"]),
                            ident["token"],
                            ident.get("metadata")
                        )
                        if not self.active_identity_id:
                            self.switch_identity(ident["id"])
        except Exception as e:
            logger.error(f"Failed to load credentials from file: {e}")

    def add_identity(self, id: str, type: AuthType, token: str, metadata: Dict[str, Any] = None):
        self.identities[id] = AuthIdentity(id, type, token, metadata)
        logger.info(f"Added identity: {id} ({type.value})")

    def switch_identity(self, id: str):
        if id in self.identities:
            self.active_identity_id = id
            logger.info(f"Switched to identity: {id}")
        else:
            raise MCPError(f"Identity not found: {id}")

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

# --- GitHub Client ---

class GitHubClient:
    def __init__(self, auth_manager: AuthManager):
        self.auth_manager = auth_manager
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        # Timeout to prevent hanging: 60s connect, 60s read
        timeout = aiohttp.ClientTimeout(total=120, connect=60, sock_read=60)
        self.session = aiohttp.ClientSession(timeout=timeout)

    async def stop(self):
        if self.session:
            await self.session.close()

    async def request(self, method: str, path: str, **kwargs) -> Any:
        if not self.session:
            await self.start()

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

        try:
            async with self.session.request(method, url, headers=headers, **kwargs) as response:
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
                    data = await response.json()
                else:
                    data = await response.text()

                if response.status >= 400:
                    # Specific error handling
                    msg = f"GitHub API {response.status}"
                    if isinstance(data, dict) and "message" in data:
                        msg = data["message"]
                    logger.error(f"GitHub Request Failed: {method} {url} -> {response.status} {msg}")
                    raise GitHubError(response.status, msg, data)
                
                return data

        except aiohttp.ClientError as e:
            logger.error(f"Network error: {e}")
            raise MCPError(f"Network error: {str(e)}")
        except asyncio.TimeoutError:
            logger.error("Request timed out")
            raise MCPError("Request timed out")

# --- Tool Registry ---

class Tool:
    """Represents a registered MCP tool."""
    def __init__(self, name: str, description: str, handler: Callable, schema: Dict[str, Any]):
        self.name = name
        self.description = description
        self.handler = handler
        self.schema = schema

class ToolRegistry:
    """Manages the registration and retrieval of MCP tools."""
    def __init__(self):
        self.tools: Dict[str, Tool] = {}

    def register(self, name: str, description: str, schema: Dict[str, Any]):
        def decorator(func: Callable):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                return await func(*args, **kwargs)
            self.tools[name] = Tool(name, description, wrapper, schema)
            return wrapper
        return decorator

    def get_tools_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.schema
            }
            for t in self.tools.values()
        ]

registry = ToolRegistry()
auth_manager = AuthManager()
github_client = GitHubClient(auth_manager)

# Server Protection: Limit concurrent tool executions
TOOL_CONCURRENCY = asyncio.Semaphore(20)

# --- Tool Implementations ---

# Batch 1: Repositories (Extended)
@registry.register(
    "list_repositories",
    "List repositories for the authenticated user or an organization.",
    {
        "type": "object",
        "properties": {
            "visibility": {"type": "string", "enum": ["all", "public", "private"], "default": "all"},
            "sort": {"type": "string", "default": "updated"},
            "org": {"type": "string", "description": "Organization name (optional)"}
        }
    }
)
async def list_repositories(visibility="all", sort="updated", org=None):
    params = {"sort": sort}
    if org:
        # Org endpoint uses 'type' instead of 'visibility'
        # Map visibility enum to type enum roughly
        if visibility in ["public", "private", "all"]:
            params["type"] = visibility
        else:
            params["type"] = "all"
        endpoint = f"/orgs/{org}/repos"
    else:
        # User endpoint uses 'visibility'
        params["visibility"] = visibility
        endpoint = "/user/repos"
        
    return await github_client.request("GET", endpoint, params=params)

@registry.register(
    "create_repository",
    "Create a new repository.",
    {
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
)
async def create_repository(name, description=None, private=False, auto_init=True, org=None):
    data = {
        "name": name,
        "description": description,
        "private": private,
        "auto_init": auto_init
    }
    endpoint = f"/orgs/{org}/repos" if org else "/user/repos"
    return await github_client.request("POST", endpoint, json=data)

@registry.register(
    "get_repository",
    "Get details of a specific repository.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"}
        },
        "required": ["owner", "repo"]
    }
)
async def get_repository(owner, repo):
    return await github_client.request("GET", f"/repos/{owner}/{repo}")

@registry.register(
    "delete_repository",
    "Delete a repository.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"}
        },
        "required": ["owner", "repo"]
    }
)
async def delete_repository(owner, repo):
    return await github_client.request("DELETE", f"/repos/{owner}/{repo}")

@registry.register(
    "transfer_repository",
    "Transfer a repository.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "new_owner": {"type": "string"}
        },
        "required": ["owner", "repo", "new_owner"]
    }
)
async def transfer_repository(owner, repo, new_owner):
    return await github_client.request("POST", f"/repos/{owner}/{repo}/transfer", json={"new_owner": new_owner})

@registry.register(
    "update_repository_archive",
    "Archive or unarchive a repository.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "archived": {"type": "boolean"}
        },
        "required": ["owner", "repo", "archived"]
    }
)
async def update_repository_archive(owner, repo, archived):
    return await github_client.request("PATCH", f"/repos/{owner}/{repo}", json={"archived": archived})

# Batch 2: Files & Commits (Extended)

@registry.register(
    "get_file_contents",
    "Get the contents of a file (Base64 encoded).",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "path": {"type": "string"},
            "ref": {"type": "string", "description": "Branch, tag, or commit SHA"}
        },
        "required": ["owner", "repo", "path"]
    }
)
async def get_file_contents(owner, repo, path, ref=None):
    params = {"ref": ref} if ref else {}
    return await github_client.request("GET", f"/repos/{owner}/{repo}/contents/{path}", params=params)

@registry.register(
    "create_or_update_file",
    "Create or update a file.",
    {
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
)
async def create_or_update_file(owner, repo, path, message, content, sha=None, branch=None):
    data = {
        "message": message,
        "content": content,
        "sha": sha,
        "branch": branch
    }
    # Remove None values
    data = {k: v for k, v in data.items() if v is not None}
    return await github_client.request("PUT", f"/repos/{owner}/{repo}/contents/{path}", json=data)

@registry.register(
    "delete_file",
    "Delete a file.",
    {
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
)
async def delete_file(owner, repo, path, message, sha, branch=None):
    data = {"message": message, "sha": sha, "branch": branch}
    data = {k: v for k, v in data.items() if v is not None}
    return await github_client.request("DELETE", f"/repos/{owner}/{repo}/contents/{path}", json=data)

@registry.register(
    "create_branch",
    "Create a branch (ref).",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "ref": {"type": "string", "description": "The name of the fully qualified reference (ie: refs/heads/master)"},
            "sha": {"type": "string", "description": "The SHA1 value for this reference."}
        },
        "required": ["owner", "repo", "ref", "sha"]
    }
)
async def create_branch(owner, repo, ref, sha):
    if not ref.startswith("refs/"):
        ref = f"refs/heads/{ref}"
    data = {"ref": ref, "sha": sha}
    return await github_client.request("POST", f"/repos/{owner}/{repo}/git/refs", json=data)

@registry.register(
    "delete_branch",
    "Delete a branch (ref).",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "ref": {"type": "string", "description": "The name of the fully qualified reference (ie: heads/master)"}
        },
        "required": ["owner", "repo", "ref"]
    }
)
async def delete_branch(owner, repo, ref):
    if ref.startswith("refs/"):
        ref = ref[5:] # Remove refs/ prefix if present, api expects heads/feature
    # Actually, for DELETE /repos/{owner}/{repo}/git/refs/{ref}, ref must be fully qualified but without refs/ prefix in URL usually?
    # GitHub API: DELETE /repos/{owner}/{repo}/git/refs/{ref} where ref is heads/feature-a
    return await github_client.request("DELETE", f"/repos/{owner}/{repo}/git/refs/{ref}")

@registry.register(
    "push_files",
    "Push multiple files in a single atomic commit.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "branch": {"type": "string"},
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string", "description": "Base64 encoded content"}
                    },
                    "required": ["path", "content"]
                }
            },
            "message": {"type": "string"}
        },
        "required": ["owner", "repo", "branch", "files", "message"]
    }
)
async def push_files(owner, repo, branch, files, message):
    # 1. Get latest commit SHA of branch
    ref_data = await github_client.request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
    latest_commit_sha = ref_data["object"]["sha"]
    
    # 2. Get tree SHA of that commit
    commit_data = await github_client.request("GET", f"/repos/{owner}/{repo}/git/commits/{latest_commit_sha}")
    base_tree_sha = commit_data["tree"]["sha"]
    
    # 3. Create Blobs for each file & build tree structure
    tree_items = []
    for file in files:
        blob_data = await github_client.request("POST", f"/repos/{owner}/{repo}/git/blobs", json={
            "content": file["content"],
            "encoding": "base64"
        })
        tree_items.append({
            "path": file["path"],
            "mode": "100644",
            "type": "blob",
            "sha": blob_data["sha"]
        })
        
    # 4. Create Tree
    tree_data = await github_client.request("POST", f"/repos/{owner}/{repo}/git/trees", json={
        "base_tree": base_tree_sha,
        "tree": tree_items
    })
    new_tree_sha = tree_data["sha"]
    
    # 5. Create Commit
    new_commit_data = await github_client.request("POST", f"/repos/{owner}/{repo}/git/commits", json={
        "message": message,
        "tree": new_tree_sha,
        "parents": [latest_commit_sha]
    })
    new_commit_sha = new_commit_data["sha"]
    
    # 6. Update Ref
    return await github_client.request("PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{branch}", json={
        "sha": new_commit_sha
    })

@registry.register(
    "get_tree",
    "Get the entire file tree of a repository.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "tree_sha": {"type": "string", "description": "Branch name or commit SHA"}
        },
        "required": ["owner", "repo", "tree_sha"]
    }
)
async def get_tree(owner, repo, tree_sha):
    return await github_client.request("GET", f"/repos/{owner}/{repo}/git/trees/{tree_sha}", params={"recursive": "1"})

@registry.register(
    "read_files_batch",
    "Read content of multiple files in parallel.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"}},
            "ref": {"type": "string", "description": "Branch or SHA"}
        },
        "required": ["owner", "repo", "paths"]
    }
)
async def read_files_batch(owner, repo, paths, ref=None):
    async def fetch_file(path):
        try:
            params = {"ref": ref} if ref else {}
            data = await github_client.request("GET", f"/repos/{owner}/{repo}/contents/{path}", params=params)
            return {"path": path, "content": data.get("content"), "encoding": data.get("encoding"), "error": None}
        except Exception as e:
            return {"path": path, "content": None, "error": str(e)}

    results = await asyncio.gather(*[fetch_file(p) for p in paths])
    return results

@registry.register(
    "get_organization",
    "Get organization information.",
    {
        "type": "object",
        "properties": {
            "org": {"type": "string"}
        },
        "required": ["org"]
    }
)
async def get_organization(org):
    return await github_client.request("GET", f"/orgs/{org}")

@registry.register(
    "list_commits",
    "List commits on a repository.",
    {
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
)
async def list_commits(owner, repo, sha=None, path=None, author=None, since=None, until=None):
    params = {k: v for k, v in locals().items() if v is not None and k not in ["owner", "repo"]}
    return await github_client.request("GET", f"/repos/{owner}/{repo}/commits", params=params)

@registry.register(
    "get_commit",
    "Get a specific commit.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "ref": {"type": "string", "description": "Commit SHA"}
        },
        "required": ["owner", "repo", "ref"]
    }
)
async def get_commit(owner, repo, ref):
    return await github_client.request("GET", f"/repos/{owner}/{repo}/commits/{ref}")

# Batch 3: Issues & Pull Requests

@registry.register(
    "list_issues",
    "List issues in a repository.",
    {
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
)
async def list_issues(owner, repo, state="open", labels=None, sort="created", direction="desc"):
    params = {"state": state, "labels": labels, "sort": sort, "direction": direction}
    params = {k: v for k, v in params.items() if v is not None}
    return await github_client.request("GET", f"/repos/{owner}/{repo}/issues", params=params)

@registry.register(
    "create_issue",
    "Create an issue.",
    {
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
)
async def create_issue(owner, repo, title, body=None, assignees=None, labels=None):
    data = {"title": title, "body": body, "assignees": assignees, "labels": labels}
    data = {k: v for k, v in data.items() if v is not None}
    return await github_client.request("POST", f"/repos/{owner}/{repo}/issues", json=data)

@registry.register(
    "update_issue",
    "Update an issue.",
    {
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
)
async def update_issue(owner, repo, issue_number, title=None, body=None, state=None, labels=None):
    data = {"title": title, "body": body, "state": state, "labels": labels}
    data = {k: v for k, v in data.items() if v is not None}
    return await github_client.request("PATCH", f"/repos/{owner}/{repo}/issues/{issue_number}", json=data)

@registry.register(
    "create_issue_comment",
    "Create a comment on an issue or PR.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "issue_number": {"type": "integer"},
            "body": {"type": "string"}
        },
        "required": ["owner", "repo", "issue_number", "body"]
    }
)
async def create_issue_comment(owner, repo, issue_number, body):
    return await github_client.request("POST", f"/repos/{owner}/{repo}/issues/{issue_number}/comments", json={"body": body})

@registry.register(
    "list_pull_requests",
    "List pull requests.",
    {
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
)
async def list_pull_requests(owner, repo, state="open", head=None, base=None):
    params = {"state": state, "head": head, "base": base}
    params = {k: v for k, v in params.items() if v is not None}
    return await github_client.request("GET", f"/repos/{owner}/{repo}/pulls", params=params)

@registry.register(
    "create_pull_request",
    "Create a pull request.",
    {
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
)
async def create_pull_request(owner, repo, title, head, base, body=None, draft=False):
    data = {"title": title, "head": head, "base": base, "body": body, "draft": draft}
    data = {k: v for k, v in data.items() if v is not None}
    return await github_client.request("POST", f"/repos/{owner}/{repo}/pulls", json=data)

@registry.register(
    "merge_pull_request",
    "Merge a pull request.",
    {
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
)
async def merge_pull_request(owner, repo, pull_number, commit_title=None, commit_message=None, merge_method="merge"):
    data = {"commit_title": commit_title, "commit_message": commit_message, "merge_method": merge_method}
    data = {k: v for k, v in data.items() if v is not None}
    return await github_client.request("PUT", f"/repos/{owner}/{repo}/pulls/{pull_number}/merge", json=data)

# Batch 4: Actions & Search & Misc (Extended)

@registry.register(
    "list_workflows",
    "List GitHub Actions workflows.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"}
        },
        "required": ["owner", "repo"]
    }
)
async def list_workflows(owner, repo):
    return await github_client.request("GET", f"/repos/{owner}/{repo}/actions/workflows")

@registry.register(
    "list_workflow_runs",
    "List workflow runs.",
    {
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
)
async def list_workflow_runs(owner, repo, workflow_id, status=None, event=None):
    params = {"status": status, "event": event}
    params = {k: v for k, v in params.items() if v is not None}
    return await github_client.request("GET", f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs", params=params)

@registry.register(
    "get_workflow_run",
    "Get a specific workflow run.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "run_id": {"type": "integer"}
        },
        "required": ["owner", "repo", "run_id"]
    }
)
async def get_workflow_run(owner, repo, run_id):
    return await github_client.request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}")

@registry.register(
    "cancel_workflow_run",
    "Cancel a workflow run.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "run_id": {"type": "integer"}
        },
        "required": ["owner", "repo", "run_id"]
    }
)
async def cancel_workflow_run(owner, repo, run_id):
    return await github_client.request("POST", f"/repos/{owner}/{repo}/actions/runs/{run_id}/cancel")

@registry.register(
    "trigger_workflow_dispatch",
    "Trigger a workflow dispatch event.",
    {
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
)
async def trigger_workflow_dispatch(owner, repo, workflow_id, ref, inputs=None):
    data = {"ref": ref, "inputs": inputs or {}}
    return await github_client.request("POST", f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches", json=data)

@registry.register(
    "search_code",
    "Search for code.",
    {
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "Search query"},
            "sort": {"type": "string"},
            "order": {"type": "string"}
        },
        "required": ["q"]
    }
)
async def search_code(q, sort=None, order=None):
    params = {"q": q, "sort": sort, "order": order}
    params = {k: v for k, v in params.items() if v is not None}
    return await github_client.request("GET", "/search/code", params=params)

@registry.register(
    "search_issues",
    "Search for issues and pull requests.",
    {
        "type": "object",
        "properties": {
            "q": {"type": "string"},
            "sort": {"type": "string"},
            "order": {"type": "string"}
        },
        "required": ["q"]
    }
)
async def search_issues(q, sort=None, order=None):
    params = {"q": q, "sort": sort, "order": order}
    params = {k: v for k, v in params.items() if v is not None}
    return await github_client.request("GET", "/search/issues", params=params)

@registry.register(
    "search_repositories",
    "Search for repositories.",
    {
        "type": "object",
        "properties": {
            "q": {"type": "string"},
            "sort": {"type": "string"},
            "order": {"type": "string"}
        },
        "required": ["q"]
    }
)
async def search_repositories(q, sort=None, order=None):
    params = {"q": q, "sort": sort, "order": order}
    params = {k: v for k, v in params.items() if v is not None}
    return await github_client.request("GET", "/search/repositories", params=params)

@registry.register(
    "search_users",
    "Search for users.",
    {
        "type": "object",
        "properties": {
            "q": {"type": "string"},
            "sort": {"type": "string"},
            "order": {"type": "string"}
        },
        "required": ["q"]
    }
)
async def search_users(q, sort=None, order=None):
    params = {"q": q, "sort": sort, "order": order}
    params = {k: v for k, v in params.items() if v is not None}
    return await github_client.request("GET", "/search/users", params=params)

@registry.register(
    "get_user",
    "Get user information.",
    {
        "type": "object",
        "properties": {
            "username": {"type": "string", "description": "Username (optional, defaults to auth user)"}
        }
    }
)
async def get_user(username=None):
    endpoint = f"/users/{username}" if username else "/user"
    return await github_client.request("GET", endpoint)

@registry.register(
    "list_collaborators",
    "List collaborators on a repository.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "affiliation": {"type": "string", "default": "all"}
        },
        "required": ["owner", "repo"]
    }
)
async def list_collaborators(owner, repo, affiliation="all"):
    return await github_client.request("GET", f"/repos/{owner}/{repo}/collaborators", params={"affiliation": affiliation})

@registry.register(
    "add_collaborator",
    "Add a collaborator.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "username": {"type": "string"},
            "permission": {"type": "string", "enum": ["pull", "push", "admin", "maintain", "triage"], "default": "push"}
        },
        "required": ["owner", "repo", "username"]
    }
)
async def add_collaborator(owner, repo, username, permission="push"):
    return await github_client.request("PUT", f"/repos/{owner}/{repo}/collaborators/{username}", json={"permission": permission})

@registry.register(
    "remove_collaborator",
    "Remove a collaborator.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "username": {"type": "string"}
        },
        "required": ["owner", "repo", "username"]
    }
)
async def remove_collaborator(owner, repo, username):
    return await github_client.request("DELETE", f"/repos/{owner}/{repo}/collaborators/{username}")

# Batch 5: Webhooks, Gists, Releases, Security

@registry.register(
    "list_webhooks",
    "List webhooks for a repository.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"}
        },
        "required": ["owner", "repo"]
    }
)
async def list_webhooks(owner, repo):
    return await github_client.request("GET", f"/repos/{owner}/{repo}/hooks")

@registry.register(
    "create_webhook",
    "Create a webhook.",
    {
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
)
async def create_webhook(owner, repo, url, content_type="json", events=None, active=True, secret=None):
    config = {"url": url, "content_type": content_type}
    if secret:
        config["secret"] = secret
    data = {"config": config, "events": events or ["push"], "active": active}
    return await github_client.request("POST", f"/repos/{owner}/{repo}/hooks", json=data)

@registry.register(
    "delete_webhook",
    "Delete a webhook.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "hook_id": {"type": "integer"}
        },
        "required": ["owner", "repo", "hook_id"]
    }
)
async def delete_webhook(owner, repo, hook_id):
    return await github_client.request("DELETE", f"/repos/{owner}/{repo}/hooks/{hook_id}")

@registry.register(
    "list_gists",
    "List gists.",
    {
        "type": "object",
        "properties": {
            "username": {"type": "string", "description": "Optional: list user's gists"}
        }
    }
)
async def list_gists(username=None):
    endpoint = f"/users/{username}/gists" if username else "/gists"
    return await github_client.request("GET", endpoint)

@registry.register(
    "create_gist",
    "Create a gist.",
    {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "files": {"type": "object", "description": "Map of filename to content"},
            "public": {"type": "boolean", "default": False}
        },
        "required": ["files"]
    }
)
async def create_gist(files, description=None, public=False):
    # API expects files: { "filename": { "content": "..." } }
    formatted_files = {k: {"content": v} for k, v in files.items()}
    data = {"files": formatted_files, "description": description, "public": public}
    return await github_client.request("POST", "/gists", json=data)

@registry.register(
    "delete_gist",
    "Delete a gist.",
    {
        "type": "object",
        "properties": {
            "gist_id": {"type": "string"}
        },
        "required": ["gist_id"]
    }
)
async def delete_gist(gist_id):
    return await github_client.request("DELETE", f"/gists/{gist_id}")

@registry.register(
    "create_release",
    "Create a release.",
    {
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
)
async def create_release(owner, repo, tag_name, name=None, body=None, draft=False, prerelease=False):
    data = {"tag_name": tag_name, "name": name, "body": body or "", "draft": draft, "prerelease": prerelease}
    return await github_client.request("POST", f"/repos/{owner}/{repo}/releases", json=data)

@registry.register(
    "upload_release_asset",
    "Upload a binary asset to a release.",
    {
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
)
async def upload_release_asset(owner, repo, release_id, name, content, label=None, content_type="application/octet-stream"):
    # First get release to find upload_url
    release = await github_client.request("GET", f"/repos/{owner}/{repo}/releases/{release_id}")
    upload_url_template = release.get("upload_url")
    if not upload_url_template:
        raise ToolError("Release does not have an upload_url")
    
    # Remove template params {?name,label}
    upload_url = upload_url_template.split("{")[0]
    
    # Decode content
    try:
        data = base64.b64decode(content)
    except Exception:
        raise ToolError("Invalid Base64 content")
        
    params = {"name": name}
    if label:
        params["label"] = label
        
    headers = {"Content-Type": content_type}
    
    return await github_client.request("POST", upload_url, params=params, data=data, headers=headers)

@registry.register(
    "enable_vulnerability_alerts",
    "Enable vulnerability alerts for a repository.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"}
        },
        "required": ["owner", "repo"]
    }
)
async def enable_vulnerability_alerts(owner, repo):
    return await github_client.request("PUT", f"/repos/{owner}/{repo}/vulnerability-alerts")

@registry.register(
    "disable_vulnerability_alerts",
    "Disable vulnerability alerts for a repository.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"}
        },
        "required": ["owner", "repo"]
    }
)
async def disable_vulnerability_alerts(owner, repo):
    return await github_client.request("DELETE", f"/repos/{owner}/{repo}/vulnerability-alerts")

@registry.register(
    "enable_automated_security_fixes",
    "Enable automated security fixes.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"}
        },
        "required": ["owner", "repo"]
    }
)
async def enable_automated_security_fixes(owner, repo):
    return await github_client.request("PUT", f"/repos/{owner}/{repo}/automated-security-fixes")

@registry.register(
    "disable_automated_security_fixes",
    "Disable automated security fixes.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"}
        },
        "required": ["owner", "repo"]
    }
)
async def disable_automated_security_fixes(owner, repo):
    return await github_client.request("DELETE", f"/repos/{owner}/{repo}/automated-security-fixes")

@registry.register(
    "list_projects",
    "List projects (classic).",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string", "description": "If provided, lists repo projects. Otherwise lists org/user projects."},
            "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"}
        },
        "required": ["owner"]
    }
)
async def list_projects(owner, repo=None, state="open"):
    params = {"state": state}
    if repo:
        endpoint = f"/repos/{owner}/{repo}/projects"
    else:
        # Try org first, then user? Or separate? 
        # GitHub API has /orgs/{org}/projects and /users/{username}/projects
        # For simplicity, if repo is not provided, we assume 'owner' is an org or user. 
        # But we need to know which one.
        # Let's try org first, if 404, try user? No, deterministic is better.
        # Let's assume it's an org project listing if no repo.
        endpoint = f"/orgs/{owner}/projects"
        
    try:
        return await github_client.request("GET", endpoint, params=params)
    except GitHubError as e:
        if e.status == 404 and not repo:
             # Fallback to user projects
             endpoint = f"/users/{owner}/projects"
             return await github_client.request("GET", endpoint, params=params)
        raise e

@registry.register(
    "create_project",
    "Create a project (classic).",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string", "description": "If provided, creates repo project."},
            "name": {"type": "string"},
            "body": {"type": "string"}
        },
        "required": ["owner", "name"]
    }
)
async def create_project(owner, name, repo=None, body=None):
    data = {"name": name, "body": body}
    if repo:
        endpoint = f"/repos/{owner}/{repo}/projects"
    else:
        endpoint = f"/orgs/{owner}/projects" # Only orgs can have projects created via this simplified interface?
        # User projects creation endpoint is /user/projects (for authenticated user)
        # But 'owner' implies we create it for 'owner'. 
        # If owner is the auth user, we use /user/projects.
        # Let's support Repo and Org projects primarily.
    
    return await github_client.request("POST", endpoint, json=data)

@registry.register(
    "list_milestones",
    "List milestones.",
    {
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
)
async def list_milestones(owner, repo, state="open", sort="due_date", direction="asc"):
    params = {"state": state, "sort": sort, "direction": direction}
    return await github_client.request("GET", f"/repos/{owner}/{repo}/milestones", params=params)

@registry.register(
    "create_milestone",
    "Create a milestone.",
    {
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
)
async def create_milestone(owner, repo, title, state="open", description=None, due_on=None):
    data = {"title": title, "state": state, "description": description, "due_on": due_on}
    return await github_client.request("POST", f"/repos/{owner}/{repo}/milestones", json=data)

@registry.register(
    "list_labels",
    "List labels.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"}
        },
        "required": ["owner", "repo"]
    }
)
async def list_labels(owner, repo):
    return await github_client.request("GET", f"/repos/{owner}/{repo}/labels")

@registry.register(
    "create_label",
    "Create a label.",
    {
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
)
async def create_label(owner, repo, name, color, description=None):
    data = {"name": name, "color": color, "description": description}
    return await github_client.request("POST", f"/repos/{owner}/{repo}/labels", json=data)

@registry.register(
    "delete_label",
    "Delete a label.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "name": {"type": "string"}
        },
        "required": ["owner", "repo", "name"]
    }
)
async def delete_label(owner, repo, name):
    return await github_client.request("DELETE", f"/repos/{owner}/{repo}/labels/{name}")

# --- Server Logic ---

async def handle_health(request):
    identity = auth_manager.get_active_identity()
    return web.json_response({
        "status": "healthy",
        "service": "github-mcp-server",
        "version": MCP_VERSION,
        "auth_status": {
            "active": identity.id if identity else None,
            "type": identity.type.value if identity else None,
            "rate_limit_remaining": identity.rate_limit_remaining if identity else None
        }
    })

async def handle_messages(request):
    try:
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None}, status=400)

        jsonrpc = data.get("jsonrpc")
        method = data.get("method")
        msg_id = data.get("id")
        params = data.get("params", {})

        if jsonrpc != "2.0":
             return web.json_response({"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid Request"}, "id": msg_id}, status=400)

        if method == "initialize":
            return web.json_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "capabilities": {
                        "tools": {},
                        "logging": {},
                        "resources": {}
                    },
                    "serverInfo": {
                        "name": "github-mcp-server",
                        "version": MCP_VERSION
                    },
                    "protocolVersion": "2024-11-05" 
                }
            })
        
        elif method == "notifications/initialized":
            return web.Response(status=200)

        elif method == "tools/list":
            return web.json_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": registry.get_tools_schema()
                }
            })

        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})

            if not tool_name:
                return web.json_response({"jsonrpc": "2.0", "error": {"code": -32602, "message": "Missing 'name'"}, "id": msg_id})

            if tool_name not in registry.tools:
                return web.json_response({"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Tool not found: {tool_name}"}, "id": msg_id})

            tool = registry.tools[tool_name]
            
            # Server Protection: Concurrency Limit
            async with TOOL_CONCURRENCY:
                try:
                    result = await asyncio.wait_for(tool.handler(**arguments), timeout=120.0)
                    
                    content = [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2) if not isinstance(result, str) else result
                        }
                    ]
                    
                    return web.json_response({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": content,
                            "isError": False
                        }
                    })
                except asyncio.TimeoutError:
                    return web.json_response({"jsonrpc": "2.0", "error": {"code": -32000, "message": "Execution timed out"}, "id": msg_id})
                except ToolError as e:
                    return web.json_response({"jsonrpc": "2.0", "error": {"code": -32000, "message": str(e)}, "id": msg_id})
                except GitHubError as e:
                    return web.json_response({"jsonrpc": "2.0", "error": {"code": -32000, "message": f"GitHub API Error: {str(e)}"}, "id": msg_id})
                except Exception as e:
                    logger.exception(f"Unexpected error in tool {tool_name}")
                    return web.json_response({"jsonrpc": "2.0", "error": {"code": -32000, "message": "Internal error"}, "id": msg_id})

        elif method == "ping":
            return web.json_response({"jsonrpc": "2.0", "id": msg_id, "result": {}})

        else:
            return web.json_response({"jsonrpc": "2.0", "error": {"code": -32601, "message": "Method not found"}, "id": msg_id})

    except Exception as e:
        logger.exception("Error processing message")
        return web.json_response({"jsonrpc": "2.0", "error": {"code": -32603, "message": "Internal error"}, "id": None}, status=500)

async def handle_sse(request):
    response = web.StreamResponse()
    response.headers['Content-Type'] = 'text/event-stream'
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    
    try:
        await response.prepare(request)
        
        # Standard MCP Handshake: Endpoint Event
        session_id = str(uuid.uuid4())
        
        # Using relative URI for flexibility
        endpoint_uri = f"/messages?session_id={session_id}"
        
        await response.write(f"event: endpoint\ndata: {endpoint_uri}\n\n".encode('utf-8'))
        logger.info(f"New SSE connection. Session: {session_id}")
        
        while True:
            # Keep connection open with explicit pings
            await response.write(b": keepalive\n\n")
            await asyncio.sleep(15)
            
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    except Exception as e:
        logger.error(f"SSE Error: {e}")
    
    return response

# --- Auth Management Endpoints ---

async def handle_add_pat(request):
    try:
        data = await request.json()
        token = data.get("token")
        id = data.get("id", f"pat_{len(auth_manager.identities)}")
        if not token:
            raise ValueError("Token required")
        
        auth_manager.add_identity(id, AuthType.PAT, token)
        if not auth_manager.active_identity_id:
            auth_manager.switch_identity(id)
            
        return web.json_response({"status": "success", "id": id})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=400)

async def handle_switch_identity(request):
    try:
        data = await request.json()
        id = data.get("id")
        auth_manager.switch_identity(id)
        return web.json_response({"status": "success", "active": id})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=400)

async def handle_remove_identity(request):
    try:
        data = await request.json()
        id = data.get("id")
        auth_manager.remove_identity(id)
        return web.json_response({"status": "success", "removed": id})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=400)

async def handle_add_app_token(request):
    try:
        data = await request.json()
        token = data.get("token")
        id = data.get("id", f"app_{len(auth_manager.identities)}")
        if not token:
            raise ValueError("Token required")
            
        auth_manager.add_identity(id, AuthType.APP, token)
        if not auth_manager.active_identity_id:
            auth_manager.switch_identity(id)
            
        return web.json_response({"status": "success", "id": id})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=400)

# --- OAuth Handlers ---

async def handle_oauth_start(request):
    client_id = os.environ.get("GITHUB_CLIENT_ID")
    if not client_id:
        return web.Response(text="Missing GITHUB_CLIENT_ID configuration", status=500)
    
    # Scopes: repo, user, workflow, delete_repo, etc.
    scopes = "repo user workflow delete_repo admin:org project"
    redirect_uri = f"https://github.com/login/oauth/authorize?client_id={client_id}&scope={scopes}"
    
    raise web.HTTPFound(redirect_uri)

async def handle_oauth_callback(request):
    code = request.query.get("code")
    if not code:
        return web.Response(text="<html><body><h1>Error: No code provided</h1></body></html>", content_type="text/html", status=400)

    client_id = os.environ.get("GITHUB_CLIENT_ID")
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        return web.Response(text="<html><body><h1>Error: Server misconfigured (Missing Client ID/Secret)</h1></body></html>", content_type="text/html", status=500)
        
    # Exchange code for token
    token_url = "https://github.com/login/oauth/access_token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code
    }
    headers = {"Accept": "application/json"}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, json=data, headers=headers) as resp:
            if resp.status != 200:
                return web.Response(text=f"<html><body><h1>Error exchanging token: {resp.status}</h1></body></html>", content_type="text/html", status=400)
            token_data = await resp.json()
            
    access_token = token_data.get("access_token")
    if not access_token:
        return web.Response(text=f"<html><body><h1>Error: No access token received</h1><pre>{json.dumps(token_data)}</pre></body></html>", content_type="text/html", status=400)
        
    # Add identity
    # We fetch user info to name the identity
    temp_client = GitHubClient(auth_manager)
    # Mock auth manager for a single request
    temp_auth = AuthIdentity("temp", AuthType.OAUTH, access_token)
    
    # We can't easily use the main GitHubClient because it's bound to the global AuthManager.
    # So we do a raw request here or add it first.
    
    ident_id = f"oauth_{code[:8]}" # Temporary ID
    auth_manager.add_identity(ident_id, AuthType.OAUTH, access_token)
    
    # Now try to get user info to rename/update it
    try:
        # Switch temporarily to get info? Or just use raw request
        headers = {
            "Authorization": f"token {access_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        async with aiohttp.ClientSession() as session:
             async with session.get(f"{GITHUB_API_BASE}/user", headers=headers) as resp:
                 if resp.status == 200:
                     user_data = await resp.json()
                     username = user_data.get("login")
                     # Rename identity
                     auth_manager.remove_identity(ident_id)
                     final_id = f"oauth_{username}"
                     auth_manager.add_identity(final_id, AuthType.OAUTH, access_token, metadata=user_data)
                     auth_manager.switch_identity(final_id)
                     message = f"Authenticated as {username}"
                 else:
                     message = "Authenticated (User info fetch failed)"
                     auth_manager.switch_identity(ident_id)
                     
        return web.Response(text=f"<html><body><h1>Success</h1><p>{message}</p><p>You can close this window.</p></body></html>", content_type="text/html")
        
    except Exception as e:
        return web.Response(text=f"<html><body><h1>Error during setup</h1><p>{str(e)}</p></body></html>", content_type="text/html", status=500)

# --- Main Application ---

async def handle_root(request):
    return web.Response(text=f"""
    <html>
        <head><title>GitHub MCP Server</title></head>
        <body>
            <h1>GitHub MCP Server Running</h1>
            <p>Version: {MCP_VERSION}</p>
            <p>Status: Online</p>
            <p><a href="/health">Health Check</a></p>
        </body>
    </html>
    """, content_type="text/html")

async def run_server():
    setup_logging()
    
    # Start Client
    await github_client.start()
    
    app = web.Application()
    
    # CORS setup (Simple middleware approach)
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            response = web.Response()
        else:
            try:
                response = await handler(request)
            except web.HTTPException as e:
                response = e
        
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS, PUT, DELETE, PATCH'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With, Accept, Origin'
        return response

    # Crash Protection: Global Error Handling Middleware
    @web.middleware
    async def error_middleware(request, handler):
        try:
            return await handler(request)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Log full traceback
            logger.exception(f"Unhandled exception processing {request.method} {request.path}")
            # Return JSON error instead of crashing/raw 500
            return web.json_response(
                {"status": "error", "message": "Internal Server Error", "detail": str(e)},
                status=500
            )

    app.middlewares.append(cors_middleware)
    app.middlewares.append(error_middleware)

    app.router.add_get("/", handle_root)
    app.router.add_get("/sse", handle_sse)
    app.router.add_post("/messages", handle_messages)
    app.router.add_get("/health", handle_health)
    
    # Auth endpoints
    app.router.add_post("/auth/pat", handle_add_pat)
    app.router.add_post("/auth/app", handle_add_app_token)
    app.router.add_post("/auth/switch", handle_switch_identity)
    app.router.add_post("/auth/remove", handle_remove_identity)
    
    # OAuth endpoints
    app.router.add_get("/auth/oauth/start", handle_oauth_start)
    app.router.add_get("/auth/oauth/callback", handle_oauth_callback)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Updated to 0.0.0.0 and 8001 per request
    site = web.TCPSite(runner, '0.0.0.0', 8001)
    
    logger.info("Starting GitHub MCP Server on http://0.0.0.0:8001")
    await site.start()
    
    # Graceful Shutdown Handling
    stop_event = asyncio.Event()
    
    def signal_handler():
        logger.info("Signal received, shutting down...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    await stop_event.wait()
    
    logger.info("Cleaning up resources...")
    await github_client.stop()
    await runner.cleanup()
    logger.info("Server stopped gracefully")

if __name__ == "__main__":
    try:
        asyncio.run(run_server())
    except (KeyboardInterrupt, SystemExit):
        pass # Handled inside run_server logic mostly
