"""Provider-neutral chat + tool-calling layer.

The agent keeps the conversation as a list of `Turn`s. Each provider adapter
translates that list into its own wire format. Assistant turns keep the
provider's raw content (`raw`) so things like Gemini thought signatures and
Claude thinking blocks are passed back unchanged on the next request.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from .config import LLMConfig

log = logging.getLogger(__name__)

LLM_TIMEOUT_SECONDS = 60
MAX_OUTPUT_TOKENS = 800
REASONING_EXTRA_TOKENS = 4000  # room for thinking (billed only when used)


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False
    final: str | None = None  # set by action tools: the turn ends without another AI call


@dataclass
class Turn:
    role: Literal["user", "assistant", "tool"]
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    raw: Any = None
    provider: str | None = None
    usage: dict = field(default_factory=dict)  # input / cached / output tokens of this call


class LLMError(Exception):
    """Raised with a user-presentable message."""


class LLMProvider:
    name = "base"

    async def complete(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> Turn:
        raise NotImplementedError


# --------------------------------------------------------------------------
# Google Gemini (google-genai SDK)
# --------------------------------------------------------------------------
class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, cfg: LLMConfig):
        from google import genai
        from google.genai import types

        self.model = cfg.model
        # Without these the SDK has no timeout and retries 429s for minutes, so the bot looks dead.
        self.client = genai.Client(
            api_key=cfg.api_key,
            http_options=types.HttpOptions(
                timeout=LLM_TIMEOUT_SECONDS * 1000,
                retry_options=types.HttpRetryOptions(attempts=2, max_delay=10),
            ),
        )

    def _contents(self, history: list[Turn]) -> list:
        from google.genai import types

        out = []
        for t in history:
            if t.role == "user":
                out.append(types.Content(role="user", parts=[types.Part(text=t.text or "")]))
            elif t.role == "assistant":
                if t.provider == self.name and t.raw is not None:
                    out.append(t.raw)
                    continue
                parts = []
                if t.text:
                    parts.append(types.Part(text=t.text))
                for c in t.tool_calls:
                    parts.append(types.Part(function_call=types.FunctionCall(name=c.name, args=c.args)))
                out.append(types.Content(role="model", parts=parts or [types.Part(text="")]))
            else:
                parts = [
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=None if r.call_id.startswith("local-") else r.call_id,
                            name=r.name,
                            response={"error": r.content} if r.is_error else {"result": r.content},
                        )
                    )
                    for r in t.tool_results
                ]
                out.append(types.Content(role="user", parts=parts))
        return out

    async def complete(self, system, history, tools):
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name, description=t.description, parameters_json_schema=t.schema
                        )
                        for t in tools
                    ]
                )
            ],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            resp = await self.client.aio.models.generate_content(
                model=self.model, contents=self._contents(history), config=config
            )
        except errors.APIError as e:
            if e.code == 429:
                raise LLMError(
                    "Das Gemini-Kontingent ist gerade aufgebraucht (Limit pro Minute oder pro Tag). "
                    "Bitte in einer Minute nochmal."
                ) from e
            raise LLMError(f"Gemini-Fehler {e.code}: {e.message}") from e
        except (httpx.HTTPError, TimeoutError) as e:
            raise LLMError(f"Gemini antwortet nicht ({type(e).__name__}).") from e

        if not resp.candidates or resp.candidates[0].content is None:
            reason = getattr(resp.prompt_feedback, "block_reason", None) if resp.prompt_feedback else None
            return Turn(role="assistant", text=f"(Keine Antwort vom Modell{f': {reason}' if reason else ''})")

        content = resp.candidates[0].content
        texts, calls = [], []
        for p in content.parts or []:
            if p.function_call:
                calls.append(
                    ToolCall(
                        id=p.function_call.id or f"local-{uuid.uuid4().hex[:8]}",
                        name=p.function_call.name,
                        args=dict(p.function_call.args or {}),
                    )
                )
            elif p.text and not p.thought:
                texts.append(p.text)
        um = resp.usage_metadata
        usage = {
            "input": (um.prompt_token_count or 0) if um else 0,
            "cached": (um.cached_content_token_count or 0) if um else 0,
            "output": ((um.candidates_token_count or 0) + (um.thoughts_token_count or 0)) if um else 0,
        }
        return Turn(
            role="assistant",
            text="".join(texts).strip() or None,
            tool_calls=calls,
            raw=content,
            provider=self.name,
            usage=usage,
        )


# --------------------------------------------------------------------------
# Anthropic Claude (anthropic SDK)
# --------------------------------------------------------------------------
class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, cfg: LLMConfig):
        import anthropic

        self.model = cfg.model
        self.client = anthropic.AsyncAnthropic(
            api_key=cfg.api_key, base_url=cfg.base_url, timeout=LLM_TIMEOUT_SECONDS * 2, max_retries=1
        )

    def _messages(self, history: list[Turn]) -> list[dict]:
        out: list[dict] = []
        for t in history:
            if t.role == "user":
                out.append({"role": "user", "content": t.text or ""})
            elif t.role == "assistant":
                if t.provider == self.name and t.raw is not None:
                    out.append({"role": "assistant", "content": t.raw})
                    continue
                blocks: list[dict] = []
                if t.text:
                    blocks.append({"type": "text", "text": t.text})
                for c in t.tool_calls:
                    blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.args})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "…"}]})
            else:
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": r.call_id,
                                "content": r.content,
                                "is_error": r.is_error,
                            }
                            for r in t.tool_results
                        ],
                    }
                )
        return out

    async def complete(self, system, history, tools):
        import anthropic

        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=16000,
            system=system,
            cache_control={"type": "ephemeral"},  # system prompt + tools are identical on every call
            messages=self._messages(history),
            tools=[{"name": t.name, "description": t.description, "input_schema": t.schema} for t in tools],
        )
        try:
            if self.model.startswith(("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")):
                # Server-side refusal fallback, routed by refusal category.
                resp = await self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                resp = await self.client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise LLMError("Claude-Ratenlimit erreicht. Bitte gleich nochmal versuchen.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude-Fehler {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Claude ist nicht erreichbar.") from e

        if resp.stop_reason == "refusal":
            return Turn(role="assistant", text="Dabei kann ich leider nicht helfen.")

        texts, calls = [], []
        for b in resp.content:
            if b.type == "text":
                texts.append(b.text)
            elif b.type == "tool_use":
                calls.append(ToolCall(id=b.id, name=b.name, args=dict(b.input or {})))
        u = resp.usage
        usage = {
            "input": u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0),
            "cached": u.cache_read_input_tokens or 0,
            "output": u.output_tokens,
        }
        return Turn(
            role="assistant",
            text="".join(texts).strip() or None,
            tool_calls=calls,
            raw=resp.content,
            provider=self.name,
            usage=usage,
        )


# --------------------------------------------------------------------------
# OpenAI-compatible (OpenAI, OpenRouter, Groq, Ollama, LM Studio, ...)
# --------------------------------------------------------------------------
def _cache_key(tools: list) -> str:
    import hashlib

    names = ",".join(t.name for t in tools)
    return "arrbot-" + hashlib.sha1(names.encode()).hexdigest()[:8]


class OpenAICompatProvider(LLMProvider):
    name = "openai"

    def __init__(self, cfg: LLMConfig):
        import openai

        self.model = cfg.model
        self.reasoning = (cfg.reasoning or "").lower()  # reasoning_effort for thinking models; "" = model default
        self.reasoning_ok = True  # turned off when the model does not take the setting
        self.reasoning_fallback: str | None = None  # what works instead ("none" or None = leave out)
        self.max_output = cfg.max_output or MAX_OUTPUT_TOKENS
        self.client = openai.AsyncOpenAI(
            api_key=cfg.api_key or "not-needed", base_url=cfg.base_url, timeout=LLM_TIMEOUT_SECONDS, max_retries=1
        )

    def _messages(self, system: str, history: list[Turn]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for t in history:
            if t.role == "user":
                out.append({"role": "user", "content": t.text or ""})
            elif t.role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": t.text or ""}
                if t.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.args)},
                        }
                        for c in t.tool_calls
                    ]
                out.append(msg)
            else:
                for r in t.tool_results:
                    content = f"FEHLER: {r.content}" if r.is_error else r.content
                    out.append({"role": "tool", "tool_call_id": r.call_id, "content": content})
        return out

    async def complete(self, system, history, tools):
        import openai

        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=self._messages(system, history),
            # Same key for the same tool set -> requests with the same prompt start land on the same
            # cache (admins get extra tools, so they get their own key).
            prompt_cache_key=_cache_key(tools),
            tools=[
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.schema},
                }
                for t in tools
            ],
        )
        # Answers are short; the cap only guards against runaway output. Thinking models count their
        # thinking as output too, so they get more room.
        thinking = self.reasoning and self.reasoning_ok and self.reasoning != "none"
        kwargs["max_completion_tokens"] = self.max_output + (REASONING_EXTRA_TOKENS if thinking else 0)
        if self.reasoning and self.reasoning_ok:
            kwargs["reasoning_effort"] = self.reasoning
        elif self.reasoning_fallback:
            kwargs["reasoning_effort"] = self.reasoning_fallback
        try:
            try:
                resp = await self.client.chat.completions.create(**kwargs)
            except openai.BadRequestError as e:
                if not self.reasoning or "reason" not in str(e.message).lower():
                    raise
                # The model does not take this level here: older models (gpt-4o-mini ...) know no
                # reasoning_effort at all, newer ones (gpt-6-luna) think by default and allow tools in
                # Chat Completions only with reasoning_effort "none". Try "none", then without.
                log.warning("%s: reasoning_effort=%s geht nicht (%s) – ab jetzt ohne Denken", self.model,
                            kwargs.get("reasoning_effort", "-"), e.message)
                self.reasoning_ok = False
                kwargs["max_completion_tokens"] = self.max_output
                resp = None
                for fallback in ("none", None):
                    if fallback == kwargs.get("reasoning_effort"):
                        continue
                    if fallback:
                        kwargs["reasoning_effort"] = fallback
                    else:
                        kwargs.pop("reasoning_effort", None)
                    try:
                        resp = await self.client.chat.completions.create(**kwargs)
                    except openai.BadRequestError as e2:
                        if fallback is None or "reason" not in str(e2.message).lower():
                            raise
                        continue
                    self.reasoning_fallback = fallback
                    break
        except openai.RateLimitError as e:
            raise LLMError("Ratenlimit des KI-Anbieters erreicht. Bitte gleich nochmal.") from e
        except openai.APIStatusError as e:
            raise LLMError(f"KI-Fehler {e.status_code}: {e.message}") from e
        except openai.APIConnectionError as e:
            raise LLMError("KI-Anbieter ist nicht erreichbar.") from e

        msg = resp.choices[0].message
        calls = []
        for c in msg.tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=c.id, name=c.function.name, args=args))
        u = resp.usage
        usage = {}
        if u:
            details = getattr(u, "prompt_tokens_details", None)
            usage = {
                "input": u.prompt_tokens or 0,
                "cached": (getattr(details, "cached_tokens", 0) or 0) if details else 0,
                "output": u.completion_tokens or 0,
            }
        return Turn(
            role="assistant",
            text=(msg.content or "").strip() or None,
            tool_calls=calls,
            provider=self.name,
            usage=usage,
        )


def make_provider(cfg: LLMConfig) -> LLMProvider:
    return {
        "gemini": GeminiProvider,
        "anthropic": AnthropicProvider,
        "openai": OpenAICompatProvider,
    }[cfg.provider](cfg)
