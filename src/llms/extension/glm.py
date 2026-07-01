"""ChatDeepSeek subclass that round-trips GLM ``reasoning_content`` across turns.

GLM (Zhipu / bigmodel) over the OpenAI-compatible ``/api/paas/v4/`` endpoint
returns chain-of-thought in ``reasoning_content`` (DeepSeek protocol). The base
``ChatDeepSeek`` captures it inbound (into ``additional_kwargs['reasoning_content']``)
but drops it on the way back out. Unlike DeepSeek's own API, bigmodel paas/v4
*accepts* ``reasoning_content`` on input assistant messages, so we re-attach it to
preserve the model's prior thinking through an agent loop.
"""

from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_core.prompt_values import PromptValue
from langchain_deepseek import ChatDeepSeek


class ChatGLM(ChatDeepSeek):
    """ChatDeepSeek for GLM/bigmodel paas/v4 that preserves reasoning across turns.

    Re-injects each assistant message's ``reasoning_content`` (captured inbound by
    ChatDeepSeek) back into the outbound payload so chain-of-thought survives the
    agent loop. bigmodel tolerates the field on input; DeepSeek's own API does not,
    which is why the base class strips it.

    For the re-injection to be honored, the model config must set
    ``extra_body.thinking.clear_thinking = false``; bigmodel defaults it to ``true``,
    which strips prior-turn ``reasoning_content`` server-side before the model reads
    it (glm-5.2 ignores it entirely without the flag; glm-4.6 reads it either way).
    """

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = input_.to_messages() if isinstance(input_, PromptValue) else input_
        if not isinstance(messages, list):
            return payload
        # Source messages map 1:1, in order, onto payload["messages"]; copy the
        # reasoning each AIMessage carried in additional_kwargs onto its dict.
        for source, serialized in zip(messages, payload.get("messages", [])):
            if not isinstance(source, AIMessage):
                continue
            if serialized.get("role") != "assistant":
                continue
            reasoning = source.additional_kwargs.get("reasoning_content")
            if reasoning:
                serialized["reasoning_content"] = reasoning
        return payload
