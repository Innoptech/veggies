"""Toolbox MCP server: the pure tool functions (no fastmcp needed - the
server wiring lives in main() precisely so tests stay dependency-free)."""

import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "deploy" / "mcp" / "toolbox.py"
_spec = importlib.util.spec_from_file_location("mcp_toolbox_server", _SRC)
toolbox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(toolbox)


def test_current_time_utc():
    stamp = toolbox.current_time()
    assert stamp.endswith("+00:00")
    assert "T" in stamp


def test_current_time_timezone():
    stamp = toolbox.current_time("America/Toronto")
    assert stamp.endswith(("-04:00", "-05:00"))  # EDT or EST
    with pytest.raises(Exception):
        toolbox.current_time("Not/AZone")


def test_roll_dice_bounds():
    rolls = toolbox.roll_dice(sides=6, count=50)
    assert len(rolls) == 50
    assert all(1 <= r <= 6 for r in rolls)


def test_roll_dice_validation():
    with pytest.raises(ValueError):
        toolbox.roll_dice(sides=1)
    with pytest.raises(ValueError):
        toolbox.roll_dice(count=0)
    with pytest.raises(ValueError):
        toolbox.roll_dice(count=101)
