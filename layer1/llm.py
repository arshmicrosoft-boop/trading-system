"""LLM client for Layer 1's four agents.

Two real providers (Ollama, OpenAI-compatible) and one explicit offline mode.

The offline mode is not a fallback: it is selected deliberately via
`L1_LLM_MODE=deterministic` and stamps `deterministic/v1` into `model_used`,
so any artifact it produces is permanently identifiable as
non-LLM-derived in the research database. It exists so the pipeline, the
schema and the API can be exercised without a GPU; it never masquerades as
a model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import Settings, get_settings

__all__ = ["LLMClient", "LLMError", "get_llm", "reset_llm_cache"]

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMError(RuntimeError):
    """Raised when the model cannot be reached or returns unusable output."""


@dataclass(slots=True)
class Completion:
    text: str
    model: str
    raw: dict[str, Any] | None = None


class Provider(Protocol):
    name: str

    def complete(self, system: str, user: str, *, json_mode: bool) -> Completion: ...


def _extract_json(text: str) -> Any:
    """Parse a JSON object out of a model response.

    Small models wrap JSON in prose or fences even when told not to, so peel
    the fence, then fall back to the outermost balanced object.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = _JSON_FENCE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start == -1:
        raise LLMError(f"no JSON object in model output: {text[:200]!r}")
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise LLMError(f"malformed JSON from model: {exc}") from exc
    raise LLMError(f"unterminated JSON object in model output: {text[:200]!r}")


class OllamaProvider:
    """Local Ollama.  Uses native `format: json` for constrained decoding."""

    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self._model = settings.ollama_model
        self._url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
        self._timeout = settings.llm_timeout_s

    def complete(self, system: str, user: str, *, json_mode: bool) -> Completion:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 1400},
        }
        if json_mode:
            payload["format"] = "json"
        try:
            resp = httpx.post(self._url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama request failed: {exc}") from exc
        return Completion(
            text=data.get("message", {}).get("content", ""),
            model=f"ollama/{self._model}",
            raw=data,
        )


class OpenAIProvider:
    """Any OpenAI-compatible chat-completions endpoint."""

    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self._model = settings.openai_model
        self._url = f"{settings.openai_base_url.rstrip('/')}/chat/completions"
        self._key = settings.openai_api_key
        self._timeout = settings.llm_timeout_s

    def complete(self, system: str, user: str, *, json_mode: bool) -> Completion:
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            resp = httpx.post(self._url, json=payload, headers=headers, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError) as exc:
            raise LLMError(f"openai request failed: {exc}") from exc
        return Completion(text=text, model=f"openai/{self._model}", raw=data)


class OpenRouterProvider:
    """OpenRouter-hosted models.

    Wire-compatible with the OpenAI chat-completions API, but with two
    differences that matter here:

    * attribution headers (`HTTP-Referer`, `X-Title`) identify the calling
      app on the provider's dashboard;
    * failures are informative enough to act on. An exhausted balance and a
      model restricted to approved harnesses both arrive as opaque HTTP
      errors otherwise, and the second one invites a pointless hunt through
      the request payload when the real cause is the model tier.
    """

    name = "openrouter"

    #: Upstream messages worth telling apart, because the remedy differs.
    _HINTS: tuple[tuple[str, str], ...] = (
        (
            "only available on agentic harnesses",
            "this model tier is restricted to approved agentic harnesses and "
            "rejects direct API calls; use a non-':free' model id",
        ),
        (
            "requires more credits",
            "the balance cannot cover the requested max_tokens -- either top up "
            "at https://openrouter.ai/settings/credits or reduce the token "
            "budget requested for this call",
        ),
        (
            "sufficient credits",
            "insufficient OpenRouter balance; top up at "
            "https://openrouter.ai/settings/credits",
        ),
    )

    def __init__(self, settings: Settings) -> None:
        if not settings.openrouter_api_key:
            raise LLMError(
                "L1_OPENROUTER_API_KEY is not set. Export it, or put it in .env. "
                "Check `printenv | grep OPENROUTER` on this machine -- the key "
                "exists in the environment but the L1_ prefix is what this client "
                "reads."
            )
        self._model = settings.openrouter_model
        self._url = f"{settings.openrouter_base_url.rstrip('/')}/chat/completions"
        self._key = settings.openrouter_api_key
        self._timeout = settings.llm_timeout_s
        self._model_name = f"openrouter/{settings.openrouter_model}"
        # Captured at construction rather than re-read from get_settings().
        # Re-reading means a provider built from an explicit Settings ignores
        # those settings entirely and silently falls back to the process
        # environment -- which is how a test asserts one thing and the
        # request sends another.
        self._site_url = settings.openrouter_site_url
        self._app_title = settings.openrouter_app_title

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._key}",
        }
        if self._site_url:
            headers["HTTP-Referer"] = self._site_url
        if self._app_title:
            headers["X-Title"] = self._app_title
        return headers

    @classmethod
    def _explain(cls, message: str) -> str:
        lowered = message.lower()
        for needle, hint in cls._HINTS:
            if needle in lowered:
                return hint
        return ""

    def complete(self, system: str, user: str, *, json_mode: bool) -> Completion:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            resp = httpx.post(
                self._url, json=payload, headers=self._headers(), timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"openrouter request failed: {exc}") from exc

        if resp.status_code >= 400:
            detail = resp.text[:300]
            try:
                detail = json.dumps(resp.json())[:300]
            except (ValueError, TypeError):
                pass
            hint = self._explain(detail)
            raise LLMError(
                f"openrouter HTTP {resp.status_code} for model {self._model!r}: "
                f"{detail}" + (f" -- {hint}" if hint else "")
            )

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"openrouter returned an unreadable response: {exc}") from exc
        return Completion(text=content, model=self._model_name, raw=data)

    def preflight(self) -> str:
        """Report credit balance. Raises with an actionable message on failure.

        Worth calling before a long pipeline: discovering a zero balance after
        forty minutes of extraction is a bad way to learn it.
        """
        url = f"{get_settings().openrouter_base_url.rstrip('/')}/credits"
        try:
            resp = httpx.get(
                url,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=min(self._timeout, 30.0),
            )
            data = resp.json()
            total = data.get("data", {}).get("total_credits")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise LLMError(f"openrouter preflight failed: {exc}") from exc
        if total is None:
            raise LLMError("openrouter preflight returned no credit total")
        if float(total) <= 0:
            raise LLMError(
                f"openrouter balance is {total} -- cannot run any paid model. "
                "Top up at https://openrouter.ai/settings/credits, or set "
                "L1_LLM_MODE=ollama to use a local model."
            )
        return f"openrouter balance: ${float(total):.2f}"



class DeterministicProvider:
    """Rule-based provider for offline runs and tests.

    Callers supply the structured result they expect via `user` being a JSON
    document with a `__response__` key. This is a test seam, not a model.
    """

    name = "deterministic"

    def __init__(self, settings: Settings) -> None:
        self._model = "deterministic/v1"

    def complete(self, system: str, user: str, *, json_mode: bool) -> Completion:
        try:
            payload = json.loads(user)
        except json.JSONDecodeError as exc:
            raise LLMError(
                "deterministic provider requires a JSON user message "
                f"carrying '__response__': {exc}"
            ) from exc
        if "__response__" not in payload:
            raise LLMError("deterministic provider input missing '__response__'")
        return Completion(
            text=json.dumps(payload["__response__"]),
            model=self._model,
            raw={"deterministic": True},
        )


class LLMClient:
    """Retrying facade over a provider, with JSON parsing helpers."""

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    @property
    def model_name(self) -> str:
        return self._provider.name

    def complete(
        self, system: str, user: str, *, json_mode: bool = True, retries: int | None = None
    ) -> Completion:
        settings = get_settings()
        attempts = (settings.llm_max_retries if retries is None else retries) + 1
        last: Exception | None = None
        for _ in range(attempts):
            try:
                return self._provider.complete(system, user, json_mode=json_mode)
            except LLMError as exc:
                last = exc
        raise LLMError(str(last)) if last else LLMError("unreachable")

    def complete_json(
        self, system: str, user: str, *, retries: int | None = None
    ) -> tuple[Any, str]:
        """Return (parsed_json, model_name)."""
        completion = self.complete(system, user, json_mode=True, retries=retries)
        return _extract_json(completion.text), completion.model

    def preflight(self) -> str:
        """Verify the provider is usable before a long pipeline run.

        Returns a human-readable status line. Raises LLMError with an
        actionable message when the provider cannot serve -- an exhausted
        balance or an unreachable endpoint. Providers without a preflight
        report that they have nothing to check, rather than raising, so
        callers can treat this as unconditional.
        """
        checker = getattr(self._provider, "preflight", None)
        if checker is None:
            return f"{self._provider.name}: no preflight available"
        return str(checker())


_cached: LLMClient | None = None


def get_llm() -> LLMClient:
    global _cached
    if _cached is None:
        settings = get_settings()
        provider: Provider = {
            "ollama": OllamaProvider,
            "openai": OpenAIProvider,
            "openrouter": OpenRouterProvider,
            "deterministic": DeterministicProvider,
        }[settings.llm_mode](settings)
        _cached = LLMClient(provider)
    return _cached


def reset_llm_cache() -> None:
    global _cached
    _cached = None