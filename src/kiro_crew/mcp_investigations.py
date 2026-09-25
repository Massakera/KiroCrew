"""Stateless MCP client for the optional local investigations extension."""

from __future__ import annotations

import json

from kiro_crew.mcp_core import _post, require_strict_session_key
from kiro_crew.mcp_shared import run_mcp_stdio_loop

SERVER_NAME = "kirocrew-investigations"


def list_tools() -> list[dict]:
    return [
        {
            "name": "investigation",
            "description": (
                "Start an independent local service investigation while this conversation continues. "
                "List saved services and investigations; start with service_id and question; "
                "status/cancel/resume with id. Returns a durable ID and page URL. "
                "Investigation agents use report on their own ID with text fields summary, evidence, "
                "hypotheses, gaps, recommendation, decisions; status running/completed/waiting_auth/needs_attention. "
                "Reads are reviewed automatically; effective changes require native approval. "
                "Service access is configured by the owner in the extension."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["action"],
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "start", "status", "cancel", "resume", "report"],
                    },
                    "id": {"type": "string"},
                    "service_id": {"type": "string"},
                    "question": {"type": "string"},
                    "status": {"type": "string"},
                    "report": {"type": "object", "additionalProperties": {"type": "string"}},
                },
            },
        }
    ]


def call_tool(name: str, args: dict) -> str:
    if name != "investigation":
        return json.dumps({"error": "Unknown tool."})
    key, error = require_strict_session_key(
        "A gateway-issued session identity is required.", server=SERVER_NAME
    )
    if not key:
        return json.dumps({"error": error})
    payload = _post("/api/apps/service-investigations/investigations", args, session_key=key)
    if "error" not in payload:
        if args.get("action") == "list":
            payload = {
                "services": [
                    {"id": s["id"], "name": s["name"]} for s in payload.get("services", [])
                ],
                "runs": [
                    {k: row[k] for k in ("id", "question", "status", "url")}
                    for row in payload.get("runs", [])[:30]
                ],
                "total": len(payload.get("runs", [])),
            }
        elif args.get("action") == "status":
            payload = {
                k: payload[k] for k in ("id", "question", "status", "url", "report", "identity")
            }
        else:
            payload = {k: payload[k] for k in ("id", "status", "url") if k in payload}
    return json.dumps(payload, ensure_ascii=False)


if __name__ == "__main__":
    run_mcp_stdio_loop(SERVER_NAME, "1.0.0", list_tools, call_tool, advertise_caller_identity=True)
