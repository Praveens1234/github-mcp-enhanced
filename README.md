# Enhanced GitHub MCP

An advanced Model Context Protocol (MCP) server for comprehensive GitHub integration with extended functionality beyond the standard GitHub MCP.

## Features

- **Enhanced Repository Management**: Create, delete, list, and manage repositories with advanced options
- **File Operations**: Create, update, and delete files in repositories
- **Branch Management**: Create and delete branches
- **Issues & Pull Requests**: Full lifecycle management for issues and PRs
- **GitHub Actions**: List workflows, trigger runs, and manage workflow executions
- **Search Capabilities**: Search code, issues, repositories, and users
- **User & Organization Management**: Get user info and list organizational repositories
- **Release Management**: Create releases and upload assets
- **Commit Operations**: List commits and get specific commit details
- **Collaborator Management**: Add and remove repository collaborators
- **Webhook Management**: Create and delete repository webhooks
- **Project Management**: List and create projects
- **Milestone Management**: List and create milestones
- **Label Management**: List, create, and delete labels
- **Gist Operations**: List, create, and delete gists
- **Advanced Repository Features**: Transfer ownership, archive/unarchive, and manage security alerts

## Prerequisites

- Python 3.8 or higher
- GitHub Personal Access Token (PAT) with appropriate scopes

## Installation

1. Clone the repository:
```bash
git clone https://github.com/Praveens1234/github-mcp-enhanced.git
cd github-mcp-enhanced
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Configuration

Set your GitHub Personal Access Token as an environment variable:
```bash
export GITHUB_PAT=your_github_pat_here
```

## Usage

1. Start the server:
```bash
python server.py
```

2. Connect your MCP client to:
```
http://localhost:9601/sse
```

## Available Tools

### Repository Tools
- `list_repos`: List repositories with enhanced filtering
- `get_repo`: Get repository details
- `create_repo`: Create a new repository with additional options
- `delete_repo`: Delete a repository
- `get_repo_contents`: Get repository contents
- `create_file`: Create a new file in a repository
- `update_file`: Update an existing file in a repository
- `delete_file`: Delete a file from a repository
- `get_branches`: Get all branches in a repository
- `create_branch`: Create a new branch
- `delete_branch`: Delete a branch

### Issues & Pull Requests
- `list_issues`: List issues in a repository with enhanced filtering
- `create_issue`: Create a new issue
- `close_issue`: Close an issue
- `update_issue`: Update an issue
- `list_pull_requests`: List pull requests
- `create_pull_request`: Create a new pull request
- `merge_pull_request`: Merge a pull request

### Actions
- `list_workflows`: List GitHub Actions workflows
- `trigger_workflow`: Trigger a workflow
- `list_workflow_runs`: List workflow runs
- `cancel_workflow_run`: Cancel a workflow run

### Search
- `search_code`: Search code
- `search_issues`: Search issues
- `search_repositories`: Search repositories
- `search_users`: Search users

### Users & Organizations
- `get_user`: Get user information
- `list_user_orgs`: List organizations for a user
- `list_org_repos`: List organization repositories

### Releases
- `list_releases`: List releases
- `create_release`: Create a release
- `upload_release_asset`: Upload a release asset

### Commits
- `list_commits`: List commits
- `get_commit`: Get a commit

### Collaborators
- `list_collaborators`: List collaborators
- `add_collaborator`: Add a collaborator
- `remove_collaborator`: Remove a collaborator

### Webhooks
- `list_hooks`: List webhooks
- `create_hook`: Create a webhook
- `delete_hook`: Delete a webhook

### Projects
- `list_projects`: List projects
- `create_project`: Create a project

### Milestones
- `list_milestones`: List milestones
- `create_milestone`: Create a milestone

### Labels
- `list_labels`: List labels
- `create_label`: Create a label
- `delete_label`: Delete a label

### Gists
- `list_gists`: List gists
- `create_gist`: Create a gist
- `delete_gist`: Delete a gist

### Advanced Repository Management
- `transfer_repo`: Transfer repository ownership
- `archive_repo`: Archive a repository
- `unarchive_repo`: Unarchive a repository
- `enable_vulnerability_alerts`: Enable vulnerability alerts
- `disable_vulnerability_alerts`: Disable vulnerability alerts
- `enable_automated_security_fixes`: Enable automated security fixes
- `disable_automated_security_fixes`: Disable automated security fixes

## Authentication

The server supports multiple authentication methods:
- **Personal Access Token (PAT)**: Set the `GITHUB_PAT` environment variable
- **GitHub App Installation Tokens**: Use the `/auth/app` endpoint
- **OAuth Flow**: Initiate OAuth through the `/auth/oauth` endpoint

## Security

- Store your GitHub PAT securely and never commit it to version control
- Use the principle of least privilege when creating PATs
- Regularly rotate your tokens
- Monitor API usage and rate limits

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.