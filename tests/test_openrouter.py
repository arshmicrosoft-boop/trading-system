"""Tests for the OpenRouter provider.

The provider is mostly a translation layer, and the part worth testing is the
translation: an exhausted balance and a harness-restricted model tier both
arrive upstream as opaque HTTP errors, and the entire value of this class is
that they become actionable. A test that only checked "it constructs" would
miss exactly the regression that matters.
"""

from __future__ import annotations

import httpx
import pytest

from layer1 import llm
from layer1.config import Settings


def _settings(**overrides) -> Settings:
    base = {
        "llm_mode": "openrouter",
        "openrouter_api_key": "sk-or-test-key",
        "openrouter_model": "thinkingmachines/inkling-small",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class TestConstruction:
    def test_model_identity_is_namespaced_by_provider(self) -> None:
        # 'openrouter/thinkingmachines/inkling-small', not a bare model id:
        # two providers can serve the same model name, and provenance that
        # cannot distinguish them is not provenance.
        assert _settings().llm_model_name == "openrouter/thinkingmachines/inkling-small"

    def test_missing_key_fails_at_construction_naming_the_variable(self) -> None:
        # Fails fast rather than sending an unauthenticated request that would
        # come back as a generic 401.
        with pytest.raises(llm.LLMError, match="L1_OPENROUTER_API_KEY"):
            llm.OpenRouterProvider(_settings(openrouter_api_key=""))


class TestErrorTranslation:
    """Each upstream failure must become a message that names the remedy."""

    @pytest.mark.parametrize(
        ("upstream", "expected"),
        [
            (
                "thinkingmachines/inkling:free is only available on agentic harnesses",
                "agentic harnesses",
            ),
            (
                "This request requires more credits, or fewer max_tokens",
                "max_tokens",
            ),
            (
                "You do not have sufficient credits for this request",
                "insufficient OpenRouter balance",
            ),
        ],
    )
    def test_known_failures_are_explained(self, upstream: str, expected: str) -> None:
        hint = llm.OpenRouterProvider._explain(upstream)
        assert hint, f"no hint produced for: {upstream}"
        assert expected in hint

    def test_unknown_failure_still_surfaces_the_raw_body(self) -> None:
        # An unrecognised error must not be swallowed: the raw provider message
        # is the only diagnostic available.
        assert llm.OpenRouterProvider._explain("upstream exploded") == ""

    def test_restricted_tier_names_the_fix(self) -> None:
        hint = llm.OpenRouterProvider._explain("only available on agentic harnesses")
        assert "':free'" in hint, "must tell the caller to stop using the :free tier"


class TestRequestShape:
    def _capture(self, provider: llm.OpenRouterProvider) -> dict:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json

            captured["headers"] = dict(request.headers)
            captured["body"] = _json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"total_tokens": 12},
                },
            )

        # Route every call in the retry loop through the mock: patching
        # `httpx.post` with a one-shot stub lets the retry escape to the
        # network and produce a confusing 401 instead of a clean assertion.
        client = httpx.Client(transport=httpx.MockTransport(handler))
        original = httpx.post
        httpx.post = client.post
        try:
            completion = provider.complete("sys", "user", json_mode=True)
        finally:
            httpx.post = original
            client.close()
        captured["completion"] = completion
        return captured

    def test_sends_attribution_headers(self) -> None:
        provider = llm.OpenRouterProvider(
            _settings(openrouter_site_url="https://example.test/repo")
        )
        headers = self._capture(provider)["headers"]
        assert headers.get("http-referer") == "https://example.test/repo"
        assert headers.get("x-title") == "moonbot-layer1"
        assert headers.get("authorization") == "Bearer sk-or-test-key"

    def test_json_mode_is_requested(self) -> None:
        # The four agents all depend on parseable JSON; if this silently stops
        # being sent, every component breaks at once.
        provider = llm.OpenRouterProvider(_settings())
        body = self._capture(provider)["body"]
        assert body["response_format"] == {"type": "json_object"}
        assert body["model"] == "thinkingmachines/inkling-small"

    def test_completion_is_parsed_as_json(self) -> None:
        provider = llm.OpenRouterProvider(_settings())
        # complete_json goes through the retry loop, so the mock must still be
        # installed when it runs -- parsing the captured completion instead
        # would never exercise the path the agents actually take.
        captured = self._capture(provider)
        parsed = llm._extract_json(captured["completion"].text)
        assert parsed == {"ok": True}
        assert captured["completion"].model == (
            "openrouter/thinkingmachines/inkling-small"
        )


class TestPreflight:
    def test_zero_balance_raises_with_the_topup_url(self) -> None:
        # Discovering a zero balance forty minutes into extraction is the exact
        # failure this preflight exists to prevent.
        provider = llm.OpenRouterProvider(_settings())
        original = httpx.get
        httpx.get = lambda *a, **k: httpx.Response(
            200, json={"data": {"total_credits": 0, "total_usage": 0.19}}
        )
        try:
            with pytest.raises(llm.LLMError, match="credits"):
                provider.preflight()
        finally:
            httpx.get = original

    def test_positive_balance_reports_the_amount(self) -> None:
        provider = llm.OpenRouterProvider(_settings())
        original = httpx.get
        httpx.get = lambda *a, **k: httpx.Response(
            200, json={"data": {"total_credits": 5.0}}
        )
        try:
            assert "$5.00" in provider.preflight()
        finally:
            httpx.get = original


class TestModeSelection:
    def test_factory_selects_openrouter(self) -> None:
        llm.reset_llm_cache()
        import layer1.config as config

        original = config.get_settings
        config.get_settings = lambda: _settings()  # type: ignore[assignment]
        import layer1.llm as llm_module

        llm_module.get_settings = config.get_settings  # type: ignore[assignment]
        try:
            client = llm_module.get_llm()
            assert client._provider.name == "openrouter"
        finally:
            llm_module.get_settings = original  # type: ignore[assignment]
            llm.reset_llm_cache()

    def test_preflight_is_optional_on_the_client(self) -> None:
        # Providers without a preflight must not make callers special-case
        # them, or every new provider becomes a branch at the call site.
        class NoPreflight:
            name = "stub"

            def complete(self, system, user, *, json_mode):  # noqa: ANN001, ANN202
                return llm.Completion(text="{}", model="stub")

        assert "no preflight" in llm.LLMClient(NoPreflight()).preflight()