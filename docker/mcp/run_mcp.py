"""HTTP/stdio launcher kept outside the runtime-cloned upstream checkout."""

import os

from gpt_researcher.mcp_profile_server import _get_job_manager, mcp


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise ValueError("MCP_TRANSPORT must be stdio, sse, or streamable-http")

    # Older upstream checkouts construct FastMCP with stdio defaults. Mutating
    # its settings here keeps HTTP deployment configurable without patching the
    # repository that is cloned at container start.
    mcp.settings.host = os.getenv("MCP_HOST", "0.0.0.0")
    mcp.settings.port = int(os.getenv("MCP_PORT", "8811"))
    _get_job_manager()
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
