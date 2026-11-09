"""LLM access: an in-process litellm Router over the chain groq -> openrouter -> ollama.

Hosted deployments are added only when their key variable is set, so the chain still runs
end to end on a local Ollama alone. Every reply says which deployment answered, whether it
was a fallback, and how many failed attempts came before it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import litellm
from litellm import Router
from litellm.router import RetryPolicy
from litellm.integrations.custom_logger import CustomLogger
from pydantic import BaseModel, Field

from assay.settings import Settings

log = logging.getLogger(__name__)

Tier = Literal["fast", "strong"]
TIERS: tuple[Tier, ...] = ("fast", "strong")

litellm.suppress_debug_info = True
# Hosted providers differ on optional params such as response_format; drop what one cannot take.
litellm.drop_params = True


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMReply(BaseModel):
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    deployment: str
    model: str
    fallback_used: bool
    retries: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class LLMBackend(Protocol):
    async def complete(
        self,
        tier: Tier,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        timeout_s: float,
    ) -> LLMReply: ...


@dataclass(frozen=True)
class Deployment:
    name: str  # e.g. "strong:groq"; also the router model group and model_info id
    litellm_params: dict[str, Any]


def build_chain(settings: Settings, environ: dict[str, str] | None = None) -> dict[Tier, list[Deployment]]:
    """Ordered deployments per tier, skipping any whose key variable is absent."""
    env = os.environ if environ is None else environ
    providers: list[tuple[str, str | None, dict[Tier, str], dict[str, Any]]] = [
        # gpt-oss reasons before answering; "low" keeps that inside the stage budgets.
        ("groq", "GROQ_API_KEY", {"fast": settings.groq_fast_model, "strong": settings.groq_strong_model},
         {"reasoning_effort": "low"}),
        ("groq-backup", "GROQ_API_KEY", {"fast": settings.groq_backup_model, "strong": settings.groq_backup_model},
         {"reasoning_effort": "none"}),
        (
            "openrouter",
            # A project-scoped name: a machine-wide OpenRouter key often carries another project's budget.
            "ASSAY_OPENROUTER_API_KEY",
            {"fast": settings.openrouter_fast_model, "strong": settings.openrouter_strong_model},
            {},
        ),
    ]
    if settings.use_ollama:
        providers.append(
            (
                "ollama",
                None,
                {"fast": settings.ollama_model, "strong": settings.ollama_model},
                {"api_base": settings.ollama_base_url, "num_ctx": settings.ollama_num_ctx, "keep_alive": "30m"},
            )
        )
    chain: dict[Tier, list[Deployment]] = {tier: [] for tier in TIERS}
    for provider, key_var, models, extra in providers:
        if key_var and not env.get(key_var):
            log.info("skipping %s deployments: %s is not set", provider, key_var)
            continue
        for tier in TIERS:
            params: dict[str, Any] = {"model": models[tier], **extra}
            if key_var:
                # litellm reads the key itself; naming the variable keeps the value out of this process's objects.
                params["api_key"] = f"os.environ/{key_var}"
            chain[tier].append(Deployment(name=f"{tier}:{provider}", litellm_params=params))
    for tier in TIERS:
        if not chain[tier]:
            raise RuntimeError(
                f"no LLM deployment available for tier {tier!r}: set GROQ_API_KEY or ASSAY_OPENROUTER_API_KEY, "
                "or leave ASSAY_USE_OLLAMA=true with Ollama running"
            )
    return chain


class _AttemptLog(CustomLogger):
    """Counts failed attempts per call id; litellm reports each retry and fallback hop here."""

    def __init__(self) -> None:
        super().__init__()
        self.failures: dict[str, list[str]] = defaultdict(list)

    def _record(self, kwargs: dict[str, Any]) -> None:
        metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
        call_id = metadata.get("assay_call_id")
        if call_id:
            self.failures[call_id].append(str(metadata.get("model_group") or kwargs.get("model")))

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
        self._record(kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
        self._record(kwargs)

    def take(self, call_id: str) -> list[str]:
        return self.failures.pop(call_id, [])


_attempts = _AttemptLog()
litellm.callbacks = [*litellm.callbacks, _attempts]


class RouterLLM:
    def __init__(self, settings: Settings, chain: dict[Tier, list[Deployment]] | None = None) -> None:
        self.chain = chain if chain is not None else build_chain(settings)
        model_list = []
        fallbacks = []
        for tier in TIERS:
            names = [d.name for d in self.chain[tier]]
            for deployment in self.chain[tier]:
                model_list.append(
                    {
                        "model_name": deployment.name,
                        "litellm_params": {
                            **deployment.litellm_params,
                            "max_parallel_requests": settings.provider_max_parallel,
                        },
                        "model_info": {"id": deployment.name},
                    }
                )
            for i, name in enumerate(names[:-1]):
                fallbacks.append({name: names[i + 1 :]})
        # litellm rejects a call outright when a deployment's parallel slots are full; waiting here
        # queues the call instead, so a run with many claims does not strike them as provider failures.
        self._slots = asyncio.Semaphore(settings.provider_max_parallel)
        self._router = Router(
            model_list=model_list,
            fallbacks=fallbacks,
            num_retries=settings.llm_num_retries,
            # A 429 or a timeout will not clear inside a 30 s run, so those go straight to the next
            # provider; retrying the same one spent 7 s of the critique budget on Groq's TPM limit.
            retry_policy=RetryPolicy(RateLimitErrorRetries=0, TimeoutErrorRetries=0),
            retry_after=0,
            allowed_fails=1,
            cooldown_time=settings.llm_cooldown_s,
            timeout=settings.llm_timeout_s,
        )
        log.info("llm chain: %s", {tier: [d.name for d in self.chain[tier]] for tier in TIERS})

    async def warm(self, timeout_s: float = 60.0) -> None:
        """Load local models before the first run; a cold Ollama load would eat a whole stage budget."""
        local = {d.name: d for tier in TIERS for d in self.chain[tier] if d.name.endswith(":ollama")}
        for name in local:
            try:
                await self._router.acompletion(model=name, messages=[{"role": "user", "content": "ok"}],
                                               max_tokens=1, timeout=timeout_s)
            except Exception as exc:
                # Warm-up is an optimization; a failure here shows up again, with context, in the first run.
                log.warning("warm-up of %s failed: %s", name, exc)

    def describe(self) -> dict[str, list[str]]:
        return {tier: [f"{d.name} ({d.litellm_params['model']})" for d in self.chain[tier]] for tier in TIERS}

    async def complete(
        self,
        tier: Tier,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        timeout_s: float,
    ) -> LLMReply:
        primary = self.chain[tier][0].name
        call_id = uuid.uuid4().hex
        kwargs: dict[str, Any] = {"metadata": {"assay_call_id": call_id}, "timeout": timeout_s}
        if tools:
            kwargs["tools"] = tools
            # litellm routes any tools request through its MCP handler, which imports an optional package
            # this service does not use; plain function calling needs none of it.
            kwargs["_skip_mcp_handler"] = True
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            async with self._slots:
                response = await self._router.acompletion(model=primary, messages=messages, **kwargs)
        finally:
            failed_attempts = _attempts.take(call_id)
        answered_by = (getattr(response, "_hidden_params", None) or {}).get("model_id") or primary
        message = response.choices[0].message
        calls = []
        for raw in message.tool_calls or []:
            try:
                arguments = json.loads(raw.function.arguments or "{}")
            except json.JSONDecodeError:
                log.warning("dropping tool call %s with unparseable arguments", raw.function.name)
                continue
            if isinstance(arguments, dict):
                calls.append(ToolCall(name=raw.function.name, arguments=arguments))
        usage = getattr(response, "usage", None)
        return LLMReply(
            text=message.content or "",
            tool_calls=calls,
            deployment=answered_by,
            model=str(response.model or answered_by),
            fallback_used=answered_by != primary,
            retries=len(failed_attempts),
            tokens_in=int(getattr(usage, "prompt_tokens", 0) or 0),
            tokens_out=int(getattr(usage, "completion_tokens", 0) or 0),
        )


def parse_json_object(text: str) -> dict[str, Any]:
    """The first JSON object in a model reply. Small models sometimes wrap it in prose or fences."""
    start = text.find("{")
    if start < 0:
        raise ValueError("model reply contains no JSON object")
    decoder = json.JSONDecoder()
    while start >= 0:
        try:
            parsed, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(parsed, dict):
            return parsed
        start = text.find("{", start + 1)
    raise ValueError("model reply contains no parseable JSON object")
