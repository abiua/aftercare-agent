from time import monotonic

import pytest
from langchain_core.messages import AIMessageChunk

from aftercare.intelligence.native_stream import StreamInterrupted, collect_stream


def consume(chunks, interface="chat"):
    deltas = []
    result = collect_stream(
        chunks,
        interface=interface,
        deadline=monotonic() + 5,
        on_delta=deltas.append,
    )
    return result, deltas


def test_stream_joins_tool_arguments_only_after_terminal():
    messages = [
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "read_policy",
                    "args": '{"policy_id":"POL',
                    "id": "call-1",
                    "index": 0,
                }
            ],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": None,
                    "args": '06","version":"2"}',
                    "id": None,
                    "index": 0,
                }
            ],
            response_metadata={"finish_reason": "tool_calls"},
        ),
        AIMessageChunk(
            content="",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 12,
                "total_tokens": 112,
            },
        ),
    ]
    result, deltas = consume(messages)
    assert result.tool_calls[0]["args"] == {"policy_id": "POL06", "version": "2"}
    assert result.usage_metadata["total_tokens"] == 112
    assert deltas == []


def test_stream_ignores_reasoning_and_emits_text_once():
    messages = [
        AIMessageChunk(
            content=[
                {"type": "reasoning", "reasoning": "private"},
                {"type": "text", "text": "中", "index": 1},
            ]
        ),
        AIMessageChunk(content=[{"type": "text", "text": "文", "index": 1}]),
        AIMessageChunk(content="", response_metadata={"status": "completed"}),
    ]
    result, deltas = consume(messages, "responses")
    assert deltas == ["中文"]
    assert result.text == "中文"


def test_partial_stream_without_terminal_remains_unknown_even_after_parseable_json():
    chunks = [AIMessageChunk(content='{"notes":"看来完成了"}')]
    with pytest.raises(StreamInterrupted) as raised:
        consume(chunks)
    assert raised.value.usage is None
