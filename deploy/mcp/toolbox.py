"""veggies toolbox MCP server (ADR 0018 reference implementation).

Deliberately trivial: zero egress, zero secrets. Pure functions at module
level (pytest imports them without fastmcp installed); the FastMCP wiring
lives in main() so the import stays dependency-free.

Runs inside the pod on pod loopback only - never published to the host.
"""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

TOOLBOX_PORT = 7000


def current_time(timezone: str = "UTC") -> str:
    """Current date and time in the given IANA timezone."""
    return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")


def roll_dice(sides: int = 6, count: int = 1) -> list[int]:
    """Roll `count` dice with `sides` faces each; return the results."""
    if not 1 <= count <= 100:
        raise ValueError("count must be between 1 and 100")
    if not 2 <= sides <= 1000:
        raise ValueError("sides must be between 2 and 1000")
    return [random.randint(1, sides) for _ in range(count)]


def main() -> None:
    from fastmcp import FastMCP  # image dependency, not a test dependency

    mcp = FastMCP("veggies-toolbox")
    mcp.tool(current_time)
    mcp.tool(roll_dice)
    # 0.0.0.0 inside the pod netns (pod loopback must reach it); the port
    # is never published to the host.
    mcp.run(transport="http", host="0.0.0.0", port=TOOLBOX_PORT, path="/mcp")


if __name__ == "__main__":
    main()
