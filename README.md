# GitHub MCP Enhanced Server

An advanced GitHub integration server built with the Model Context Protocol (MCP) that provides comprehensive access to GitHub's API through standardized tools. This server enables automation, batch operations, and seamless integration with GitHub repositories.

## Features

- Complete GitHub API integration through MCP tools
- Advanced batch operations for efficient processing of multiple files and directories
- Directory scanning and synchronization capabilities
- Authentication management with support for multiple identities
- Real-time logging and monitoring
- Rate limit tracking and management

## Prerequisites

- Python 3.8+
- GitHub Personal Access Token with appropriate permissions

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Praveens1234/github-mcp-enhanced.git
   cd github-mcp-enhanced
   ```

2. Install dependencies:
   ```bash
   pip install mcp starlette uvicorn httpx aiofiles
   ```

3. Configure credentials:
   Create a `credentials.json` file with your GitHub token:
   ```json
   {
     "identities": [
       {
         "id": "your_github_username",
         "type": "pat",
         "token": "your_personal_access_token_here"
       }
     ]
   }
   ```

## Usage

Start the server:
```bash
python server.py
```

The server will start on `http://0.0.0.0:8001` with SSE transport at `/sse` and message handling at `/messages`.

## Tool Categories

### Repository Management
Tools for managing repositories including creation, deletion, archiving, and transferring repositories.

### Organization Tools
Tools for retrieving organization information.

### Commit Tools
Tools for listing and retrieving commit information.

### Issue Management
Tools for creating, listing, updating issues and adding comments.

### Pull Request Tools
Tools for listing, creating, and merging pull requests.

### Workflow Automation
Tools for managing GitHub Actions workflows, runs, and triggers.

### Search Capabilities
Tools for searching code, issues, repositories, and users.

### Collaboration Tools
Tools for managing collaborators, webhooks, and gists.

### Release Management
Tools for creating releases and managing security settings.

### Project & Milestone Tools
Tools for managing projects and milestones.

### Label Management
Tools for creating, listing, and deleting labels.

### Batch Operations (Enhanced Features)
Advanced tools for bulk processing of files and directories:
1. `scan_local_directory` - Analyze local directory structures
2. `read_multiple_files` - Read multiple files efficiently
3. `upload_directory_to_github` - Upload entire directories
4. `upload_multiple_directories_to_github` - Upload multiple directories
5. `sync_local_directory_with_github` - Synchronize directories
6. `sync_multiple_directories_with_github` - Synchronize multiple directories
7. `get_batch_operation_status` - Track batch operation progress
8. `cancel_batch_operation` - Cancel ongoing operations

## Complete Tool Reference

### Repository Tools

#### `list_repositories`
List repositories for the authenticated user or an organization.

Parameters:
- `visibility` (string): Repository visibility (`all`, `public`, `private`) - default: `all`
- `sort` (string): Sort order - default: `updated`
- `org` (string): Organization name (optional)

#### `create_repository`
Create a new repository.

Parameters:
- `name` (string): Repository name (required)
- `description` (string): Repository description
- `private` (boolean): Whether repository is private - default: `false`
- `auto_init` (boolean): Initialize with README - default: `true`
- `org` (string): Organization to create in (optional)

#### `get_repository`
Get details of a specific repository.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)

#### `delete_repository`
Delete a repository.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)

#### `transfer_repository`
Transfer a repository.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `new_owner` (string): New owner username (required)

#### `update_repository_archive`
Archive or unarchive a repository.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `archived` (boolean): Archive status (required)

#### `create_branch`
Create a branch (ref).

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `ref` (string): Branch reference (required)
- `sha` (string): SHA1 value for the reference (required)

#### `delete_branch`
Delete a branch (ref).

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `ref` (string): Branch reference (required)

### Organization Tools

#### `get_organization`
Get organization information.

Parameters:
- `org` (string): Organization name (required)

### Commit Tools

#### `list_commits`
List commits on a repository.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `sha` (string): SHA or branch to start listing from
- `path` (string): Filter by file path
- `author` (string): Filter by author
- `since` (string): Filter commits since date
- `until` (string): Filter commits until date

#### `get_commit`
Get a specific commit.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `ref` (string): Commit SHA (required)

### Issue Tools

#### `list_issues`
List issues in a repository.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `state` (string): Issue state (`open`, `closed`, `all`) - default: `open`
- `labels` (string): Filter by labels
- `sort` (string): Sort order - default: `created`
- `direction` (string): Sort direction - default: `desc`

#### `create_issue`
Create an issue.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `title` (string): Issue title (required)
- `body` (string): Issue body
- `assignees` (array): Assignee usernames
- `labels` (array): Labels to apply

#### `update_issue`
Update an issue.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `issue_number` (integer): Issue number (required)
- `title` (string): New title
- `body` (string): New body
- `state` (string): New state (`open`, `closed`)
- `labels` (array): New labels

#### `create_issue_comment`
Create a comment on an issue or PR.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `issue_number` (integer): Issue number (required)
- `body` (string): Comment body (required)

### Pull Request Tools

#### `list_pull_requests`
List pull requests.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `state` (string): PR state (`open`, `closed`, `all`) - default: `open`
- `head` (string): Filter by head user/branch
- `base` (string): Filter by base branch

#### `create_pull_request`
Create a pull request.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `title` (string): PR title (required)
- `head` (string): Head branch (required)
- `base` (string): Base branch (required)
- `body` (string): PR description
- `draft` (boolean): Create as draft

#### `merge_pull_request`
Merge a pull request.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `pull_number` (integer): PR number (required)
- `commit_title` (string): Commit title
- `commit_message` (string): Commit message
- `merge_method` (string): Merge method (`merge`, `squash`, `rebase`) - default: `merge`

### Workflow Tools

#### `list_workflows`
List GitHub Actions workflows.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)

#### `list_workflow_runs`
List workflow runs.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `workflow_id` (string): Workflow ID or filename (required)
- `status` (string): Filter by status
- `event` (string): Filter by event type

#### `get_workflow_run`
Get a specific workflow run.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `run_id` (integer): Run ID (required)

#### `cancel_workflow_run`
Cancel a workflow run.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `run_id` (integer): Run ID (required)

#### `trigger_workflow_dispatch`
Trigger a workflow dispatch event.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `workflow_id` (string): Workflow ID or filename (required)
- `ref` (string): Reference (required)
- `inputs` (object): Input parameters

### Search Tools

#### `search_code`
Search for code.

Parameters:
- `q` (string): Search query (required)
- `sort` (string): Sort field
- `order` (string): Sort order

#### `search_issues`
Search for issues and pull requests.

Parameters:
- `q` (string): Search query (required)
- `sort` (string): Sort field
- `order` (string): Sort order

#### `search_repositories`
Search for repositories.

Parameters:
- `q` (string): Search query (required)
- `sort` (string): Sort field
- `order` (string): Sort order

#### `search_users`
Search for users.

Parameters:
- `q` (string): Search query (required)
- `sort` (string): Sort field
- `order` (string): Sort order

### User Tools

#### `get_user`
Get user information.

Parameters:
- `username` (string): Username (optional, defaults to authenticated user)

### Collaborator Tools

#### `list_collaborators`
List collaborators on a repository.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `affiliation` (string): Affiliation filter - default: `all`

#### `add_collaborator`
Add a collaborator.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `username` (string): Username to add (required)
- `permission` (string): Permission level (`pull`, `push`, `admin`, `maintain`, `triage`) - default: `push`

#### `remove_collaborator`
Remove a collaborator.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `username` (string): Username to remove (required)

### Webhook Tools

#### `list_webhooks`
List webhooks for a repository.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)

#### `create_webhook`
Create a webhook.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `url` (string): Webhook URL (required)
- `content_type` (string): Content type (`json`, `form`) - default: `json`
- `events` (array): Event types - default: `["push"]`
- `active` (boolean): Active status - default: `true`
- `secret` (string): Secret for signature verification

#### `delete_webhook`
Delete a webhook.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `hook_id` (integer): Webhook ID (required)

### Gist Tools

#### `list_gists`
List gists.

Parameters:
- `username` (string): Username to list gists for (optional)

#### `create_gist`
Create a gist.

Parameters:
- `description` (string): Gist description
- `files` (object): Map of filename to content (required)
- `public` (boolean): Public visibility - default: `false`

#### `delete_gist`
Delete a gist.

Parameters:
- `gist_id` (string): Gist ID (required)

### Release Tools

#### `create_release`
Create a release.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `tag_name` (string): Tag name (required)
- `name` (string): Release name
- `body` (string): Release description
- `draft` (boolean): Create as draft - default: `false`
- `prerelease` (boolean): Mark as prerelease - default: `false`

### Security Tools

#### `enable_vulnerability_alerts`
Enable vulnerability alerts for a repository.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)

#### `disable_vulnerability_alerts`
Disable vulnerability alerts for a repository.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)

#### `enable_automated_security_fixes`
Enable automated security fixes.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)

#### `disable_automated_security_fixes`
Disable automated security fixes.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)

### Project Tools

#### `list_projects`
List projects (classic).

Parameters:
- `owner` (string): Organization or user (required)
- `repo` (string): Repository name (optional)
- `state` (string): Project state (`open`, `closed`, `all`) - default: `open`

#### `create_project`
Create a project (classic).

Parameters:
- `owner` (string): Organization or user (required)
- `repo` (string): Repository name (optional)
- `name` (string): Project name (required)
- `body` (string): Project description

### Milestone Tools

#### `list_milestones`
List milestones.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `state` (string): Milestone state (`open`, `closed`, `all`) - default: `open`
- `sort` (string): Sort field - default: `due_date`
- `direction` (string): Sort direction - default: `asc`

#### `create_milestone`
Create a milestone.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `title` (string): Milestone title (required)
- `state` (string): Milestone state - default: `open`
- `description` (string): Milestone description
- `due_on` (string): Due date (ISO 8601 timestamp)

### Label Tools

#### `list_labels`
List labels.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)

#### `create_label`
Create a label.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `name` (string): Label name (required)
- `color` (string): Color code (6 characters, without #) (required)
- `description` (string): Label description

#### `delete_label`
Delete a label.

Parameters:
- `owner` (string): Repository owner (required)
- `repo` (string): Repository name (required)
- `name` (string): Label name (required)

### Batch Operations Tools

#### `scan_local_directory`
Scan and analyze a local directory structure for batch operations.

Parameters:
- `path` (string): Local directory path to scan (required)
- `recursive` (boolean): Scan subdirectories - default: `true`
- `include_hidden` (boolean): Include hidden files - default: `false`
- `exclude_patterns` (array): Patterns to exclude - default: `[]`
- `max_files` (integer): Maximum files to scan - default: `1000`
- `file_info_level` (string): Detail level (`basic`, `detailed`, `full`) - default: `detailed`

#### `read_multiple_files`
Read content of multiple files in bulk and return Base64 encoded content.

Parameters:
- `paths` (array): Array of file paths to read (required)
- `max_total_size` (integer): Max total size in bytes (50MB) - default: `50000000`
- `continue_on_error` (boolean): Continue on error - default: `true`

#### `upload_directory_to_github`
Upload an entire local directory to a GitHub repository path in a single atomic operation.

Parameters:
- `local_path` (string): Local directory path to upload (required)
- `owner` (string): GitHub repository owner (required)
- `repo` (string): GitHub repository name (required)
- `commit_message` (string): Commit message for the upload (required)
- `repo_path` (string): Target path in repository - default: `""`
- `branch` (string): Target branch - default: `"main"`
- `author_name` (string): Commit author name
- `author_email` (string): Commit author email
- `exclude_patterns` (array): File patterns to exclude - default: `[]`
- `include_hidden` (boolean): Include hidden files - default: `false`
- `force_overwrite` (boolean): Force overwrite existing files - default: `false`
- `dry_run` (boolean): Perform dry run - default: `false`

#### `upload_multiple_directories_to_github`
Upload multiple local directories to different paths in a GitHub repository.

Parameters:
- `directory_mappings` (array): Array of local to repository path mappings (required)
- `owner` (string): GitHub repository owner (required)
- `repo` (string): GitHub repository name (required)
- `commit_message` (string): Commit message for all uploads (required)
- `branch` (string): Target branch - default: `"main"`
- `author_name` (string): Commit author name
- `author_email` (string): Commit author email
- `exclude_patterns` (array): File patterns to exclude - default: `[]`
- `include_hidden` (boolean): Include hidden files - default: `false`
- `dry_run` (boolean): Perform dry run - default: `false`

#### `sync_local_directory_with_github`
Synchronize a local directory with a GitHub repository path (add, update, delete files).

Parameters:
- `local_path` (string): Local directory path to sync (required)
- `owner` (string): GitHub repository owner (required)
- `repo` (string): GitHub repository name (required)
- `repo_path` (string): Target path in repository - default: `""`
- `branch` (string): Target branch - default: `"main"`
- `commit_message_add` (string): Commit message for additions - default: `"Add new files"`
- `commit_message_update` (string): Commit message for updates - default: `"Update existing files"`
- `commit_message_delete` (string): Commit message for deletions - default: `"Remove deleted files"`
- `author_name` (string): Commit author name
- `author_email` (string): Commit author email
- `exclude_patterns` (array): File patterns to exclude - default: `[]`
- `include_hidden` (boolean): Include hidden files - default: `false`
- `delete_remote_files` (boolean): Delete remote files not present locally - default: `false`
- `dry_run` (boolean): Perform dry run - default: `false`

#### `sync_multiple_directories_with_github`
Synchronize multiple local directories with different paths in a GitHub repository.

Parameters:
- `sync_mappings` (array): Array of sync mappings (required)
- `owner` (string): GitHub repository owner (required)
- `repo` (string): GitHub repository name (required)
- `branch` (string): Target branch - default: `"main"`
- `commit_message_template` (string): Commit message template - default: `"Sync {local_path} to {repo_path}"`
- `author_name` (string): Commit author name
- `author_email` (string): Commit author email
- `exclude_patterns` (array): File patterns to exclude - default: `[]`
- `include_hidden` (boolean): Include hidden files - default: `false`
- `dry_run` (boolean): Perform dry run - default: `false`

#### `get_batch_operation_status`
Get status and progress of ongoing batch operations.

Parameters:
- `operation_id` (string): Batch operation identifier (optional, returns all if not provided)

#### `cancel_batch_operation`
Cancel an ongoing batch operation.

Parameters:
- `operation_id` (string): Batch operation identifier (required)

## Logging

The server maintains detailed logs in:
- `github_server.log` - General application logs
- `github_server_debug.log` - Debug-level logs

## Health Check

A health check endpoint is available at `/health` which provides information about:
- Server status
- Authentication status
- Rate limit remaining

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the LICENSE file for details.