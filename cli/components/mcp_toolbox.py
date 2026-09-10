"""MCP sidecar: toolbox (ADR 0018 reference implementation).

Zero-egress, zero-secret FastMCP server (current_time, roll_dice) serving
streamable HTTP on pod loopback. Proves the whole MCP loop - component,
pod wiring, opencode.json mcp: block, tool call - before real MCPs
(sonarqube et al.) join as config-only adds. The server code ships as a
stack-config file (ADR 0018), so the image
is nothing but python + fastmcp."""

from __future__ import annotations

from capabilities import (
    HARDENED,
    BuildSpec,
    Component,
    PodContext,
)

# Derived image (python + pinned fastmcp); base pinned by tag+digest.
IMAGE_TOOLBOX = "localhost/veggies-mcp-toolbox:4.0.3"
_TOOLBOX_PORT = 7000  # pod loopback only, never published


def _render(ctx: PodContext) -> dict:
    return {
        "name": "toolbox",
        "image": IMAGE_TOOLBOX,
        "args": ["python", "/stack-config/mcp-toolbox-server.py"],
        "volumeMounts": [
            {"name": "stack-config", "mountPath": "/stack-config", "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": "128Mi"}},
        "securityContext": HARDENED,
        "livenessProbe": {
            "exec": {"command": ["sh", "-c",
                                 f"python -c \"import socket; socket.create_connection(('127.0.0.1', {_TOOLBOX_PORT}), 3)\" || exit 1"]},
            "initialDelaySeconds": 10,
            "periodSeconds": 30,
        },
    }


def _volumes(ctx: PodContext) -> list[dict]:
    return []  # stack-config is squid's declaration (first declaration wins)


def _mcp_entry(ctx: PodContext) -> dict:
    return {"type": "remote",
            "url": f"http://127.0.0.1:{_TOOLBOX_PORT}/mcp",
            "enabled": True}


def _config_files(ctx: PodContext) -> dict[str, str]:
    src = ctx.infra_repo / "deploy" / "mcp" / "toolbox.py"
    return {"mcp-toolbox-server.py": src.read_text()}


COMPONENT = Component(
    name="toolbox",
    provides="mcp-toolbox",
    requires=(),
    render=_render,
    volumes=_volumes,
    config_files=_config_files,
    mcp_entry=_mcp_entry,
    build=BuildSpec(IMAGE_TOOLBOX, "deploy/images/mcp-toolbox.Containerfile"),
)
