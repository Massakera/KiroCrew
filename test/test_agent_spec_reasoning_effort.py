"""Per-profile effort, pi-only effort levels and provider-qualified spawn models.

An agent spec may pin its own ``reasoning_effort``; it ranks between a crew's
pin and the configured defaults, mirroring how a spec ``model`` ranks. pi alone
offers ``off``/``minimal``, so those levels count only for a pi session, and a
spawn naming them on another harness is refused rather than silently dropped.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.validation import SPAWN_RUN_SCHEMA, ValidationError, validate_tool_args


class TestSpawnModelPattern:
    @pytest.mark.parametrize(
        "model", ["deepseek-3.2", "openai-codex/gpt-6-sol", "opencode-go/mimo-v2.6-pro"]
    )
    def test_plain_and_provider_qualified_ids_are_accepted(self, model):
        cleaned = validate_tool_args({"task": "x", "model": model}, SPAWN_RUN_SCHEMA)
        assert cleaned["model"] == model

    @pytest.mark.parametrize(
        "model",
        ["a/b/c", "/gpt", "openai/", "../gpt", "openai/.hidden", "gpt:high", "a b"],
    )
    def test_path_shapes_and_suffixes_are_refused(self, model):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "model": model}, SPAWN_RUN_SCHEMA)

    def test_other_model_fields_keep_the_plain_pattern(self):
        from kiro_crew.validation import _MODEL_NAME_RE

        assert _MODEL_NAME_RE.match("openai-codex/gpt-6-sol") is None

    @pytest.mark.parametrize("level", ["off", "minimal"])
    def test_pi_only_levels_pass_the_schema(self, level):
        cleaned = validate_tool_args({"task": "x", "reasoning_effort": level}, SPAWN_RUN_SCHEMA)
        assert cleaned["reasoning_effort"] == level


class TestApiSpawnPiOnlyEffort:
    def _request(self, body: dict) -> tuple[Any, MagicMock]:
        mgr = MagicMock()
        mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
        mgr.max_concurrent = 4
        state = SimpleNamespace(subagents=mgr, conversation_log=MagicMock())
        request = MagicMock()
        request.app = {"state": state}
        request.headers = {}

        async def _json() -> dict:
            return body

        request.json = _json
        return request, mgr

    @staticmethod
    def _config(backend: str) -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = backend
        return cfg

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["off", "minimal"])
    async def test_refused_when_the_default_backend_is_not_pi(self, level):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "reasoning_effort": level})
        with patch.object(KiroCrewConfig, "load", classmethod(lambda c: self._config("kiro"))):
            resp = await api_spawn(request)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "effort_unsupported_backend"
        mgr.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepted_when_the_default_backend_is_pi(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "reasoning_effort": "off"})
        with patch.object(KiroCrewConfig, "load", classmethod(lambda c: self._config("pi"))):
            await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["reasoning_effort"] == "off"

    @pytest.mark.asyncio
    async def test_accepted_with_an_explicit_pi_backend(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "reasoning_effort": "minimal", "backend": "pi"})
        with (
            patch("kiro_crew.subagent_backend.check_spawn_backend", return_value=None),
            patch("kiro_crew.subagent_backend.check_backend_installed", return_value=None),
            patch.object(KiroCrewConfig, "load", classmethod(lambda c: self._config("kiro"))),
        ):
            await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["reasoning_effort"] == "minimal"

    @pytest.mark.asyncio
    async def test_an_explicit_other_backend_is_refused(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "reasoning_effort": "off", "backend": "droid"})
        with (
            patch("kiro_crew.subagent_backend.check_spawn_backend", return_value=None),
            patch("kiro_crew.subagent_backend.check_backend_installed", return_value=None),
            patch.object(KiroCrewConfig, "load", classmethod(lambda c: self._config("pi"))),
        ):
            resp = await api_spawn(request)
        assert resp.status == 400
        mgr.spawn.assert_not_called()


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.kiro_agents_dir", lambda: tmp_path)
    return tmp_path


def _write_md(directory, name: str, **fields: str) -> None:
    # Quoted: YAML reads a bare ``off`` as the boolean false.
    front = "\n".join(f"{key}: {json.dumps(value)}" for key, value in fields.items())
    (directory / f"{name}.md").write_text(
        f"---\nname: {name}\n{front}\n---\nReview the assigned scope.\n", encoding="utf-8"
    )


class TestSpecEffortTier:
    def _cfg(self, *, chat: str = "low", backend: str = "pi") -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        cfg.agent.reasoning_effort = chat
        cfg.agent.acp_backend = backend
        return cfg

    def test_spec_level_beats_the_chat_default(self, agents_dir):
        _write_md(agents_dir, "reviewer", reasoning_effort="high")
        assert self._cfg().resolve_session_effort("reviewer") == "high"

    def test_native_role_effort_uses_the_bound_template_and_defers_to_pins(self, agents_dir):
        cfg = self._cfg()
        cfg.agent.role_efforts["research"] = "high"
        cfg.agents["campaign"] = KiroCrewAgentConfig(kiro_agent="kirocrew-research")
        assert cfg.resolve_session_effort("campaign") == "high"
        _write_md(agents_dir, "kirocrew-research", reasoning_effort="medium")
        assert cfg.resolve_session_effort("campaign") == "medium"
        cfg.agents["campaign"].reasoning_effort = "max"
        assert cfg.resolve_session_effort("campaign") == "max"
        assert cfg.resolve_session_effort("unrelated-template") == "low"

    def test_crew_pin_beats_the_spec(self, agents_dir):
        _write_md(agents_dir, "reviewer", reasoning_effort="high")
        cfg = self._cfg()
        cfg.agents["crewmate"] = KiroCrewAgentConfig(kiro_agent="reviewer", reasoning_effort="max")
        assert cfg.resolve_session_effort("reviewer", "crewmate") == "max"

    def test_a_crew_without_a_pin_takes_its_templates_spec(self, agents_dir):
        _write_md(agents_dir, "reviewer", reasoning_effort="xhigh")
        cfg = self._cfg()
        cfg.agents["crewmate"] = KiroCrewAgentConfig(kiro_agent="reviewer")
        assert cfg.resolve_session_effort("crewmate") == "xhigh"

    @pytest.mark.parametrize("level", ["off", "minimal"])
    def test_pi_only_levels_apply_on_pi(self, agents_dir, level):
        _write_md(agents_dir, "reader", reasoning_effort=level)
        assert self._cfg(backend="pi").resolve_session_effort("reader") == level

    def test_pi_only_levels_defer_on_another_backend(self, agents_dir):
        _write_md(agents_dir, "reader", reasoning_effort="off")
        cfg = self._cfg(backend="pi")
        assert cfg.resolve_session_effort("reader", backend="droid") == "low"
        assert self._cfg(backend="kiro").resolve_session_effort("reader") == "low"

    @pytest.mark.parametrize("junk", ["turbo", "HIGH", "[]"])
    def test_an_unknown_value_pins_nothing(self, agents_dir, junk):
        _write_md(agents_dir, "reviewer", reasoning_effort=junk)
        assert self._cfg().resolve_session_effort("reviewer") == "low"

    def test_a_spec_without_the_field_pins_nothing(self, agents_dir):
        _write_md(agents_dir, "reviewer", model="openai-codex/gpt-6-astra")
        assert self._cfg().resolve_session_effort("reviewer") == "low"

    def test_the_model_lookup_is_unchanged(self, agents_dir):
        _write_md(agents_dir, "reviewer", model="openai-codex/gpt-6-astra")
        assert KiroCrewConfig._resolve_named_agent_model("reviewer") == "openai-codex/gpt-6-astra"
        assert KiroCrewConfig._resolve_named_agent_model("absent") == ""


class TestFactoryCarriesTheSpecEffort:
    def test_pi_session_receives_the_spec_level_for_the_spec_model(self, agents_dir):
        _write_md(
            agents_dir,
            "reader",
            model="openai-codex/gpt-6-astra",
            reasoning_effort="off",
        )
        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        cfg.agent.acp_backend = "pi"
        with patch("kiro_crew.providers.acp.AcpProvider") as provider:
            provider.return_value = MagicMock()
            cfg.create_provider_factory()(session_key="subagent:abc", agent="reader")
        kwargs = provider.call_args.kwargs
        assert kwargs.get("effort_per_model") == {"openai-codex/gpt-6-astra": "off"}

    def test_an_explicit_override_beats_the_spec(self, agents_dir):
        _write_md(
            agents_dir,
            "reader",
            model="openai-codex/gpt-6-astra",
            reasoning_effort="off",
        )
        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        cfg.agent.acp_backend = "pi"
        with patch("kiro_crew.providers.acp.AcpProvider") as provider:
            provider.return_value = MagicMock()
            cfg.create_provider_factory()(
                session_key="subagent:abc", agent="reader", reasoning_effort_override="high"
            )
        kwargs = provider.call_args.kwargs
        assert kwargs.get("effort_per_model") == {"openai-codex/gpt-6-astra": "high"}
