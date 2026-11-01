"""Settings loaded from config/settings.yaml with environment overrides."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

ProviderName = Literal["scripted", "gemini", "ollama", "replay"]


def project_root() -> Path:
    """Directory that holds config/settings.yaml (FRAUD_ROOT wins, then the source checkout)."""
    if env := os.environ.get("FRAUD_ROOT"):
        return Path(env).resolve()
    for candidate in [Path.cwd(), *Path.cwd().parents, Path(__file__).resolve().parents[2]]:
        if (candidate / "config" / "settings.yaml").exists():
            return candidate
    return Path(__file__).resolve().parents[2]


class GeminiSettings(BaseModel):
    model_id: str = "gemini-3.5-flash"
    temperature: float = 0.0
    max_output_tokens: int = 2048


class OllamaSettings(BaseModel):
    host: str = "http://localhost:11434"
    model_id: str = "qwen3:8b"
    temperature: float = 0.0


class AuxModelSettings(BaseModel):
    provider: ProviderName = "gemini"
    model_id: str = "gemini-3.5-flash-lite"


class ModelSettings(BaseModel):
    provider: ProviderName = "scripted"
    fallback: ProviderName | None = None
    fallback_model_id: str | None = None  # model for the fallback provider; defaults to that provider's model
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    aux_model: AuxModelSettings = Field(default_factory=AuxModelSettings)
    recordings_dir: str = "evals/recordings"


class BudgetSettings(BaseModel):
    max_tool_calls: int = 12
    max_total_tokens: int = 60000


class PolicySettings(BaseModel):
    approval_amount_threshold: float = 5000.0
    max_policy_retries: int = 1
    version: str = "v1"


class ToolSettings(BaseModel):
    transport: Literal["local", "mcp"] = "local"
    timeout_seconds: float = 5.0
    retry_attempts: int = 3
    max_text_chars: int = 2000
    mcp_command: list[str] = Field(default_factory=lambda: ["python", "-m", "fraud_investigator.mcp_server"])


class PathSettings(BaseModel):
    database: str = "data/fraud.db"
    sessions: str = "data/sessions"
    runs: str = "data/runs"


class ObservabilitySettings(BaseModel):
    enabled: bool = False
    otlp_endpoint: str = "http://localhost:6006/v1/traces"
    service_name: str = "fraud-alert-investigator"


class Price(BaseModel):
    input: float
    output: float


class Settings(BaseModel):
    model: ModelSettings = Field(default_factory=ModelSettings)
    budgets: BudgetSettings = Field(default_factory=BudgetSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    pricing: dict[str, Price] = Field(default_factory=dict)

    def resolve(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else project_root() / path

    @property
    def database_path(self) -> Path:
        return self.resolve(self.paths.database)

    @property
    def model_id(self) -> str:
        match self.model.provider:
            case "gemini":
                return self.model.gemini.model_id
            case "ollama":
                return self.model.ollama.model_id
            case _:
                return self.model.provider


def _apply_env(raw: dict[str, Any]) -> dict[str, Any]:
    model = raw.setdefault("model", {})
    if provider := os.environ.get("FRAUD_MODEL_PROVIDER"):
        model["provider"] = provider
    if model_id := os.environ.get("FRAUD_MODEL_ID"):
        provider = model.get("provider", "scripted")
        if provider in ("gemini", "ollama"):
            model.setdefault(provider, {})["model_id"] = model_id
    if host := os.environ.get("OLLAMA_HOST"):
        model.setdefault("ollama", {})["host"] = host
    if db := os.environ.get("FRAUD_DATABASE"):
        raw.setdefault("paths", {})["database"] = db
    if fallback := os.environ.get("FRAUD_MODEL_FALLBACK"):
        model["fallback"] = None if fallback == "none" else fallback
    if os.environ.get("FRAUD_TRACING") == "1":
        raw.setdefault("observability", {})["enabled"] = True
    return raw


def load_dotenv(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from .env into the environment; variables already set win."""
    path = path or project_root() / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.split(" #")[0].strip().strip('"').strip("'"))


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv()
    path = path or project_root() / "config" / "settings.yaml"
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return Settings.model_validate(_apply_env(raw or {}))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
