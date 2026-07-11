"""Middleware for OpenAI explicit prompt-cache breakpoints (GPT-5.6+).

OpenAI analog of ``AnthropicPromptCachingMiddleware``: tags the last system
content block it sees with a ``prompt_cache_breakpoint`` marker so the static
prefix (system prompt + skills) is written to cache at a stable boundary.
Dynamic-context middlewares (workspace, runtime) run innermost and append
after the marker, keeping the cached prefix stable across requests.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI

from src.llms.endpoints import is_official_openai_endpoint

# Explicit-breakpoint marker forwarded verbatim onto the wire content part.
_BREAKPOINT: dict[str, Any] = {"mode": "explicit"}


class OpenAIPromptCachingMiddleware(AgentMiddleware):
    """Places an OpenAI prompt-cache breakpoint on the static system prefix.

    Applies only to ``ChatOpenAI`` models constructed with a non-None
    ``prompt_cache_options`` (opted in via the model manifest ``parameters``)
    AND pointed at api.openai.com — other backends (codex, platform proxy,
    OpenAI-compatible endpoints) reject or drop the marker, so anything with
    a non-official base_url passes through untouched. ``create_llm`` applies
    the same gate when attaching ``prompt_cache_options``; this check is
    defense-in-depth for clients constructed outside the factory.
    """

    @staticmethod
    def _should_apply(request: ModelRequest) -> bool:
        model = request.model
        return (
            isinstance(model, ChatOpenAI)
            and getattr(model, "prompt_cache_options", None) is not None
            and is_official_openai_endpoint(getattr(model, "openai_api_base", None))
        )

    @staticmethod
    def _tag_system_message(
        system_message: SystemMessage | None,
    ) -> SystemMessage | None:
        if system_message is None:
            return None
        content = system_message.content
        new_content: list[Any]
        if isinstance(content, str):
            if not content:
                return system_message
            new_content = [
                {"type": "text", "text": content, "prompt_cache_breakpoint": dict(_BREAKPOINT)}
            ]
        elif isinstance(content, list):
            if not content:
                return system_message
            new_content = list(content)
            last = new_content[-1]
            if isinstance(last, dict):
                new_content[-1] = {**last, "prompt_cache_breakpoint": dict(_BREAKPOINT)}
            elif isinstance(last, str):
                new_content[-1] = {
                    "type": "text",
                    "text": last,
                    "prompt_cache_breakpoint": dict(_BREAKPOINT),
                }
            else:
                return system_message
        else:
            return system_message
        return SystemMessage(content=new_content)

    def _apply(self, request: ModelRequest) -> ModelRequest:
        tagged = self._tag_system_message(request.system_message)
        if tagged is request.system_message:
            return request
        return request.override(system_message=tagged)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        if not self._should_apply(request):
            return handler(request)
        return handler(self._apply(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if not self._should_apply(request):
            return await handler(request)
        return await handler(self._apply(request))
