import asyncio
import json
import logging
import os
import secrets
import base64
from typing import Any, Dict, List, Optional, Union
from datetime import datetime

import aiohttp
from aiohttp import web

from auth import EnhancedGitHubAuthManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler("/root/mcp/github-mcp-enhanced/server.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("ENHANCED_GITHUB_MCP")

class EnhancedGitHubAPIClient:
    """Enhanced GitHub API client with extended functionality."""

    def __init__(self, auth_manager: EnhancedGitHubAuthManager):
        self.auth_manager = auth_manager
        self.base_url = "https://api.github.com"
        self.upload_url = "https://uploads.github.com"
        self.session: Optional[aiohttp.ClientSession] = None

    async def ensure_session(self):
        """Ensure HTTP session exists."""
        if not self.session or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        """Close HTTP session."""
        if self.session and not self.session.closed:
            await self.session.close()

    async def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None, 
                           params: Optional[Dict] = None, headers: Optional[Dict] = None,
                           is_upload: bool = False) -> Dict[str, Any]:
        """Make authenticated request to GitHub API with enhanced error handling."""
        if not self.auth_manager.is_authenticated():
            return {
                "status": 401,
                "error": "Authentication required",
                "instructions": "Please authenticate using PAT or OAuth flow",
                "auth_methods": self.auth_manager.get_authentication_methods()
            }
            
        await self.ensure_session()
        url = f"{self.upload_url if is_upload else self.base_url}{endpoint}"
        
        # Merge custom headers with auth headers
        req_headers = self.auth_manager.get_auth_headers()
        req_headers["Accept"] = "application/vnd.github.v3+json"
        req_headers["User-Agent"] = "ENHANCED-GitHub-MCP/2.0"
        
        if headers:
            req_headers.update(headers)
            
        try:
            if method.upper() == "GET":
                async with self.session.get(url, headers=req_headers, params=params) as resp:
                    result = await resp.json() if resp.content_length else {}
                    # Update rate limit info
                    self._update_rate_limit(resp)
                    return {"status": resp.status, "data": result, "headers": dict(resp.headers)}
            elif method.upper() == "POST":
                async with self.session.post(url, headers=req_headers, json=data) as resp:
                    result = await resp.json() if resp.content_length else {}
                    self._update_rate_limit(resp)
                    return {"status": resp.status, "data": result, "headers": dict(resp.headers)}
            elif method.upper() == "PATCH":
                async with self.session.patch(url, headers=req_headers, json=data) as resp:
                    result = await resp.json() if resp.content_length else {}
                    self._update_rate_limit(resp)
                    return {"status": resp.status, "data": result, "headers": dict(resp.headers)}
            elif method.upper() == "DELETE":
                async with self.session.delete(url, headers=req_headers) as resp:
                    result = await resp.text() if resp.content_length else ""
                    self._update_rate_limit(resp)
                    return {"status": resp.status, "data": result, "headers": dict(resp.headers)}
            elif method.upper() == "PUT":
                async with self.session.put(url, headers=req_headers, json=data) as resp:
                    result = await resp.json() if resp.content_length else {}
                    self._update_rate_limit(resp)
                    return {"status": resp.status, "data": result, "headers": dict(resp.headers)}
        except asyncio.TimeoutError:
            logger.error(f"API request timed out: {method} {url}")
            return {"status": 504, "error": "Request timeout"}
        except Exception as e:
            logger.error(f"API request failed: {e}")
            return {"status": 500, "error": str(e)}

    def _update_rate_limit(self, response):
        """Update rate limit information from response headers."""
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_time = response.headers.get("X-RateLimit-Reset")
        limit = response.headers.get("X-RateLimit-Limit")
        
        if remaining and reset_time and limit:
            self.auth_manager.update_rate_limit_info(
                self.auth_manager.current_identity,
                {
                    "remaining": int(remaining),
                    "reset_time": int(reset_time),
                    "limit": int(limit),
                    "timestamp": datetime.now().isoformat()
                }
            )

    # Repository Operations
    async def list_repos(self, visibility: str = "all", affiliation: str = "owner,collaborator,organization_member") -> Dict[str, Any]:
        """List repositories with enhanced filtering."""
        params = {"visibility": visibility, "affiliation": affiliation, "per_page": 100}
        return await self._make_request("GET", "/user/repos", params=params)

    async def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        """Get repository details."""
        return await self._make_request("GET", f"/repos/{owner}/{repo}")

    async def create_repo(self, name: str, private: bool = False, description: str = "", 
                         auto_init: bool = False, gitignore_template: str = None, 
                         license_template: str = None) -> Dict[str, Any]:
        """Create a new repository with additional options."""
        data = {
            "name": name,
            "private": private,
            "description": description,
            "auto_init": auto_init
        }
        
        if gitignore_template:
            data["gitignore_template"] = gitignore_template
        if license_template:
            data["license_template"] = license_template
            
        return await self._make_request("POST", "/user/repos", data=data)

    async def delete_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        """Delete a repository."""
        return await self._make_request("DELETE", f"/repos/{owner}/{repo}")

    async def get_repo_contents(self, owner: str, repo: str, path: str = "") -> Dict[str, Any]:
        """Get repository contents."""
        return await self._make_request("GET", f"/repos/{owner}/{repo}/contents/{path}")

    async def create_file(self, owner: str, repo: str, path: str, content: str, message: str, 
                         branch: str = None) -> Dict[str, Any]:
        """Create a new file."""
        encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        data = {
            "message": message,
            "content": encoded_content
        }
        
        if branch:
            data["branch"] = branch
            
        return await self._make_request("PUT", f"/repos/{owner}/{repo}/contents/{path}", data=data)

    async def update_file(self, owner: str, repo: str, path: str, content: str, message: str, 
                         sha: str, branch: str = None) -> Dict[str, Any]:
        """Update an existing file."""
        encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        data = {
            "message": message,
            "content": encoded_content,
            "sha": sha
        }
        
        if branch:
            data["branch"] = branch
            
        return await self._make_request("PUT", f"/repos/{owner}/{repo}/contents/{path}", data=data)

    async def delete_file(self, owner: str, repo: str, path: str, message: str, sha: str, 
                         branch: str = None) -> Dict[str, Any]:
        """Delete a file."""
        data = {
            "message": message,
            "sha": sha
        }
        
        if branch:
            data["branch"] = branch
            
        return await self._make_request("DELETE", f"/repos/{owner}/{repo}/contents/{path}", data=data)

    async def get_branches(self, owner: str, repo: str) -> Dict[str, Any]:
        """Get all branches in a repository."""
        return await self._make_request("GET", f"/repos/{owner}/{repo}/branches")

    async def create_branch(self, owner: str, repo: str, branch_name: str, source_sha: str) -> Dict[str, Any]:
        """Create a new branch."""
        data = {
            "ref": f"refs/heads/{branch_name}",
            "sha": source_sha
        }
        return await self._make_request("POST", f"/repos/{owner}/{repo}/git/refs", data=data)

    async def delete_branch(self, owner: str, repo: str, branch_name: str) -> Dict[str, Any]:
        """Delete a branch."""
        return await self._make_request("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{branch_name}")

    # Issues & Pull Requests
    async def list_issues(self, owner: str, repo: str, state: str = "open", 
                         assignee: str = None, creator: str = None, mentioned: str = None,
                         labels: List[str] = None) -> Dict[str, Any]:
        """List issues in a repository with enhanced filtering."""
        params = {"state": state, "per_page": 100}
        if assignee:
            params["assignee"] = assignee
        if creator:
            params["creator"] = creator
        if mentioned:
            params["mentioned"] = mentioned
        if labels:
            params["labels"] = ",".join(labels)
            
        return await self._make_request("GET", f"/repos/{owner}/{repo}/issues", params=params)

    async def create_issue(self, owner: str, repo: str, title: str, body: str = "", 
                          labels: List[str] = None, assignees: List[str] = None, 
                          milestone: int = None) -> Dict[str, Any]:
        """Create a new issue."""
        data = {"title": title, "body": body}
        if labels:
            data["labels"] = labels
        if assignees:
            data["assignees"] = assignees
        if milestone:
            data["milestone"] = milestone
            
        return await self._make_request("POST", f"/repos/{owner}/{repo}/issues", data=data)

    async def close_issue(self, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
        """Close an issue."""
        data = {"state": "closed"}
        return await self._make_request("PATCH", f"/repos/{owner}/{repo}/issues/{issue_number}", data=data)

    async def update_issue(self, owner: str, repo: str, issue_number: int, title: str = None, 
                          body: str = None, state: str = None, labels: List[str] = None,
                          assignees: List[str] = None) -> Dict[str, Any]:
        """Update an issue."""
        data = {}
        if title is not None:
            data["title"] = title
        if body is not None:
            data["body"] = body
        if state is not None:
            data["state"] = state
        if labels is not None:
            data["labels"] = labels
        if assignees is not None:
            data["assignees"] = assignees
            
        return await self._make_request("PATCH", f"/repos/{owner}/{repo}/issues/{issue_number}", data=data)

    async def list_pull_requests(self, owner: str, repo: str, state: str = "open", 
                                head: str = None, base: str = None) -> Dict[str, Any]:
        """List pull requests."""
        params = {"state": state, "per_page": 100}
        if head:
            params["head"] = head
        if base:
            params["base"] = base
            
        return await self._make_request("GET", f"/repos/{owner}/{repo}/pulls", params=params)

    async def create_pull_request(self, owner: str, repo: str, title: str, head: str, 
                                 base: str, body: str = "", draft: bool = False) -> Dict[str, Any]:
        """Create a new pull request."""
        data = {
            "title": title,
            "head": head,
            "base": base,
            "body": body,
            "draft": draft
        }
        return await self._make_request("POST", f"/repos/{owner}/{repo}/pulls", data=data)

    async def merge_pull_request(self, owner: str, repo: str, pull_number: int, 
                                commit_title: str = None, commit_message: str = None,
                                merge_method: str = "merge") -> Dict[str, Any]:
        """Merge a pull request."""
        data = {
            "merge_method": merge_method
        }
        if commit_title:
            data["commit_title"] = commit_title
        if commit_message:
            data["commit_message"] = commit_message
            
        return await self._make_request("PUT", f"/repos/{owner}/{repo}/pulls/{pull_number}/merge", data=data)

    # Actions
    async def list_workflows(self, owner: str, repo: str) -> Dict[str, Any]:
        """List GitHub Actions workflows."""
        return await self._make_request("GET", f"/repos/{owner}/{repo}/actions/workflows")

    async def trigger_workflow(self, owner: str, repo: str, workflow_id: Union[int, str], 
                              ref: str, inputs: Dict = None) -> Dict[str, Any]:
        """Trigger a workflow."""
        data = {"ref": ref}
        if inputs:
            data["inputs"] = inputs
            
        return await self._make_request("POST", f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches", data=data)

    async def list_workflow_runs(self, owner: str, repo: str, workflow_id: Union[int, str],
                                branch: str = None, event: str = None, status: str = None) -> Dict[str, Any]:
        """List workflow runs."""
        params = {}
        if branch:
            params["branch"] = branch
        if event:
            params["event"] = event
        if status:
            params["status"] = status
            
        return await self._make_request("GET", f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs", params=params)

    async def cancel_workflow_run(self, owner: str, repo: str, run_id: int) -> Dict[str, Any]:
        """Cancel a workflow run."""
        return await self._make_request("POST", f"/repos/{owner}/{repo}/actions/runs/{run_id}/cancel")

    # Search
    async def search_code(self, query: str, sort: str = None, order: str = "desc") -> Dict[str, Any]:
        """Search code."""
        params = {"q": query}
        if sort:
            params["sort"] = sort
        if order:
            params["order"] = order
            
        return await self._make_request("GET", "/search/code", params=params)

    async def search_issues(self, query: str, sort: str = None, order: str = "desc") -> Dict[str, Any]:
        """Search issues."""
        params = {"q": query}
        if sort:
            params["sort"] = sort
        if order:
            params["order"] = order
            
        return await self._make_request("GET", "/search/issues", params=params)

    async def search_repositories(self, query: str, sort: str = None, order: str = "desc") -> Dict[str, Any]:
        """Search repositories."""
        params = {"q": query}
        if sort:
            params["sort"] = sort
        if order:
            params["order"] = order
            
        return await self._make_request("GET", "/search/repositories", params=params)

    async def search_users(self, query: str, sort: str = None, order: str = "desc") -> Dict[str, Any]:
        """Search users."""
        params = {"q": query}
        if sort:
            params["sort"] = sort
        if order:
            params["order"] = order
            
        return await self._make_request("GET", "/search/users", params=params)

    # Users & Organizations
    async def get_user(self, username: str = None) -> Dict[str, Any]:
        """Get user information."""
        endpoint = f"/users/{username}" if username else "/user"
        return await self._make_request("GET", endpoint)

    async def list_user_orgs(self, username: str = None) -> Dict[str, Any]:
        """List organizations for a user."""
        endpoint = f"/users/{username}/orgs" if username else "/user/orgs"
        return await self._make_request("GET", endpoint)

    async def list_org_repos(self, org: str, type: str = "all") -> Dict[str, Any]:
        """List organization repositories."""
        params = {"type": type}
        return await self._make_request("GET", f"/orgs/{org}/repos", params=params)

    # Releases
    async def list_releases(self, owner: str, repo: str) -> Dict[str, Any]:
        """List releases."""
        return await self._make_request("GET", f"/repos/{owner}/{repo}/releases")

    async def create_release(self, owner: str, repo: str, tag_name: str, name: str = None,
                            body: str = None, draft: bool = False, prerelease: bool = False) -> Dict[str, Any]:
        """Create a release."""
        data = {
            "tag_name": tag_name,
            "draft": draft,
            "prerelease": prerelease
        }
        if name:
            data["name"] = name
        if body:
            data["body"] = body
            
        return await self._make_request("POST", f"/repos/{owner}/{repo}/releases", data=data)

    async def upload_release_asset(self, upload_url: str, asset_name: str, content: bytes,
                                  content_type: str = "application/octet-stream") -> Dict[str, Any]:
        """Upload a release asset."""
        # Remove {?name,label} from upload_url
        clean_url = upload_url.split("{")[0]
        params = {"name": asset_name}
        
        headers = {"Content-Type": content_type, "Content-Length": str(len(content))}
        
        return await self._make_request("POST", clean_url, data=content, params=params, 
                                       headers=headers, is_upload=True)

    # Commits
    async def list_commits(self, owner: str, repo: str, sha: str = None, path: str = None,
                          author: str = None, since: str = None, until: str = None) -> Dict[str, Any]:
        """List commits."""
        params = {}
        if sha:
            params["sha"] = sha
        if path:
            params["path"] = path
        if author:
            params["author"] = author
        if since:
            params["since"] = since
        if until:
            params["until"] = until
            
        return await self._make_request("GET", f"/repos/{owner}/{repo}/commits", params=params)

    async def get_commit(self, owner: str, repo: str, ref: str) -> Dict[str, Any]:
        """Get a commit."""
        return await self._make_request("GET", f"/repos/{owner}/{repo}/commits/{ref}")

    # Collaborators
    async def list_collaborators(self, owner: str, repo: str, affiliation: str = "all") -> Dict[str, Any]:
        """List collaborators."""
        params = {"affiliation": affiliation}
        return await self._make_request("GET", f"/repos/{owner}/{repo}/collaborators", params=params)

    async def add_collaborator(self, owner: str, repo: str, username: str, permission: str = "push") -> Dict[str, Any]:
        """Add a collaborator."""
        data = {"permission": permission}
        return await self._make_request("PUT", f"/repos/{owner}/{repo}/collaborators/{username}", data=data)

    async def remove_collaborator(self, owner: str, repo: str, username: str) -> Dict[str, Any]:
        """Remove a collaborator."""
        return await self._make_request("DELETE", f"/repos/{owner}/{repo}/collaborators/{username}")

    # Webhooks
    async def list_hooks(self, owner: str, repo: str) -> Dict[str, Any]:
        """List webhooks."""
        return await self._make_request("GET", f"/repos/{owner}/{repo}/hooks")

    async def create_hook(self, owner: str, repo: str, name: str = "web", config: Dict[str, str] = None,
                         events: List[str] = None, active: bool = True) -> Dict[str, Any]:
        """Create a webhook."""
        data = {
            "name": name,
            "active": active
        }
        if config:
            data["config"] = config
        if events:
            data["events"] = events
            
        return await self._make_request("POST", f"/repos/{owner}/{repo}/hooks", data=data)

    async def delete_hook(self, owner: str, repo: str, hook_id: int) -> Dict[str, Any]:
        """Delete a webhook."""
        return await self._make_request("DELETE", f"/repos/{owner}/{repo}/hooks/{hook_id}")

    # Projects
    async def list_projects(self, owner: str, repo: str) -> Dict[str, Any]:
        """List projects."""
        return await self._make_request("GET", f"/repos/{owner}/{repo}/projects")

    async def create_project(self, owner: str, repo: str, name: str, body: str = None) -> Dict[str, Any]:
        """Create a project."""
        data = {"name": name}
        if body:
            data["body"] = body
            
        return await self._make_request("POST", f"/repos/{owner}/{repo}/projects", data=data)

    # Milestones
    async def list_milestones(self, owner: str, repo: str, state: str = "open") -> Dict[str, Any]:
        """List milestones."""
        params = {"state": state}
        return await self._make_request("GET", f"/repos/{owner}/{repo}/milestones", params=params)

    async def create_milestone(self, owner: str, repo: str, title: str, description: str = None,
                              due_on: str = None) -> Dict[str, Any]:
        """Create a milestone."""
        data = {"title": title}
        if description:
            data["description"] = description
        if due_on:
            data["due_on"] = due_on
            
        return await self._make_request("POST", f"/repos/{owner}/{repo}/milestones", data=data)

    # Labels
    async def list_labels(self, owner: str, repo: str) -> Dict[str, Any]:
        """List labels."""
        return await self._make_request("GET", f"/repos/{owner}/{repo}/labels")

    async def create_label(self, owner: str, repo: str, name: str, color: str, description: str = None) -> Dict[str, Any]:
        """Create a label."""
        data = {
            "name": name,
            "color": color
        }
        if description:
            data["description"] = description
            
        return await self._make_request("POST", f"/repos/{owner}/{repo}/labels", data=data)

    async def delete_label(self, owner: str, repo: str, name: str) -> Dict[str, Any]:
        """Delete a label."""
        return await self._make_request("DELETE", f"/repos/{owner}/{repo}/labels/{name}")

    # Gists
    async def list_gists(self, username: str = None) -> Dict[str, Any]:
        """List gists."""
        endpoint = f"/users/{username}/gists" if username else "/gists"
        return await self._make_request("GET", endpoint)

    async def create_gist(self, description: str, files: Dict[str, Dict[str, str]], public: bool = True) -> Dict[str, Any]:
        """Create a gist."""
        data = {
            "description": description,
            "public": public,
            "files": files
        }
        return await self._make_request("POST", "/gists", data=data)

    async def delete_gist(self, gist_id: str) -> Dict[str, Any]:
        """Delete a gist."""
        return await self._make_request("DELETE", f"/gists/{gist_id}")

    # Advanced repository management
    async def transfer_repo(self, owner: str, repo: str, new_owner: str) -> Dict[str, Any]:
        """Transfer repository ownership."""
        data = {"new_owner": new_owner}
        return await self._make_request("POST", f"/repos/{owner}/{repo}/transfer", data=data)

    async def archive_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        """Archive a repository."""
        data = {"archived": True}
        return await self._make_request("PATCH", f"/repos/{owner}/{repo}", data=data)

    async def unarchive_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        """Unarchive a repository."""
        data = {"archived": False}
        return await self._make_request("PATCH", f"/repos/{owner}/{repo}", data=data)

    async def enable_vulnerability_alerts(self, owner: str, repo: str) -> Dict[str, Any]:
        """Enable vulnerability alerts."""
        return await self._make_request("PUT", f"/repos/{owner}/{repo}/vulnerability-alerts")

    async def disable_vulnerability_alerts(self, owner: str, repo: str) -> Dict[str, Any]:
        """Disable vulnerability alerts."""
        return await self._make_request("DELETE", f"/repos/{owner}/{repo}/vulnerability-alerts")

    async def enable_automated_security_fixes(self, owner: str, repo: str) -> Dict[str, Any]:
        """Enable automated security fixes."""
        return await self._make_request("PUT", f"/repos/{owner}/{repo}/automated-security-fixes")

    async def disable_automated_security_fixes(self, owner: str, repo: str) -> Dict[str, Any]:
        """Disable automated security fixes."""
        return await self._make_request("DELETE", f"/repos/{owner}/{repo}/automated-security-fixes")


class EnhancedMCPProtocolEngine:
    """Enhanced MCP protocol engine with extended tools."""

    def __init__(self, app: web.Application, github_client: EnhancedGitHubAPIClient):
        self.app = app
        self.github_client = github_client
        self.tools = self._register_tools()

    def _register_tools(self) -> Dict[str, Any]:
        """Register all available tools."""
        return {
            # Repository tools
            "list_repos": {
                "description": "List repositories with enhanced filtering",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "visibility": {"type": "string", "enum": ["all", "public", "private"], "default": "all"},
                        "affiliation": {"type": "string", "default": "owner,collaborator,organization_member"}
                    }
                },
                "handler": self.github_client.list_repos
            },
            "get_repo": {
                "description": "Get repository details",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.get_repo
            },
            "create_repo": {
                "description": "Create a new repository with additional options",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "private": {"type": "boolean", "default": False},
                        "description": {"type": "string"},
                        "auto_init": {"type": "boolean", "default": False},
                        "gitignore_template": {"type": "string"},
                        "license_template": {"type": "string"}
                    },
                    "required": ["name"]
                },
                "handler": self.github_client.create_repo
            },
            "delete_repo": {
                "description": "Delete a repository",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.delete_repo
            },
            "get_repo_contents": {
                "description": "Get repository contents",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "path": {"type": "string", "default": ""}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.get_repo_contents
            },
            "create_file": {
                "description": "Create a new file in a repository",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "message": {"type": "string"},
                        "branch": {"type": "string"}
                    },
                    "required": ["owner", "repo", "path", "content", "message"]
                },
                "handler": self.github_client.create_file
            },
            "update_file": {
                "description": "Update an existing file in a repository",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "message": {"type": "string"},
                        "sha": {"type": "string"},
                        "branch": {"type": "string"}
                    },
                    "required": ["owner", "repo", "path", "content", "message", "sha"]
                },
                "handler": self.github_client.update_file
            },
            "delete_file": {
                "description": "Delete a file from a repository",
                "parameters": {
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
                },
                "handler": self.github_client.delete_file
            },
            "get_branches": {
                "description": "Get all branches in a repository",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.get_branches
            },
            "create_branch": {
                "description": "Create a new branch",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "branch_name": {"type": "string"},
                        "source_sha": {"type": "string"}
                    },
                    "required": ["owner", "repo", "branch_name", "source_sha"]
                },
                "handler": self.github_client.create_branch
            },
            "delete_branch": {
                "description": "Delete a branch",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "branch_name": {"type": "string"}
                    },
                    "required": ["owner", "repo", "branch_name"]
                },
                "handler": self.github_client.delete_branch
            },

            # Issues & PRs
            "list_issues": {
                "description": "List issues in a repository with enhanced filtering",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                        "assignee": {"type": "string"},
                        "creator": {"type": "string"},
                        "mentioned": {"type": "string"},
                        "labels": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.list_issues
            },
            "create_issue": {
                "description": "Create a new issue",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "labels": {"type": "array", "items": {"type": "string"}},
                        "assignees": {"type": "array", "items": {"type": "string"}},
                        "milestone": {"type": "integer"}
                    },
                    "required": ["owner", "repo", "title"]
                },
                "handler": self.github_client.create_issue
            },
            "close_issue": {
                "description": "Close an issue",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "issue_number": {"type": "integer"}
                    },
                    "required": ["owner", "repo", "issue_number"]
                },
                "handler": self.github_client.close_issue
            },
            "update_issue": {
                "description": "Update an issue",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "issue_number": {"type": "integer"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "state": {"type": "string", "enum": ["open", "closed"]},
                        "labels": {"type": "array", "items": {"type": "string"}},
                        "assignees": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["owner", "repo", "issue_number"]
                },
                "handler": self.github_client.update_issue
            },
            "list_pull_requests": {
                "description": "List pull requests",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                        "head": {"type": "string"},
                        "base": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.list_pull_requests
            },
            "create_pull_request": {
                "description": "Create a new pull request",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "title": {"type": "string"},
                        "head": {"type": "string"},
                        "base": {"type": "string"},
                        "body": {"type": "string"},
                        "draft": {"type": "boolean", "default": False}
                    },
                    "required": ["owner", "repo", "title", "head", "base"]
                },
                "handler": self.github_client.create_pull_request
            },
            "merge_pull_request": {
                "description": "Merge a pull request",
                "parameters": {
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
                },
                "handler": self.github_client.merge_pull_request
            },

            # Actions
            "list_workflows": {
                "description": "List GitHub Actions workflows",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.list_workflows
            },
            "trigger_workflow": {
                "description": "Trigger a workflow",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "workflow_id": {"type": ["integer", "string"]},
                        "ref": {"type": "string"},
                        "inputs": {"type": "object"}
                    },
                    "required": ["owner", "repo", "workflow_id", "ref"]
                },
                "handler": self.github_client.trigger_workflow
            },
            "list_workflow_runs": {
                "description": "List workflow runs",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "workflow_id": {"type": ["integer", "string"]},
                        "branch": {"type": "string"},
                        "event": {"type": "string"},
                        "status": {"type": "string"}
                    },
                    "required": ["owner", "repo", "workflow_id"]
                },
                "handler": self.github_client.list_workflow_runs
            },
            "cancel_workflow_run": {
                "description": "Cancel a workflow run",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "run_id": {"type": "integer"}
                    },
                    "required": ["owner", "repo", "run_id"]
                },
                "handler": self.github_client.cancel_workflow_run
            },

            # Search
            "search_code": {
                "description": "Search code",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "sort": {"type": "string"},
                        "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"}
                    },
                    "required": ["query"]
                },
                "handler": self.github_client.search_code
            },
            "search_issues": {
                "description": "Search issues",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "sort": {"type": "string"},
                        "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"}
                    },
                    "required": ["query"]
                },
                "handler": self.github_client.search_issues
            },
            "search_repositories": {
                "description": "Search repositories",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "sort": {"type": "string"},
                        "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"}
                    },
                    "required": ["query"]
                },
                "handler": self.github_client.search_repositories
            },
            "search_users": {
                "description": "Search users",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "sort": {"type": "string"},
                        "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"}
                    },
                    "required": ["query"]
                },
                "handler": self.github_client.search_users
            },

            # Users & Organizations
            "get_user": {
                "description": "Get user information",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "username": {"type": "string"}
                    }
                },
                "handler": self.github_client.get_user
            },
            "list_user_orgs": {
                "description": "List organizations for a user",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "username": {"type": "string"}
                    }
                },
                "handler": self.github_client.list_user_orgs
            },
            "list_org_repos": {
                "description": "List organization repositories",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "org": {"type": "string"},
                        "type": {"type": "string", "enum": ["all", "public", "private", "forks", "sources", "member"], "default": "all"}
                    },
                    "required": ["org"]
                },
                "handler": self.github_client.list_org_repos
            },

            # Releases
            "list_releases": {
                "description": "List releases",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.list_releases
            },
            "create_release": {
                "description": "Create a release",
                "parameters": {
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
                },
                "handler": self.github_client.create_release
            },
            "upload_release_asset": {
                "description": "Upload a release asset",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "upload_url": {"type": "string"},
                        "asset_name": {"type": "string"},
                        "content": {"type": "string"},  # Base64 encoded
                        "content_type": {"type": "string", "default": "application/octet-stream"}
                    },
                    "required": ["upload_url", "asset_name", "content"]
                },
                "handler": self._upload_release_asset_wrapper
            },

            # Commits
            "list_commits": {
                "description": "List commits",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "sha": {"type": "string"},
                        "path": {"type": "string"},
                        "author": {"type": "string"},
                        "since": {"type": "string"},
                        "until": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.list_commits
            },
            "get_commit": {
                "description": "Get a commit",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "ref": {"type": "string"}
                    },
                    "required": ["owner", "repo", "ref"]
                },
                "handler": self.github_client.get_commit
            },

            # Collaborators
            "list_collaborators": {
                "description": "List collaborators",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "affiliation": {"type": "string", "enum": ["outside", "direct", "all"], "default": "all"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.list_collaborators
            },
            "add_collaborator": {
                "description": "Add a collaborator",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "username": {"type": "string"},
                        "permission": {"type": "string", "enum": ["pull", "triage", "push", "maintain", "admin"], "default": "push"}
                    },
                    "required": ["owner", "repo", "username"]
                },
                "handler": self.github_client.add_collaborator
            },
            "remove_collaborator": {
                "description": "Remove a collaborator",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "username": {"type": "string"}
                    },
                    "required": ["owner", "repo", "username"]
                },
                "handler": self.github_client.remove_collaborator
            },

            # Webhooks
            "list_hooks": {
                "description": "List webhooks",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.list_hooks
            },
            "create_hook": {
                "description": "Create a webhook",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "name": {"type": "string", "default": "web"},
                        "config": {"type": "object"},
                        "events": {"type": "array", "items": {"type": "string"}},
                        "active": {"type": "boolean", "default": True}
                    },
                    "required": ["owner", "repo", "config"]
                },
                "handler": self.github_client.create_hook
            },
            "delete_hook": {
                "description": "Delete a webhook",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "hook_id": {"type": "integer"}
                    },
                    "required": ["owner", "repo", "hook_id"]
                },
                "handler": self.github_client.delete_hook
            },

            # Projects
            "list_projects": {
                "description": "List projects",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.list_projects
            },
            "create_project": {
                "description": "Create a project",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "name": {"type": "string"},
                        "body": {"type": "string"}
                    },
                    "required": ["owner", "repo", "name"]
                },
                "handler": self.github_client.create_project
            },

            # Milestones
            "list_milestones": {
                "description": "List milestones",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.list_milestones
            },
            "create_milestone": {
                "description": "Create a milestone",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "due_on": {"type": "string"}
                    },
                    "required": ["owner", "repo", "title"]
                },
                "handler": self.github_client.create_milestone
            },

            # Labels
            "list_labels": {
                "description": "List labels",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.list_labels
            },
            "create_label": {
                "description": "Create a label",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "name": {"type": "string"},
                        "color": {"type": "string"},
                        "description": {"type": "string"}
                    },
                    "required": ["owner", "repo", "name", "color"]
                },
                "handler": self.github_client.create_label
            },
            "delete_label": {
                "description": "Delete a label",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "name": {"type": "string"}
                    },
                    "required": ["owner", "repo", "name"]
                },
                "handler": self.github_client.delete_label
            },

            # Gists
            "list_gists": {
                "description": "List gists",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "username": {"type": "string"}
                    }
                },
                "handler": self.github_client.list_gists
            },
            "create_gist": {
                "description": "Create a gist",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "files": {"type": "object"},
                        "public": {"type": "boolean", "default": True}
                    },
                    "required": ["description", "files"]
                },
                "handler": self.github_client.create_gist
            },
            "delete_gist": {
                "description": "Delete a gist",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "gist_id": {"type": "string"}
                    },
                    "required": ["gist_id"]
                },
                "handler": self.github_client.delete_gist
            },

            # Advanced repository management
            "transfer_repo": {
                "description": "Transfer repository ownership",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "new_owner": {"type": "string"}
                    },
                    "required": ["owner", "repo", "new_owner"]
                },
                "handler": self.github_client.transfer_repo
            },
            "archive_repo": {
                "description": "Archive a repository",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.archive_repo
            },
            "unarchive_repo": {
                "description": "Unarchive a repository",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.unarchive_repo
            },
            "enable_vulnerability_alerts": {
                "description": "Enable vulnerability alerts",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.enable_vulnerability_alerts
            },
            "disable_vulnerability_alerts": {
                "description": "Disable vulnerability alerts",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.disable_vulnerability_alerts
            },
            "enable_automated_security_fixes": {
                "description": "Enable automated security fixes",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.enable_automated_security_fixes
            },
            "disable_automated_security_fixes": {
                "description": "Disable automated security fixes",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                },
                "handler": self.github_client.disable_automated_security_fixes
            }
        }

    async def _upload_release_asset_wrapper(self, upload_url: str, asset_name: str, content: str, content_type: str = "application/octet-stream") -> Dict[str, Any]:
        """Wrapper to handle base64 decoding for release assets."""
        try:
            decoded_content = base64.b64decode(content)
            return await self.github_client.upload_release_asset(upload_url, asset_name, decoded_content, content_type)
        except Exception as e:
            return {"error": f"Failed to decode content: {str(e)}"}

    async def handle_sse(self, request: web.Request) -> web.StreamResponse:
        """Handle SSE connection."""
        response = web.StreamResponse(status=200, reason='OK')
        response.headers['Content-Type'] = 'text/event-stream'
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Connection'] = 'keep-alive'
        response.headers['Access-Control-Allow-Origin'] = '*'
        
        await response.prepare(request)
        
        # Send initialization message
        auth_status = self.github_client.auth_manager.get_auth_status()
        init_msg = {
            "type": "initialize",
            "capabilities": {
                "tools": list(self.tools.keys())
            },
            "auth_status": auth_status,
            "auth_methods": self.github_client.auth_manager.get_authentication_methods()
        }
        
        try:
            # Send initial message
            msg = f"data: {json.dumps(init_msg)}\\n\\n"
            await response.write(msg.encode('utf-8'))
            
            # Keep connection alive with periodic pings
            while True:
                await asyncio.sleep(25)  # Send ping every 25 seconds
                if not response.prepared:
                    break
                    
                ping_msg = f"data: {json.dumps({'type': 'ping'})}\\n\\n"
                await response.write(ping_msg.encode('utf-8'))
                
        except Exception as e:
            logger.error(f"Error in SSE connection: {e}")
            
        return response

    async def handle_tool_call(self, request: web.Request) -> web.Response:
        """Handle tool call request."""
        try:
            data = await request.json()
            tool_name = data.get("tool")
            arguments = data.get("arguments", {})
            
            if tool_name not in self.tools:
                return web.json_response({
                    "error": f"Tool '{tool_name}' not found",
                    "available_tools": list(self.tools.keys()),
                    "auth_status": self.github_client.auth_manager.get_auth_status()
                }, status=400)
            
            tool = self.tools[tool_name]
            try:
                result = await tool["handler"](**arguments)
                return web.json_response(result)
            except Exception as e:
                logger.error(f"Tool execution error in {tool_name}: {e}")
                return web.json_response({"error": str(e)}, status=500)
                
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.error(f"Request handling error: {e}")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def handle_pat_setup(self, request: web.Request) -> web.Response:
        """Handle PAT authentication setup."""
        try:
            data = await request.json()
            token = data.get("pat_token")
            account_name = data.get("account_name", "default")
            
            if token:
                self.github_client.auth_manager.add_pat(token, account_name)
                user_info = await self.github_client.auth_manager.get_user_info()
                
                return web.json_response({
                    "status": "success",
                    "message": f"PAT token added successfully for account: {account_name}",
                    "auth_status": self.github_client.auth_manager.get_auth_status(),
                    "user_info": user_info
                })
            else:
                return web.json_response({
                    "status": "error",
                    "message": "No PAT token provided"
                }, status=400)
                
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.error(f"PAT setup error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_app_token_setup(self, request: web.Request) -> web.Response:
        """Handle GitHub App installation token setup."""
        try:
            data = await request.json()
            token = data.get("token")
            installation_id = data.get("installation_id")
            account_name = data.get("account_name", "app_default")
            
            if token and installation_id:
                self.github_client.auth_manager.add_app_installation_token(token, installation_id, account_name)
                user_info = await self.github_client.auth_manager.get_user_info()
                
                return web.json_response({
                    "status": "success",
                    "message": f"App token added successfully for account: {account_name}",
                    "auth_status": self.github_client.auth_manager.get_auth_status(),
                    "user_info": user_info
                })
            else:
                return web.json_response({
                    "status": "error",
                    "message": "Token and installation_id are required"
                }, status=400)
                
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.error(f"App token setup error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_oauth_initiate(self, request: web.Request) -> web.Response:
        """Handle OAuth initiation with customizable scopes."""
        try:
            data = await request.json()
            scopes = data.get("scopes", ["repo", "user", "workflow", "gist"])
            result = await self.github_client.auth_manager.initiate_oauth_flow(scopes)
            return web.json_response(result)
        except Exception as e:
            logger.error(f"OAuth initiation error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_oauth_callback(self, request: web.Request) -> web.Response:
        """Handle OAuth callback."""
        try:
            query_params = request.rel_url.query
            code = query_params.get("code")
            state = query_params.get("state")
            
            if not code or not state:
                # Return HTML error page
                html_content = """
                <html>
                <head><title>GitHub Authentication Error</title></head>
                <body>
                    <h1>❌ Authentication Error</h1>
                    <p>Missing code or state parameter</p>
                </body>
                </html>
                """
                return web.Response(text=html_content, content_type="text/html")
            
            result = await self.github_client.auth_manager.handle_oauth_callback(code, state)
            
            # Return HTML page for browser
            if "status" in result and result["status"] == "success":
                html_content = """
                <html>
                <head><title>GitHub Authentication Complete</title></head>
                <body style="font-family: Arial, sans-serif; text-align: center; margin-top: 50px;">
                    <h1 style="color: green;">✅ Authentication Successful!</h1>
                    <p>You can now close this window and return to your MCP client.</p>
                    <p>GitHub MCP server is now authenticated and ready to use.</p>
                    <div style="margin-top: 30px;">
                        <button onclick="window.close()" style="padding: 10px 20px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer;">
                            Close Window
                        </button>
                    </div>
                </body>
                </html>
                """
                return web.Response(text=html_content, content_type="text/html")
            else:
                error_msg = result.get('error', 'Unknown error')
                html_content = f"""
                <html>
                <head><title>GitHub Authentication Failed</title></head>
                <body style="font-family: Arial, sans-serif; text-align: center; margin-top: 50px;">
                    <h1 style="color: red;">❌ Authentication Failed</h1>
                    <p>Error: {error_msg}</p>
                    <p>Please try again or use PAT authentication.</p>
                </body>
                </html>
                """
                return web.Response(text=html_content, content_type="text/html")
                
        except Exception as e:
            logger.error(f"OAuth callback error: {e}")
            html_content = f"""
            <html>
            <head><title>GitHub Authentication Error</title></head>
            <body style="font-family: Arial, sans-serif; text-align: center; margin-top: 50px;">
                <h1 style="color: red;">❌ Authentication Error</h1>
                <p>Error: {str(e)}</p>
            </body>
            </html>
            """
            return web.Response(text=html_content, content_type="text/html")

    async def handle_switch_account(self, request: web.Request) -> web.Response:
        """Handle switching between authenticated accounts."""
        try:
            data = await request.json()
            account_name = data.get("account_name")
            
            if account_name:
                try:
                    self.github_client.auth_manager.set_current_account(account_name)
                    return web.json_response({
                        "status": "success",
                        "message": f"Switched to account: {account_name}",
                        "auth_status": self.github_client.auth_manager.get_auth_status()
                    })
                except ValueError as e:
                    return web.json_response({
                        "status": "error",
                        "message": str(e)
                    }, status=400)
            else:
                return web.json_response({
                    "status": "error",
                    "message": "Account name is required"
                }, status=400)
                
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.error(f"Account switch error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_remove_account(self, request: web.Request) -> web.Response:
        """Handle removing an authenticated account."""
        try:
            data = await request.json()
            account_name = data.get("account_name")
            
            if account_name:
                try:
                    self.github_client.auth_manager.remove_account(account_name)
                    return web.json_response({
                        "status": "success",
                        "message": f"Removed account: {account_name}",
                        "auth_status": self.github_client.auth_manager.get_auth_status()
                    })
                except ValueError as e:
                    return web.json_response({
                        "status": "error",
                        "message": str(e)
                    }, status=400)
            else:
                return web.json_response({
                    "status": "error",
                    "message": "Account name is required"
                }, status=400)
                
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.error(f"Account removal error: {e}")
            return web.json_response({"error": str(e)}, status=500)


class EnhancedGitHubMCP:
    """Enhanced GitHub MCP server with full feature set."""

    def __init__(self):
        self.auth_manager = EnhancedGitHubAuthManager()
        self.github_client = EnhancedGitHubAPIClient(self.auth_manager)
        self.app = web.Application()
        self.protocol_engine = EnhancedMCPProtocolEngine(self.app, self.github_client)
        self.setup_routes()

    def setup_routes(self):
        """Setup HTTP routes."""
        self.app.router.add_get('/sse', self.protocol_engine.handle_sse)
        self.app.router.add_post('/tool_call', self.protocol_engine.handle_tool_call)
        
        # Authentication endpoints
        self.app.router.add_post('/auth/pat', self.protocol_engine.handle_pat_setup)
        self.app.router.add_post('/auth/app', self.protocol_engine.handle_app_token_setup)
        self.app.router.add_post('/auth/switch', self.protocol_engine.handle_switch_account)
        self.app.router.add_post('/auth/remove', self.protocol_engine.handle_remove_account)
        self.app.router.add_get('/auth/oauth', self.protocol_engine.handle_oauth_initiate)
        self.app.router.add_get('/auth/callback', self.protocol_engine.handle_oauth_callback)
        
        # Health check endpoint
        self.app.router.add_get('/health', self.health_check)

    async def health_check(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        auth_status = self.auth_manager.get_auth_status()
        return web.json_response({
            "status": "ok", 
            "service": "ENHANCED GitHub MCP",
            "version": "2.0",
            "auth_status": auth_status
        })

    async def start(self, host: str = "0.0.0.0", port: int = 9601):
        """Start the MCP server."""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"ENHANCED GitHub MCP Server started on {host}:{port}")
        
        # Add PAT from environment if available
        pat_token = os.environ.get("GITHUB_PAT")
        if pat_token:
            self.auth_manager.add_pat(pat_token)
            logger.info("GitHub PAT token loaded from environment")
        else:
            logger.warning("No GitHub PAT token provided. Set GITHUB_PAT environment variable.")
        
        # Keep the server running
        while True:
            await asyncio.sleep(3600)  # Sleep for an hour, then check again


async def main():
    """Main entry point."""
    # Initialize server
    server = EnhancedGitHubMCP()
    
    # Start server
    try:
        await server.start()
    except KeyboardInterrupt:
        logger.info("Server shutdown requested")
    except Exception as e:
        logger.error(f"Server error: {e}")


if __name__ == "__main__":
    asyncio.run(main())