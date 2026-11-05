import os
import sys

# Import the single source of truth: the screen_server.py that lives two
# levels up (this package's folder is mcp-servers/screen-vision-mcp/).
_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import screen_server as _server  # noqa: E402


def main():
    """Console entry point: run the MCP server over stdio."""
    _server.mcp.run(transport="stdio")
