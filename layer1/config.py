"""Layer 1 configuration.

Boundary: this package (research engine) has no exchange credentials, no order
plumbing, and no market-data feed.  Layer 2 owns all of that.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LlmMode = Literal["ollama", "openai", "openrouter", "deterministic"]

#: Prompt version stamped onto every artifact. Bump when prompt text changes,
#: otherwise two hypotheses with the same model look identical but are not.
DEFAULT_PROMPT_VERSION = "l1-v1"


class Settings(BaseSettings):
    """Environment-driven settings, read from `.env` or the process env."""

    model_config = SettingsConfigDict(
        env_prefix="L1_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- database -----------------------------------------------------
    database_url: str = Field(
        default="postgresql://moonbot:moonbot@localhost:5433/moonbot",
        description="Layer 1 Postgres DSN.",
    )
    db_pool_min: int = 1
    db_pool_max: int = 8

    # --- llm ----------------------------------------------------------
    llm_mode: LlmMode = Field(
        default="deterministic",
        description=(
            "'openrouter' (hosted models), 'ollama' or 'openai' call a real "
            "model. 'deterministic' is a rule-based provider used for tests "
            "and offline pipeline runs; it is never selected implicitly."
        ),
    )
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # OpenRouter is OpenAI-compatible at the wire level but is a distinct
    # provider: it needs its own key and sends attribution headers that
    # OpenAI would reject. Keeping it separate means a missing key fails
    # with a message naming the right variable instead of silently sending
    # an OpenAI key to a third-party endpoint.
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str = ""
    openrouter_model: str = "thinkingmachines/inkling-small"
    openrouter_site_url: str = ""
    openrouter_app_title: str = "moonbot-layer1"

    llm_timeout_s: float = 300.0
    llm_max_retries: int = 2
    # --- pipeline defaults --------------------------------------------
    prompt_version: str = DEFAULT_PROMPT_VERSION
    default_tier: int = 3
    hypotheses_per_mechanism: int = 5

    # --- pipeline-wide safety net -------------------------------------
    # Layer 1 refuses to write a rule that names a profitability target.
    # This is the single choke point for that guarantee.
    forbidden_spec_keys: frozenset[str] = Field(
        default_factory=lambda: frozenset(
            {
                "expected_return",
                "target_return",
                "expected_profit",
                "target_sharpe",
                "expected_sharpe",
                "profit_target",
                "return_target",
                "expected_pnl",
            }
        )
    )

    @property
    def llm_model_name(self) -> str:
        """Human-readable model identity recorded on every artifact."""
        if self.llm_mode == "ollama":
            return f"ollama/{self.ollama_model}"
        if self.llm_mode == "openai":
            return f"openai/{self.openai_model}"
        if self.llm_mode == "openrouter":
            return f"openrouter/{self.openrouter_model}"
        return "deterministic/v1"

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()