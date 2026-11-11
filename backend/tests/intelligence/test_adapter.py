"""Local HTTP wire checks. No external model or provider account is used."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aftercare.intelligence.graph import TOOLS
from aftercare.intelligence.models import ModelSpec, _transport, tool_reply


@pytest.mark.parametrize("interface", ["chat", "responses"])
def test_langchain_tool_round_trip(interface, monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, request))
            first = len(requests) == 1
            if interface == "chat":
                message = {"role": "assistant", "content": '{"notes":"ok"}'}
                if first:
                    message.update(
                        content=None,
                        tool_calls=[
                            {
                                "id": "call_test",
                                "type": "function",
                                "function": {"name": "read_case_facts", "arguments": "{}"},
                            }
                        ],
                    )
                body = {
                    "id": "chatcmpl_test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if first else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
                }
            else:
                item = (
                    {
                        "type": "function_call",
                        "name": "read_case_facts",
                        "arguments": "{}",
                        "call_id": "call_test",
                        "id": "fc_test",
                        "status": "completed",
                    }
                    if first
                    else {
                        "id": "msg_test",
                        "role": "assistant",
                        "type": "message",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": '{"notes":"ok"}', "annotations": []}
                        ],
                    }
                )
                body = {
                    "id": "resp_test",
                    "object": "response",
                    "created_at": 1,
                    "model": "test-model",
                    "status": "completed",
                    "output": [item],
                    "usage": {
                        "input_tokens": 4,
                        "output_tokens": 6,
                        "total_tokens": 10,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                }
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    monkeypatch.setenv("AFTERCARE_TEST_MODEL_KEY", "loopback-only")
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            spec = ModelSpec(
                alias="economy",
                provider="loopback",
                model="test-model",
                endpoint=f"http://127.0.0.1:{server.server_port}/v1",
                api_key_env="AFTERCARE_TEST_MODEL_KEY",
                interface=interface,
            )
            prompt = HumanMessage(content="读取测试事实")
            first = _transport(spec, [prompt], TOOLS, 2, 256)
            assert first.tool_calls[0]["id"] == "call_test"
            result = _transport(
                spec,
                [
                    prompt,
                    AIMessage(content="", tool_calls=first.tool_calls),
                    tool_reply("read_case_facts", "call_test", {"status": "confirmed"}),
                ],
                [],
                2,
                256,
            )
            assert result.text == '{"notes":"ok"}'
            assert result.usage_metadata["output_tokens"] == 6
        finally:
            server.shutdown()
            thread.join(timeout=2)
    assert len(requests) == 2
    if interface == "chat":
        assert requests[-1][0] == "/v1/chat/completions"
        assert requests[-1][1]["messages"][-1]["tool_call_id"] == "call_test"
    else:
        assert requests[-1][0] == "/v1/responses"
        assert requests[-1][1]["input"][-1]["call_id"] == "call_test"


@pytest.mark.parametrize("interface", ["chat", "responses"])
@pytest.mark.parametrize("interrupt", [False, True])
def test_native_http_stream_requires_provider_terminal(interface, interrupt, monkeypatch):
    from aftercare.intelligence.models import _stream_transport
    from aftercare.intelligence.native_stream import StreamInterrupted

    payloads = []
    text = '{"notes":"中文"}'
    base = {
        "id": "resp_loop",
        "object": "response",
        "created_at": 1,
        "model": "loop-model",
        "status": "completed",
        "output": [],
        "usage": {
            "input_tokens": 8,
            "output_tokens": 6,
            "total_tokens": 14,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    if interface == "chat":
        header = {
            "id": "chatcmpl_loop",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "loop-model",
        }
        events = [
            {
                **header,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": delta},
                        "finish_reason": None,
                    }
                ],
            }
            for delta in (text[:10], text[10:])
        ]
        end = [
            {**header, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {
                **header,
                "choices": [],
                "usage": {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14},
            },
        ]
    else:
        events = [
            {
                "type": "response.created",
                "sequence_number": 0,
                "response": {**base, "status": "in_progress", "usage": None},
            },
            *[
                {
                    "type": "response.output_text.delta",
                    "sequence_number": n,
                    "content_index": 0,
                    "item_id": "msg_loop",
                    "output_index": 0,
                    "delta": delta,
                }
                for n, delta in enumerate((text[:10], text[10:]), start=1)
            ],
        ]
        end = [{"type": "response.completed", "sequence_number": 3, "response": base}]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events + ([] if interrupt else end):
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
            if not interrupt and interface == "chat":
                self.wfile.write(b"data: [DONE]\n\n")

    monkeypatch.setenv("AFTERCARE_TEST_MODEL_KEY", "loopback-only")
    emitted = []
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            spec = ModelSpec(
                alias="economy",
                provider="loopback",
                model="loop-model",
                endpoint=f"http://127.0.0.1:{server.server_port}/v1",
                api_key_env="AFTERCARE_TEST_MODEL_KEY",
                interface=interface,
                streaming=True,
                streaming_usage=True,
                streaming_compatibility_version="loopback-1",
                streaming_checked_at="2026-11-10",
            )
            args = spec, [HumanMessage(content="查询当前工单")], [], 3, 256, emitted.append
            if interrupt:
                with pytest.raises(StreamInterrupted) as failed:
                    _stream_transport(*args)
                assert failed.value.usage is None
            else:
                result = _stream_transport(*args)
                assert result.text == text
                assert result.usage_metadata["total_tokens"] == 14
        finally:
            server.shutdown()
            thread.join(timeout=2)
    assert "".join(emitted) == text
    assert payloads[0]["stream"] is True
