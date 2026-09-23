"""Codex signs in from an API key in the environment only when no login is stored.

``codex-acp`` authenticates from an env var only through ACP ``authenticate``, which
Crew never sends; the adapter's own ``DEFAULT_AUTH_REQUEST`` is used "when Codex
requires authentication", so seeding it keeps a subscription login first.
"""

from __future__ import annotations

import json


class TestCodexApiKeyFallback:
    def _seed(self, env):
        from kiro_crew.acp.harness.codex import seed_api_key_auth_request

        seed_api_key_auth_request(env)
        return env.get("DEFAULT_AUTH_REQUEST")

    def test_no_key_leaves_the_subscription_login_alone(self):
        assert self._seed({}) is None

    def test_openai_key_names_its_method_not_its_value(self):
        value = self._seed({"OPENAI_API_KEY": "sk-secret-value"})
        assert json.loads(value) == {"methodId": "openai-api-key"}
        assert "sk-secret-value" not in value

    def test_codex_key_takes_precedence(self):
        env = {"OPENAI_API_KEY": "a", "CODEX_API_KEY": "b"}
        assert json.loads(self._seed(env)) == {"methodId": "codex-api-key"}

    def test_an_operator_value_is_kept(self):
        assert self._seed({"OPENAI_API_KEY": "a", "DEFAULT_AUTH_REQUEST": "mine"}) == "mine"
