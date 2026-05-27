"""ChatOpenAI subclass for Codex store=false backends.

The official Codex CLI (codex-rs) marks the `id` field on Reasoning items
with #[serde(skip_serializing)] — IDs are read from responses but never
re-sent. With store=false, the server can't resolve these IDs → "Item not
found". We replicate that behavior here: strip `id` from reasoning items
and any item whose ID the server would try to look up.

encrypted_content is preserved so the model can resume reasoning across turns
(requires `include: ["reasoning.encrypted_content"]` in model parameters).
"""

import logging

from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# Prefixes of server-generated IDs that require store=true to resolve.
_UNPERSISTED_ID_PREFIXES = ("rs_",)


def _sanitize_input_for_stateless(items: list) -> list:
    """Sanitize a Responses API input array for store=false backends.

    Matches the official Codex CLI behavior (codex-rs/protocol/src/models.rs):
    - Strip `id` from reasoning/compaction items (skip_serializing in Rust)
    - Strip any item `id` with an unpersisted prefix (rs_)
    """
    result = []
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue

        item_type = item.get("type", "")
        item_id = item.get("id", "")

        # Strip id from reasoning/compaction items (Codex RS: skip_serializing)
        if item_type in ("reasoning", "compaction") or item_type.startswith("reasoning"):
            if item_id:
                item = {k: v for k, v in item.items() if k != "id"}
                logger.debug(f"[codex] Stripped id from {item_type} item (was {item_id[:20]}...)")
            result.append(item)
            continue

        # Safety net: strip any id with an unpersisted prefix
        if isinstance(item_id, str) and item_id.startswith(_UNPERSISTED_ID_PREFIXES):
            item = {k: v for k, v in item.items() if k != "id"}
            logger.debug(
                f"[codex] Stripped unpersisted id from type={item_type} "
                f"role={item.get('role', '')} (was {item_id[:20]}...)"
            )

        result.append(item)

    return result


def _extract_system_to_instructions(payload: dict) -> None:
    """Move system messages from input to the top-level ``instructions`` field.

    The Codex API rejects ``role:"system"`` in the input array. The Responses
    API equivalent is the top-level ``instructions`` parameter (what the
    official Codex CLI uses). Mutates *payload* in place.
    """
    items = payload.get("input")
    if not items:
        return

    system_parts: list[str] = []
    filtered: list = []

    for item in items:
        if isinstance(item, dict) and item.get("role") == "system":
            content = item.get("content", "")
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                # langchain-openai emits {"type": "input_text", "text": "..."}
                system_parts.extend(
                    block["text"]
                    for block in content
                    if isinstance(block, dict) and block.get("type") in ("text", "input_text")
                )
        else:
            filtered.append(item)

    if len(filtered) < len(items):
        # Always strip system messages from input (Codex rejects them)
        payload["input"] = filtered
        if system_parts:
            extracted = "\n\n".join(system_parts)
            existing = payload.get("instructions")
            # Append any model-level instructions after the system prompt
            payload["instructions"] = f"{extracted}\n\n{existing}" if existing else extracted
            logger.debug("[codex] Promoted %d system message(s) to instructions", len(system_parts))
        else:
            logger.debug("[codex] Stripped system message(s) with no text content")


class ChatCodexOpenAI(ChatOpenAI):
    """ChatOpenAI for Codex store=false backends.

    Replicates the official Codex CLI's behavior: reasoning item IDs are
    never re-sent to the server (they're marked skip_serializing in Rust).
    encrypted_content is preserved for reasoning continuity across turns.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        items = payload.get("input")
        if items:
            payload["input"] = _sanitize_input_for_stateless(items)
        # Codex API rejects role:"system" — promote to instructions field
        _extract_system_to_instructions(payload)
        return payload


def _install_responses_output_guard() -> None:
    """Coerce a null Responses-API ``output`` to ``[]`` before langchain iterates it.

    The chatgpt.com Codex backend ships ``response.output = null`` on terminal
    stream frames. langchain_openai (through 1.2.2, latest) iterates it unguarded
    in ``_construct_lc_result_from_responses_api`` and raises
    ``TypeError('NoneType' object is not iterable)``, killing the turn. The
    backend rejects the request-side ``exclude`` workaround with HTTP 400, so we
    patch the single chokepoint all read paths (sync, async, streaming) funnel
    through. Streamed text is already captured from ``output_text.delta`` events,
    so an empty terminal output loses no content.
    """
    try:
        import langchain_openai.chat_models.base as _base

        orig = _base._construct_lc_result_from_responses_api
    except (ImportError, AttributeError):
        # langchain_openai internals moved. Degrade to no guard rather than
        # breaking module import (which would take down all codex-oauth).
        logger.warning(
            "[codex] Responses output guard not installed — "
            "langchain_openai._construct_lc_result_from_responses_api is gone; "
            "codex-oauth may crash on a null terminal output frame"
        )
        return

    if getattr(orig, "_codex_output_guarded", False):
        return

    def _guarded(response, *args, **kwargs):
        if getattr(response, "output", None) is None:
            logger.debug("[codex] coerced null Responses output to [] (backend sent output=null)")
            try:
                response.output = []
            except Exception:
                object.__setattr__(response, "output", [])
        return orig(response, *args, **kwargs)

    _guarded._codex_output_guarded = True
    _base._construct_lc_result_from_responses_api = _guarded


_install_responses_output_guard()
