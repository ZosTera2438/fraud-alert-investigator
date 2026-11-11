"""Model factory: Cloudflare Workers AI (live default), Gemini (comparison), scripted (offline) and replay.

The provider comes from configuration, so the agents never import a provider.
`FallbackModel` switches to the secondary provider when the primary is rate
limited or unreachable, and records the switch on the current trace span.
`RecordingModel` and `ReplayModel` capture live responses so continuous
integration can rerun the evaluation set without a network.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import AsyncGenerator, AsyncIterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from pydantic import BaseModel
from strands.models.model import Model
from strands.types.content import Messages
from strands.types.exceptions import ModelThrottledException
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

from .scripted_model import Behaviour, ScriptedModel
from .settings import ProviderName, Settings, get_settings

if TYPE_CHECKING:
    from strands.models.openai import OpenAIModel

logger = logging.getLogger(__name__)


def build_provider(provider: ProviderName, settings: Settings, behaviour: Behaviour = "diligent") -> Model:
    match provider:
        case "scripted":
            return ScriptedModel(behaviour=behaviour)
        case "gemini":
            from strands.models.gemini import GeminiModel

            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not key:
                raise RuntimeError("Set GEMINI_API_KEY (or GOOGLE_API_KEY) to use the Gemini provider.")
            g = settings.model.gemini
            return GeminiModel(
                client_args={"api_key": key},
                model_id=g.model_id,
                params={"temperature": g.temperature, "max_output_tokens": g.max_output_tokens},
            )
        case "cloudflare":
            account, token = os.environ.get("CLOUDFLARE_ACCOUNT_ID"), os.environ.get("CLOUDFLARE_API_TOKEN")
            if not account or not token:
                raise RuntimeError("Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN to use the Cloudflare provider.")
            c = settings.model.cloudflare
            params: dict[str, Any] = {"temperature": c.temperature, "max_tokens": c.max_tokens}
            if c.reasoning_effort and "gpt-oss" in c.model_id:
                params["reasoning_effort"] = c.reasoning_effort
            return _cloudflare_model_class()(
                client_args={"api_key": token, "base_url": c.base_url.format(account_id=account)},
                model_id=c.model_id,
                params=params,
            )
        case "replay":
            return ReplayModel(settings.resolve(settings.model.recordings_dir))
    raise ValueError(f"unknown provider {provider}")


def _cloudflare_model_class() -> type[OpenAIModel]:
    """OpenAIModel adjusted for the Workers AI request validator.

    Workers AI rejects `"tools": []` (the field is left out instead) and, once a conversation has a
    system prompt and tool turns, rejects message content given as a list of text parts (it is
    joined into a plain string; lists holding anything other than text are left alone) and
    assistant tool-call turns without a `content` field (an empty string is sent).
    """
    from strands.models.openai import OpenAIModel

    class CloudflareModel(OpenAIModel):
        def format_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            request = super().format_request(*args, **kwargs)
            if not request.get("tools"):
                request.pop("tools", None)
                request.pop("tool_choice", None)
            for message in request.get("messages", []):
                content = message.get("content")
                if isinstance(content, list) and all(p.get("type") == "text" for p in content):
                    message["content"] = "".join(p.get("text", "") for p in content)
                elif content is None:
                    message["content"] = ""  # tool-call turns: Workers AI requires the field
            return request

    return CloudflareModel


def create_model(settings: Settings | None = None, behaviour: Behaviour = "diligent") -> Model:
    settings = settings or get_settings()
    primary = build_provider(settings.model.provider, settings, behaviour)
    fallback = settings.model.fallback
    if settings.model.provider not in ("cloudflare", "gemini") or not fallback:
        return primary  # offline models never fall back
    fallback_settings = settings
    if fallback_id := settings.model.fallback_model_id:
        sub = settings.model.model_copy(deep=True)
        getattr(sub, fallback).model_id = fallback_id
        fallback_settings = settings.model_copy(update={"model": sub})
    fallback_model_id = getattr(fallback_settings.model, fallback).model_id
    if fallback == settings.model.provider and fallback_model_id == settings.model_id:
        return primary  # falling back to the same model would not help
    try:
        return FallbackModel(primary, build_provider(fallback, fallback_settings, behaviour))
    except Exception as exc:  # fallback is optional; never block the primary on it
        logger.warning("fallback provider %s unavailable: %s", fallback, exc)
        return primary


def model_id_of(model: Model) -> str:
    config = model.get_config()
    return str(config.get("model_id", "unknown")) if isinstance(config, dict) else "unknown"


def is_transient(exc: Exception) -> bool:
    """Rate limits, outages and overload (HTTP 429 and 5xx) are worth a fallback; bad requests are not."""
    if isinstance(exc, (ModelThrottledException, ConnectionError, TimeoutError, OSError)):
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code in (429, 500, 502, 503, 504) or any(m in str(exc) for m in ("503 UNAVAILABLE", "high demand"))


class FallbackModel(Model):
    """Primary model with a fallback for rate limits and outages."""

    def __init__(self, primary: Model, secondary: Model) -> None:
        self.primary, self.secondary = primary, secondary
        self.active = primary
        self.fell_back = False

    def update_config(self, **model_config: Any) -> None:
        self.active.update_config(**model_config)

    def get_config(self) -> Any:
        return self.active.get_config()

    def structured_output(  # type: ignore[override]
        self, output_model: type[BaseModel], prompt: Messages, system_prompt: str | None = None, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        return self.active.structured_output(output_model, prompt, system_prompt, **kwargs)

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,  # type: ignore[override]
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        started = False
        try:
            async for event in self.active.stream(messages, tool_specs, system_prompt, **kwargs):
                started = True
                yield event
            return
        except Exception as exc:
            if started or self.active is self.secondary or not is_transient(exc):
                raise
            logger.warning("primary model failed (%s); falling back", exc)
            self.active, self.fell_back = self.secondary, True
            span = trace.get_current_span()
            span.set_attribute("fraud.model_fallback", True)
            span.add_event("model_fallback", {"reason": type(exc).__name__, "to": model_id_of(self.secondary)})
        async for event in self.active.stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


def _request_key(messages: Messages, tool_specs: list[ToolSpec] | None, system_prompt: str | None) -> str:
    # Volatile fields (retrieval timestamps, tool use IDs) are dropped so a replay matches its recording.
    text = json.dumps(
        {"s": system_prompt, "t": sorted(t["name"] for t in tool_specs or []), "m": messages},
        sort_keys=True,
        default=str,
    )
    text = _VOLATILE.sub("", text)
    return hashlib.sha256(text.encode()).hexdigest()[:24]


_VOLATILE = re.compile(r'\\*"(?:retrieved_at|recorded_at|toolUseId|interruptId)\\*":\s*\\*"[^"\\]*\\*"')


class RecordingModel(Model):
    """Wraps a live model and saves every streamed response by request hash."""

    def __init__(self, inner: Model, directory: Path) -> None:
        self.inner, self.directory = inner, directory
        directory.mkdir(parents=True, exist_ok=True)

    def update_config(self, **model_config: Any) -> None:
        self.inner.update_config(**model_config)

    def get_config(self) -> Any:
        return self.inner.get_config()

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):  # type: ignore[override]
        return self.inner.structured_output(output_model, prompt, system_prompt, **kwargs)

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):  # type: ignore[override]
        events: list[Any] = []
        async for event in self.inner.stream(messages, tool_specs, system_prompt, **kwargs):
            events.append(event)
            yield event
        key = _request_key(messages, tool_specs, system_prompt)
        (self.directory / f"{key}.json").write_text(json.dumps(events, default=str), encoding="utf-8")


class ReplayModel(Model):
    """Replays recorded responses; a missing recording is an error, never a live call."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.config = {"model_id": "replay"}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> Any:
        return self.config

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):  # type: ignore[override]
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):  # type: ignore[override]
        path = self.directory / f"{_request_key(messages, tool_specs, system_prompt)}.json"
        if not path.exists():
            raise RuntimeError(f"no recording for this request ({path.name}); re-record with a live model")
        for event in json.loads(path.read_text(encoding="utf-8")):
            yield event
