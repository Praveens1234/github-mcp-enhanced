# GitHub MCP Enhanced

This repository contains an enhanced version of the GitHub MCP server with additional batch operations capabilities.

## Features

- Full GitHub API integration
- Batch operations for efficient processing
- Directory scanning and synchronization
- Multiple file upload capabilities
- Authentication management

## Setup

1. Clone the repository
2. Install dependencies: `pip install mcp starlette uvicorn httpx aiofiles`
3. Configure credentials in `credentials.json`
4. Run the server: `python server.py`

## Usage

The server exposes various tools for interacting with GitHub, including:

- Repository management
- Issue tracking
- Pull request handling
- Workflow management
- Batch operations (new)

For more details on available tools, see the server.py file.

## Batch Operations Tools

This enhanced version includes several new batch operations tools:

1. **scan_local_directory** - Scan and analyze local directory structure
2. **read_multiple_files** - Read content of multiple files in bulk
3. **upload_directory_to_github** - Upload an entire local directory to a GitHub repository
4. **upload_multiple_directories_to_github** - Upload multiple local directories to different paths
5. **sync_local_directory_with_github** - Synchronize a local directory with a GitHub repository
6. **sync_multiple_directories_with_github** - Synchronize multiple local directories
7. **get_batch_operation_status** - Track batch operation progress
8. **cancel_batch_operation** - Cancel ongoing batch operations

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the LICENSE file for details.