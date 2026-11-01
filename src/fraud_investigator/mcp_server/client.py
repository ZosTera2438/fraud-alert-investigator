"""Strands MCP client for the tool server (stdio subprocess, or HTTP when FRAUD_MCP_URL is set)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mcp.client.stdio import StdioServerParameters, stdio_client
from strands.tools.mcp import MCPClient

from ..settings import Settings, project_root


def mcp_tool_provider(settings: Settings, db_path: Path) -> MCPClient:
    if url := os.environ.get("FRAUD_MCP_URL"):
        return MCPClient(url=url)
    command, *args = settings.tools.mcp_command
    if command == "python":
        command = sys.executable
    params = StdioServerParameters(
        command=command,
        args=args,
        cwd=str(project_root()),
        env={**os.environ, "FRAUD_DATABASE": str(db_path), "FRAUD_ROOT": str(project_root())},
    )
    return MCPClient(lambda: stdio_client(params), startup_timeout=60)  # type: ignore[arg-type]
