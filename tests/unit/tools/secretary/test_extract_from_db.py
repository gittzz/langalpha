"""Coverage for the secretary's completed-thread DB reader and length cap.

``_extract_from_db`` used to fetch up to 10 responses and concatenate the
``message_chunk`` text of *every* turn into one blob, so reading back a thread
that had run several turns returned all prior answers mashed together instead
of just the most recent. The fix bounds the read to the requested window
(``turns``) and returns one text entry per turn.

``_join_recent_turns`` then caps the joined output at ``MAX_OUTPUT_CHARS`` on
real turn boundaries (taken from the list, never rediscovered by scanning the
text) so a turn whose own markdown contains ``---`` is not mistaken for a
turn separator.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.tools.secretary.utils import (
    MAX_OUTPUT_CHARS,
    _MAX_HISTORY_TURNS,
    _TURN_SEPARATOR,
    _extract_from_db,
    _join_recent_turns,
    _truncate_single,
)

_RECENT = "src.server.database.conversation.get_recent_responses_for_thread"


def _chunk(text: str) -> dict:
    return {"event": "message_chunk", "data": {"content_type": "text", "content": text}}


def _response(turn_index: int, *texts: str) -> dict:
    return {
        "conversation_response_id": f"r-{turn_index}",
        "conversation_thread_id": "t-1",
        "turn_index": turn_index,
        "status": "completed",
        "sse_events": [_chunk(t) for t in texts],
    }


# --- _extract_from_db: window + per-turn text -------------------------------


@pytest.mark.asyncio
async def test_turns_default_fetches_only_latest_turn():
    """turns=1 caps the DB read at the single latest turn (the bug fix).

    The original bug concatenated every turn; the fix asks the DB for limit=1,
    so a multi-turn thread cannot leak older turns into the output.
    """
    recent = AsyncMock(return_value=[_response(9, "latest answer")])

    with patch(_RECENT, recent):
        result = await _extract_from_db("t-1", turns=1)

    assert result == ["latest answer"]
    recent.assert_awaited_once_with("t-1", limit=1)


@pytest.mark.asyncio
async def test_turns_n_passes_limit_n():
    """turns=N reads the last N turns, oldest -> newest."""
    window = [_response(1, "turn one"), _response(2, "turn two")]
    recent = AsyncMock(return_value=window)

    with patch(_RECENT, recent):
        result = await _extract_from_db("t-1", turns=2)

    assert result == ["turn one", "turn two"]
    recent.assert_awaited_once_with("t-1", limit=2)


@pytest.mark.asyncio
async def test_turns_zero_requests_recent_history_clamped():
    """turns<=0 means 'recent history', clamped to the fetch ceiling (not None)
    so a giant thread can't pull every row — output is capped anyway."""
    window = [_response(0, "a"), _response(1, "b"), _response(2, "c")]
    recent = AsyncMock(return_value=window)

    with patch(_RECENT, recent):
        result = await _extract_from_db("t-1", turns=0)

    assert result == ["a", "b", "c"]
    recent.assert_awaited_once_with("t-1", limit=_MAX_HISTORY_TURNS)


@pytest.mark.asyncio
async def test_turns_large_n_is_clamped_to_ceiling():
    """An absurd N can't translate into an unbounded read."""
    recent = AsyncMock(return_value=[_response(0, "x")])

    with patch(_RECENT, recent):
        await _extract_from_db("t-1", turns=10_000)

    recent.assert_awaited_once_with("t-1", limit=_MAX_HISTORY_TURNS)


@pytest.mark.asyncio
async def test_concatenates_chunks_within_a_turn():
    """Multiple message_chunk events inside one turn join in order."""
    recent = AsyncMock(return_value=[_response(5, "Hello ", "world", "!")])

    with patch(_RECENT, recent):
        result = await _extract_from_db("t-1", turns=1)

    assert result == ["Hello world!"]


@pytest.mark.asyncio
async def test_no_responses_returns_empty_list():
    recent = AsyncMock(return_value=[])

    with patch(_RECENT, recent):
        result = await _extract_from_db("t-1")

    assert result == []


@pytest.mark.asyncio
async def test_turns_with_empty_text_are_dropped():
    """A turn with no text content leaves no empty entry in the list."""
    window = [_response(1, "kept"), _response(2), _response(3, "also kept")]
    recent = AsyncMock(return_value=window)

    with patch(_RECENT, recent):
        result = await _extract_from_db("t-1", turns=0)

    assert result == ["kept", "also kept"]


@pytest.mark.asyncio
async def test_filters_non_text_events():
    resp = {
        "turn_index": 4,
        "sse_events": [
            {"event": "tool_call", "data": {}},
            _chunk("only this"),
            {"event": "message_chunk", "data": {"content_type": "image", "content": "x"}},
        ],
    }
    recent = AsyncMock(return_value=[resp])

    with patch(_RECENT, recent):
        result = await _extract_from_db("t-1", turns=1)

    assert result == ["only this"]


@pytest.mark.asyncio
async def test_db_failure_propagates():
    """A read failure propagates (so the tool layer can surface an error)
    rather than being swallowed into an empty, success-looking result."""
    recent = AsyncMock(side_effect=RuntimeError("db down"))

    with patch(_RECENT, recent):
        with pytest.raises(RuntimeError):
            await _extract_from_db("t-1")


# --- _truncate_single: one turn, head-truncated -----------------------------


def test_truncate_single_under_limit_is_unchanged():
    assert _truncate_single("short output") == "short output"


def test_truncate_single_at_exactly_cap_is_unchanged():
    text = "y" * MAX_OUTPUT_CHARS
    assert _truncate_single(text) == text


def test_truncate_single_keeps_head():
    text = "A" * (MAX_OUTPUT_CHARS + 500)
    out = _truncate_single(text)

    assert out.startswith("A")
    assert out.endswith("[truncated — full output available in workspace]")
    assert "earlier turns truncated" not in out


# --- _join_recent_turns: list-aware length cap ------------------------------


def test_join_under_limit_joins_with_separator():
    assert _join_recent_turns(["a", "b"]) == f"a{_TURN_SEPARATOR}b"


def test_join_empty_returns_empty():
    assert _join_recent_turns([]) == ""
    assert _join_recent_turns(["", ""]) == ""


def test_single_turn_with_markdown_divider_is_not_a_turn_boundary():
    """Regression: a long SINGLE turn containing a markdown '---' rule keeps its
    head and is never relabeled as multiple truncated turns. The divider used
    to be read as a turn separator, gutting the answer to a fragment under a
    false '[earlier turns truncated]' banner.
    """
    # One turn whose body has a markdown horizontal rule well past the cap.
    text = "LEAD " + "x" * MAX_OUTPUT_CHARS + _TURN_SEPARATOR + "footer"
    out = _join_recent_turns([text])

    assert out.startswith("LEAD ")
    assert "earlier turns truncated" not in out
    assert out.endswith("[truncated — full output available in workspace]")


def test_join_multi_turn_drops_oldest_keeps_newest():
    """Over the cap, whole older turns are dropped from the front."""
    oldest = "O" * MAX_OUTPUT_CHARS  # alone nearly fills the cap
    out = _join_recent_turns([oldest, "middle", "NEWEST"])

    assert out.endswith("NEWEST")
    assert out.startswith("[earlier turns truncated")
    assert "OOO" not in out  # the oldest turn is gone entirely


def test_join_multi_turn_huge_newest_keeps_newest_head():
    """When the newest turn alone exceeds the cap, keep its head (the start of
    the most-recent answer) and drop older turns — not the tail of the newest.
    """
    newest = "NEWSTART " + "z" * (MAX_OUTPUT_CHARS + 100)
    out = _join_recent_turns(["OLD answer", newest])

    assert out.startswith("[earlier turns truncated")
    assert "NEWSTART " in out  # newest turn's head survives
    assert "OLD answer" not in out  # older turn dropped
    assert out.endswith("[truncated — full output available in workspace]")
