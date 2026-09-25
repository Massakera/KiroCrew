"""App routes; identities come from the gateway, never from request bodies."""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.apps.route_registry import AppRoute
from kiro_crew.dashboard.handlers._shared import private_owner_surface_refusal
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.session_control import (
    SessionControlError,
    _refuse_ineligible_creator,
    caller_slot_key,
)
from kiro_crew.investigation_policy import APP_NAME
from kiro_crew.investigations import Engine, InvestigationError


def register_routes(ctx):
    # One engine per enabled app, initialized under a lock after authentication.
    engine = None
    lock = asyncio.Lock()

    async def handle(request, context):
        nonlocal engine
        state = request.app["state"]
        caller = None
        internal = request.get("internal_auth") is True
        if internal:
            refusal = await private_owner_surface_refusal(request, "investigations")
            if refusal is not None:
                return refusal
            key = caller_slot_key(state, request.headers.get("X-Session-Key", ""))
            caller = state.get_slot(key) if key else None
            if caller is None:
                raise web.HTTPForbidden(reason="A live calling session is required.")
        elif not is_owner_dashboard_request(request) and request.get("app") != APP_NAME:
            raise web.HTTPForbidden(reason="Only the owner or this app can use investigations.")
        async with lock:
            if engine is None:
                engine = Engine(state, context.data_dir)
        try:
            body = await request.json() if request.method == "POST" else {"action": "list"}
            if not isinstance(body, dict):
                raise InvestigationError("Expected a JSON object.")
            action = body.get("action", "list")
            own = engine.bound_run(caller) if caller is not None else None
            if caller is not None and own is None:
                _refuse_ineligible_creator(state, caller)
            if own is not None and action not in {"status", "report"}:
                raise web.HTTPForbidden(
                    reason="Investigators can only read and report their own run."
                )
            if action == "list":
                return web.json_response(
                    {
                        "services": engine.services(),
                        "runs": [
                            engine.view(row)
                            for row in sorted(
                                engine.runs.values(), key=lambda r: r["updated_at"], reverse=True
                            )
                        ],
                    }
                )
            if action == "save_service":
                if internal:
                    raise web.HTTPForbidden(
                        reason="Configure service access in the investigation page."
                    )
                return web.json_response(engine.save_service(body.get("service", {})))
            if action == "start":
                started = await engine.start(
                    body.get("service_id", ""),
                    body.get("question", ""),
                    caller.key if caller else "",
                )
                return web.json_response(started)
            row = engine.runs.get(body.get("id", ""))
            if row is None or (own is not None and own is not row):
                raise web.HTTPNotFound(reason="Investigation not found.")
            if action == "status":
                return web.json_response(engine.view(row))
            if action == "report":
                if own is not row or row["status"] in {"cancelled", "interrupted", "failed"}:
                    raise web.HTTPForbidden(
                        reason="Only the active investigator can report findings."
                    )
                engine.report(row, body.get("report", {}), body.get("status", "running"))
            elif action == "cancel":
                await engine.cancel(row)
            elif action in {"resume", "reconnect"}:
                if action == "reconnect" and internal:
                    raise web.HTTPForbidden(reason="Start AWS sign-in from the investigation page.")
                await engine.resume(row, reconnect=action == "reconnect")
            elif action == "open":
                # Restoring a transcript does not grant automatic diagnostic access.
                # A resumed run re-verifies the target before binding its slot.
                from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

                await rehydrate_slot_from_history_async(state, row["slot_key"], adopt_closed=True)
            else:
                raise InvestigationError("Unknown investigation action.")
            return web.json_response(engine.view(row))
        except (InvestigationError, SessionControlError, TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    return [AppRoute("GET", "/investigations", handle), AppRoute("POST", "/investigations", handle)]
