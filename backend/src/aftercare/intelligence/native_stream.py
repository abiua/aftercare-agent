"""Assemble a single native provider stream before handing any tool to the workflow."""

from collections.abc import Callable, Iterable
from time import monotonic

from langchain_core.messages import AIMessage, AIMessageChunk, message_chunk_to_message


class StreamInterrupted(Exception):
    def __init__(self, usage: dict | None = None, code: str = "MODEL_STREAM_INTERRUPTED"):
        self.usage, self.code = usage, code
        super().__init__(code)


def collect_stream(
    chunks: Iterable[AIMessageChunk],
    *,
    interface: str,
    deadline: float,
    on_delta: Callable[[str], None],
) -> AIMessage:
    aggregate = None
    saw_terminal = False
    buffered = ""
    last_flush = monotonic()
    iterator = iter(chunks)
    try:
        for chunk in iterator:
            if monotonic() >= deadline:
                raise TimeoutError
            if not isinstance(chunk, AIMessageChunk):
                raise TypeError("Unexpected stream chunk")
            aggregate = chunk if aggregate is None else aggregate + chunk
            meta = chunk.response_metadata
            saw_terminal |= (
                meta.get("status") in {"completed", "incomplete", "failed", "cancelled"}
                if interface == "responses"
                else bool(meta.get("finish_reason"))
            )
            # AIMessageChunk.text excludes reasoning blocks and tool-argument chunks.
            buffered += chunk.text
            now = monotonic()
            if buffered and (len(buffered) >= 160 or now - last_flush >= 0.15):
                on_delta(buffered)
                buffered, last_flush = "", now
        if buffered:
            on_delta(buffered)
        if aggregate is None or not saw_terminal:
            raise ValueError("No provider terminal event")
    except Exception as error:
        if aggregate is None:
            raise
        # End-of-stream without a provider terminal marker cannot authorize any action.
        usage = aggregate.usage_metadata if saw_terminal else None
        code = getattr(error, "code", None)
        if code not in {"EXPERIMENT_CANCELLED", "RUN_INACTIVE", "RUN_ACTOR_REVOKED"}:
            code = "MODEL_STREAM_INTERRUPTED"
        raise StreamInterrupted(usage, code) from error
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()
    return message_chunk_to_message(aggregate)
