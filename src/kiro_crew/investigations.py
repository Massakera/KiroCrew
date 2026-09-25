"""Local, durable investigations shared by the App Kit page and its MCP tool."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from kiro_crew.investigation_policy import APP_NAME, register_engine
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


class InvestigationError(ValueError):
    pass


def clean(value: str) -> str:
    value, _ = redact_credentials(value)
    value, _ = redact_exfiltration_urls(value)
    return value


def service_config(data: dict) -> dict:
    fields = (
        "name",
        "repository",
        "aws_profile",
        "aws_account",
        "kube_context",
        "kube_server",
        "namespaces",
        "log_sources",
        "database",
        "instructions",
    )
    result = {key: str(data.get(key, "")).strip() for key in fields}
    if not result["name"] or len(result["name"]) > 100:
        raise InvestigationError("Service name is required (up to 100 characters).")
    if any(len(value) > 12000 for value in result.values()):
        raise InvestigationError("Service context is too large.")
    if clean(json.dumps(result)) != json.dumps(result):
        raise InvestigationError(
            "Use local credential references, not secrets, in service context."
        )
    if result["aws_profile"] and not re.fullmatch(r"[\w.+@-]{1,128}", result["aws_profile"]):
        raise InvestigationError("Invalid AWS profile.")
    if result["aws_profile"] and not re.fullmatch(r"\d{12}", result["aws_account"]):
        raise InvestigationError("An AWS profile requires its expected 12-digit account.")
    if result["kube_context"] and not result["kube_server"].startswith("https://"):
        raise InvestigationError("A Kubernetes context requires its expected HTTPS API server.")
    if result["repository"] and not Path(result["repository"]).expanduser().is_dir():
        raise InvestigationError("Repository must be an existing local directory.")
    return result


class Engine:
    def __init__(self, state: Any, directory: Path):
        self.state = state
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.db = directory / "investigations.sqlite3"
        self.runs: dict[str, dict] = {}
        self.slots: dict[str, Any] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.turn_tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.specs: dict[str, tuple[str, str]] = {}
        self.closed = False
        with sqlite3.connect(self.db) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, data TEXT, PRIMARY KEY(kind,id))"
            )
            for key, data in db.execute("SELECT id,data FROM records WHERE kind='run'"):
                row = json.loads(data)
                if row["status"] in {"running", "checking", "connecting", "waiting_approval"}:
                    row["status"] = "interrupted"
                self.runs[key] = row
        register_engine(state, self)
        from kiro_crew.apps.teardown import register_app_disable_hook

        register_app_disable_hook(APP_NAME, self.shutdown)

    def save(self, kind: str, key: str, row: dict) -> None:
        with sqlite3.connect(self.db) as db:
            db.execute(
                "INSERT OR REPLACE INTO records VALUES (?,?,?)", (kind, key, json.dumps(row))
            )

    def services(self) -> list[dict]:
        with sqlite3.connect(self.db) as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT data FROM records WHERE kind='service' ORDER BY id")
            ]

    def save_service(self, data: dict) -> dict:
        row = service_config(data)
        key = data.get("id") or uuid.uuid4().hex
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key):
            raise InvestigationError("Invalid service ID.")
        row["id"] = key
        self.save("service", key, row)
        return row

    def bound_run(self, slot: Any) -> dict | None:
        for key, bound in self.slots.items():
            if bound is slot and self.state.get_slot(slot.key) is slot:
                return self.runs[key]
        return None

    def update(self, row: dict, status: str | None = None, detail: str = "") -> None:
        if status:
            row["status"] = status
        row["updated_at"] = time.time()
        if detail:
            row["timeline"].append({"at": row["updated_at"], "text": clean(detail)[:4000]})
            row["timeline"] = row["timeline"][-100:]
        self.save("run", row["id"], row)

    def view(self, row: dict) -> dict:
        result = dict(row)
        slot = self.slots.get(row["id"])
        if slot and slot.running:
            result["status"] = "waiting_approval" if slot._approval_futures else "running"
        result["url"] = f"/apps/{APP_NAME}?run={row['id']}"
        materialized = self.state.get_slot(row["slot_key"])
        result["chat_ready"] = bool(materialized and materialized._app == APP_NAME)
        return result

    def launch(self, row: dict, reconnect: bool = False) -> None:
        if self.closed:
            raise InvestigationError("The investigations extension is disabled.")
        key = row["id"]
        task = self.tasks.get(key)
        turn_task = self.turn_tasks.get(key)
        slot = self.slots.get(key)
        if (
            (task and not task.done())
            or (turn_task and not turn_task.done())
            or (slot and slot.running)
        ):
            raise InvestigationError("This investigation is already running.")
        self.update(row, "connecting" if reconnect else "checking", "Checking local access.")
        self.tasks[key] = asyncio.create_task(self.run(row, reconnect))

    async def resume(self, row: dict, reconnect: bool = False) -> None:
        async with self.locks.setdefault(row["id"], asyncio.Lock()):
            self.launch(row, reconnect)

    async def command(
        self, argv: list[str], *, timeout: int = 30, progress: dict | None = None
    ) -> str:
        env = {**os.environ, "AWS_PAGER": "", "AWS_CLI_AUTO_PROMPT": "off"}
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        output = bytearray()

        async def read() -> None:
            assert process.stdout is not None
            while chunk := await process.stdout.read(1024):
                output.extend(chunk)
                if len(output) > 65536:
                    raise InvestigationError("Local command output exceeded its limit.")
                if progress is not None:
                    progress["auth_message"] = clean(output.decode("utf-8", errors="replace"))
            await process.wait()

        try:
            await asyncio.wait_for(read(), timeout)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        text = output.decode("utf-8", errors="replace")
        if process.returncode:
            raise InvestigationError(clean(text)[:3000] or "Local command failed.")
        return text

    async def preflight(self, service: dict) -> dict:
        from kiro_crew.platform_compat import trusted_aws_bin

        identity = {}
        if service["aws_profile"]:
            aws = trusted_aws_bin()
            if not aws:
                raise InvestigationError("A trusted AWS CLI installation is required.")
            raw = await self.command(
                [
                    aws,
                    "sts",
                    "get-caller-identity",
                    "--profile",
                    service["aws_profile"],
                    "--output",
                    "json",
                ]
            )
            account = json.loads(raw)
            if account.get("Account") != service["aws_account"]:
                raise InvestigationError("AWS account mismatch. Review the saved service context.")
            identity["aws"] = {"account": account["Account"], "arn": account.get("Arn", "")}
        if service["kube_context"]:
            raw = await self.command(
                [
                    "kubectl",
                    "--context",
                    service["kube_context"],
                    "config",
                    "view",
                    "--minify",
                    "-o",
                    "json",
                ]
            )
            config = json.loads(raw)
            server = config["clusters"][0]["cluster"]["server"]
            if server.rstrip("/") != service["kube_server"].rstrip("/"):
                raise InvestigationError(
                    "Kubernetes server mismatch. Review the saved service context."
                )
            identity["kubernetes"] = {"context": service["kube_context"], "server": server}
        return identity

    async def start(self, service_id: str, question: str, origin: str = "") -> dict:
        if not isinstance(question, str) or not question.strip() or len(question) > 16000:
            raise InvestigationError(
                "An investigation question is required (up to 16000 characters)."
            )
        service = next((s for s in self.services() if s["id"] == service_id), None)
        if service is None:
            raise InvestigationError("Choose a saved service.")
        key = uuid.uuid4().hex
        scratch = self.directory / "scratch" / key
        scratch.mkdir(parents=True)
        row = {
            "id": key,
            "slot_key": f"investigation-{key}",
            "service": service,
            "question": clean(question),
            "origin": origin,
            "scratch": str(scratch),
            "status": "checking",
            "created_at": time.time(),
            "updated_at": time.time(),
            "timeline": [],
            "report": {},
            "identity": {},
            "auth_message": "",
        }
        self.runs[key] = row
        self.launch(row)
        return self.view(row)

    async def ensure_slot(self, row: dict) -> Any:
        from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

        key = row["slot_key"]
        slot = self.state.get_slot(key)
        if slot is None:
            slot = await rehydrate_slot_from_history_async(self.state, key, adopt_closed=True)
        if slot is not None and slot._app != APP_NAME:
            raise InvestigationError("The investigation session belongs to another context.")
        slot = slot or self.state.get_or_create_slot(name=key, app=APP_NAME)
        slot.project = row["service"]["repository"] or row["scratch"]
        slot.title = f"{row['service']['name']}: {row['question'][:70]}"
        slot._titled = True
        self.slots[row["id"]] = slot
        return slot

    async def prepare_turn(self, slot: Any, *, preparing: bool = False) -> None:
        from kiro_crew.agent_sdk.backends import ACP_BACKENDS_SIDE_READONLY
        from kiro_crew.apps.manager import is_app_enabled
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.chat_utils import slot_history_key
        from kiro_crew.dashboard.side_readonly_spec import publish_readonly_spec
        from kiro_crew.execution_context import read_session_execution

        row = self.bound_run(slot)
        if (
            self.closed
            or row is None
            or row["status"]
            in {
                "cancelled",
                "interrupted",
                "waiting_auth",
                "failed",
            }
        ):
            raise InvestigationError(
                "Resume this investigation from its page before sending a message."
            )
        if not preparing and row["status"] in {"checking", "connecting"}:
            raise InvestigationError(
                "Wait for target identity verification before sending a message."
            )
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if cfg.agent.acp_backend not in ACP_BACKENDS_SIDE_READONLY:
            raise InvestigationError(
                "V1 investigations require the Kiro CLI backend, whose tool pre-approvals "
                "can be removed. Your selected backend has not been changed."
            )
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            raise InvestigationError("The investigations extension is disabled.")
        published = await asyncio.to_thread(
            publish_readonly_spec,
            "service-investigator",
            slot.project,
        )
        signature = (published.name, published.digest)
        execution = await asyncio.to_thread(read_session_execution, slot_history_key(slot))
        if execution is not None and execution.template_id != published.name:
            raise InvestigationError(
                "This investigation's saved agent binding changed. Start a new investigation."
            )
        if self.specs.get(row["id"]) != signature or slot.agent != published.name:
            await self.state.sessions.reset(slot_history_key(slot))
            slot.agent = published.name
            self.specs[row["id"]] = signature
        if self.closed or row["status"] == "cancelled" or self.bound_run(slot) is not row:
            raise InvestigationError("Investigation stopped during preparation.")

    async def run(self, row: dict, reconnect: bool = False) -> None:
        from kiro_crew.dashboard.chat_runner import _run_chat

        try:
            if reconnect:
                from kiro_crew.platform_compat import trusted_aws_bin

                profile = row["service"]["aws_profile"]
                if not profile:
                    raise InvestigationError("This service has no AWS profile.")
                aws = trusted_aws_bin()
                if not aws:
                    raise InvestigationError("A trusted AWS CLI installation is required.")
                await self.command(
                    [
                        aws,
                        "sso",
                        "login",
                        "--profile",
                        profile,
                        "--no-browser",
                        "--use-device-code",
                    ],
                    timeout=300,
                    progress=row,
                )
                row["auth_message"] = ""
            row["identity"] = await self.preflight(row["service"])
            slot = await self.ensure_slot(row)
            await self.prepare_turn(slot, preparing=True)
            self.update(row, "running", "Target identity verified. Investigation started.")
            prompt = (
                "Run this on-demand service investigation independently. Read freely using "
                "local tools, DB queries, kubectl, AWS and diagnostic scripts. Use inline "
                "scripts when practical so their source can be reviewed. Scratch files may "
                "be written only in the scratch directory. Keep credentials in their local "
                "stores; never print or copy secrets. Always use the explicit configured "
                "AWS profile, Kubernetes context and namespaces. Do not switch ambient "
                "profiles or contexts. No IAM/RBAC or infrastructure setup is needed. "
                "Do not delegate tools to other agents. Do not mutate real resources "
                "without an exact native approval. Before requesting a change, present "
                "the evidence, options, recommendation and exact intended operation. "
                "Report progress and findings using investigation(action='report', id=..., "
                "report={summary,evidence,hypotheses,gaps,recommendation,decisions}). "
                "Report values are strings. If AWS auth expires, report status='waiting_auth' "
                "and stop. At the end report status='completed'. Distinguish facts from "
                "hypotheses and include reproducible evidence references, never secrets. "
                "Prior report and transcript remain available on resume.\n"
                + json.dumps(
                    {
                        k: row[k]
                        for k in ("id", "question", "service", "scratch", "identity", "report")
                    },
                    ensure_ascii=False,
                )
            )

            async def turn(state: Any, target: Any, message: str) -> None:
                async def admitted() -> None:
                    if self.closed or row["status"] == "cancelled":
                        return
                    await _run_chat(state, target, message, _directive_user_origin=False)

                try:
                    await state.run_background_turn(
                        target,
                        admitted(),
                    )
                    if row["status"] == "running":
                        self.update(
                            row,
                            "needs_attention",
                            "Turn ended. Review the conversation and any remaining questions.",
                        )
                except asyncio.CancelledError:
                    if row["status"] == "running":
                        self.update(
                            row, "interrupted", "Investigation interrupted; it can be resumed."
                        )
                    raise
                except Exception:
                    self.update(
                        row, "failed", "Agent turn failed. Review the conversation, then resume."
                    )
                finally:
                    self.notify(row)

            slot.enqueue_or_run_prompt(prompt, turn, self.state)
            self.turn_tasks[row["id"]] = slot.task
        except asyncio.CancelledError:
            if row["status"] != "cancelled":
                self.update(row, "interrupted", "Investigation interrupted; it can be resumed.")
            raise
        except Exception as exc:
            detail = clean(str(exc))[:3000]
            auth = any(
                term in detail.lower()
                for term in ("sso", "expiredtoken", "expired token", "credentials", "unauthorized")
            )
            self.update(row, "waiting_auth" if auth else "failed", detail)
            self.notify(row)

    def notify(self, row: dict) -> None:
        origin = self.state.get_slot(row["origin"]) if row["origin"] else None
        if origin is not None:
            from kiro_crew.dashboard.session_control import _refuse_ineligible_creator

            try:
                _refuse_ineligible_creator(self.state, origin)
            except Exception:
                return
            origin.append(
                "system",
                f"Investigation {row['id']}: {row['status']}. "
                f"[Open investigation](/apps/{APP_NAME}?run={row['id']})",
                "msg msg-info",
            )
            self.state.push_slots_update()

    async def cancel(self, row: dict) -> None:
        async with self.locks.setdefault(row["id"], asyncio.Lock()):
            await self._cancel(row)

    async def _cancel(self, row: dict) -> None:
        from kiro_crew.dashboard.chat_handlers import stop_slot_turn

        self.update(row, "cancelled", "Cancelled by the operator.")
        task = self.tasks.get(row["id"])
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        slot = self.slots.get(row["id"])
        if slot:
            turn_tasks = {
                task
                for task in (self.turn_tasks.get(row["id"]), slot.task)
                if task is not None and not task.done()
            }
            await stop_slot_turn(self.state, slot, force=True)
            # No provider exists while waiting for background capacity, so the
            # native stop alone cannot revoke that queued task's admission.
            for turn_task in turn_tasks:
                turn_task.cancel()
            await asyncio.gather(*turn_tasks, return_exceptions=True)

    async def shutdown(self, app_name: str = "") -> None:
        self.closed = True
        for row in list(self.runs.values()):
            task = self.tasks.get(row["id"])
            turn_task = self.turn_tasks.get(row["id"])
            slot = self.slots.get(row["id"])
            if (
                (task and not task.done())
                or (turn_task and not turn_task.done())
                or (slot and slot.running)
            ):
                await self.cancel(row)
                self.update(row, "interrupted", "Extension stopped; resume after enabling it.")

    def report(self, row: dict, report: dict, status: str) -> None:
        if status not in {"running", "completed", "waiting_auth", "needs_attention"}:
            raise InvestigationError("Invalid report status.")
        if not isinstance(report, dict):
            raise InvestigationError("Report must be an object.")
        fields = {"summary", "evidence", "hypotheses", "gaps", "recommendation", "decisions"}
        for key, value in report.items():
            if key not in fields or not isinstance(value, str) or len(value) > 16000:
                raise InvestigationError("Report fields must be text of up to 16000 characters.")
        row["report"].update({k: clean(v) for k, v in report.items()})
        self.update(row, status, "Findings updated.")
