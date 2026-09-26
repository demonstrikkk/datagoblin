# MCP — FUTURE external interface (never core pipeline)

Verified 2026-09-24: https://github.com/modelcontextprotocol/python-sdk (v2.1.1, py≥3.10, stdio/StreamableHTTP/SSE) + https://py.sdk.modelcontextprotocol.io/ ; `uv add "mcp[cli]"`
```python
from mcp.server import MCPServer
mcp = MCPServer("Demo")
@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b
```
Role Phase 3: expose search_dataset/inspect_evidence/run_collection/export as tools/resources for Claude/Inspector. Core stays direct Python. Add auth + read-only scopes + audit.
