"""Print the raw CallToolResult for one tool call - used while developing."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def main() -> None:
    tool = sys.argv[1] if len(sys.argv) > 1 else "gdb_start"
    args = json_args = None
    if len(sys.argv) > 2:
        import json

        json_args = json.loads(sys.argv[2])
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gdb_mcp.server"],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, json_args or {})
            print("isError:", getattr(result, "isError", None))
            print("structured:", getattr(result, "structuredContent", "MISSING"))
            print("content blocks:", result.content)
            print("meta:", getattr(result, "meta", None))


if __name__ == "__main__":
    asyncio.run(main())
