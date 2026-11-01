from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AgentSettings(BaseModel):
    """Model configuration for Cairn's built-in agent.

    Stored by the server and read by the dispatcher before each task, so changing
    it in the UI takes effect on the next task without restarting anything.
    """

    provider: Literal["openai", "anthropic"] = "openai"
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    api_key: str = ""
    context_window: int = Field(default=1_000_000, ge=4096)
    max_tokens: int = Field(default=32768, ge=256)
    temperature: float = Field(default=0.6, ge=0.0, le=2.0)
    request_timeout: int = Field(default=300, ge=5, le=3600)

    @field_validator("base_url", "model")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip() and self.model and self.base_url)

    @property
    def masked_key(self) -> str:
        key = self.api_key.strip()
        if not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return f"{key[:4]}...{key[-4:]}"
