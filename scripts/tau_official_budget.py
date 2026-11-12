"""Single-attempt, budgeted Responses transport for the official text simulator."""

from __future__ import annotations

import json
import os
import threading
from decimal import Decimal

PER_MILLION = Decimal(1_000_000)
AGENT = "gpt-6.1-sol"
USER = "gpt-6.1-terra"
REVIEW = "gpt-5.4-mini-2026-03-17"
PRICES = {
    AGENT: ("2", "10", "0.10"),
    USER: ("1", "5", "0.05"),
    REVIEW: ("0.75", "4.50", "0.075"),
}


class BudgetExhausted(RuntimeError):
    pass


class ProviderResultUnknown(RuntimeError):
    pass


class Budget:
    def __init__(self, max_calls, max_usd, journal):
        self.max_calls = max_calls
        self.max_usd = Decimal(str(max_usd))
        self.journal = journal
        self.calls = 0
        self.committed = Decimal(0)
        self.lock = threading.Lock()

    def _write(self, event):
        with self.journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def reserve(self, row, role, stage, amount):
        with self.lock:
            if self.calls >= self.max_calls or self.committed + amount > self.max_usd:
                raise BudgetExhausted("campaign call or cost limit")
            call_id = self.calls + 1
            self._write({"call_id": call_id, "row": row, "role": role, "stage": stage, "reserve_usd": str(amount)})
            self.calls = call_id
            self.committed += amount
            return call_id

    def record_failure(self, call_id, error):
        status = getattr(error, "status_code", None)
        with self.lock:
            self._write({"call_id": call_id, "failure_type": type(error).__name__,
                         "status_code": status if type(status) is int else None})

    def settle(self, call_id, reserved, actual, **usage):
        with self.lock:
            self._write({"call_id": call_id, "actual_usd": str(actual), **usage})
            self.committed += actual - reserved
            if actual > reserved:
                # Accounting bound no longer holds. Block future submissions.
                self.max_calls = self.calls


def responses_input(messages):
    items = []
    for message in messages:
        if message["role"] == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": message["tool_call_id"],
                "output": message["content"],
            })
            continue
        if message.get("content"):
            items.append({"role": message["role"], "content": message["content"]})
        for call in message.get("tool_calls") or []:
            items.append({
                "type": "function_call",
                "call_id": call["id"],
                "name": call["function"]["name"],
                "arguments": call["function"]["arguments"],
            })
    return items


class RowTransport:
    def __init__(self, row, limits, budget, client):
        self.row = row
        self.limits = limits
        self.budget = budget
        self.client = client
        self.calls = dict.fromkeys(("agent", "user", "review"), 0)
        self.costs = dict.fromkeys(self.calls, Decimal(0))
        self.failure_status = None
        self.lock = threading.Lock()
        self.calls_by_stage = {}
        self.usage = {role: {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
                      for role in self.calls}

    def metrics(self):
        return {
            **{f"{role}_calls": count for role, count in self.calls.items()},
            **{f"{role}_cost_usd": None if cost is None else float(cost)
               for role, cost in self.costs.items()},
            "token_usage": self.usage, "calls_by_stage": self.calls_by_stage,
        }

    def completion(self, *, model, messages, tools, tool_choice, call_name=None, **kwargs):
        import tiktoken
        from litellm import ModelResponse

        model = model.split("/")[-1]
        role = {AGENT: "agent", USER: "user", REVIEW: "review"}[model]
        inputs = responses_input(messages)
        function_tools = [
            {"type": "function", "strict": False, **tool["function"]}
            for tool in tools or []
        ]
        payload = {
            "model": model,
            "input": inputs,
            "max_output_tokens": self.limits["max_output_tokens_per_call"],
            "reasoning": {"effort": kwargs.get("reasoning_effort", "low")},
            "store": False,
            "service_tier": "default",
        }
        if model in {AGENT, USER}:
            # Explicit mode with no breakpoints disables chargeable cache writes.
            payload["extra_body"] = {"prompt_cache_options": {"mode": "explicit"}}
        if function_tools:
            payload.update(tools=function_tools, tool_choice=tool_choice or "auto", parallel_tool_calls=False)
        # Reserve uncached input plus full output. Serialization and per-item
        # overhead are included with a 2x token margin.
        encoding = tiktoken.get_encoding("o200k_base")
        encoded = encoding.encode_ordinary(json.dumps(payload, ensure_ascii=False))
        input_bound = 2 * len(encoded) + 1024
        in_price, out_price, cache_price = map(Decimal, PRICES[model])
        reserve = (input_bound * in_price + payload["max_output_tokens"] * out_price) / PER_MILLION
        with self.lock:
            if self.failure_status == "unknown":
                raise ProviderResultUnknown("previous provider result unknown")
            if self.calls[role] >= self.limits[f"max_{role}_calls_per_run"]:
                self.failure_status = "budget_exhausted"
                raise BudgetExhausted(f"{role} call limit")
            stage = call_name or role
            try:
                call_id = self.budget.reserve(self.row, role, stage, reserve)
            except BudgetExhausted:
                self.failure_status = "budget_exhausted"
                raise
            self.calls[role] += 1
            self.calls_by_stage[stage] = self.calls_by_stage.get(stage, 0) + 1
        try:
            response = self.client.responses.create(**payload)
            usage = response.usage
            cached = getattr(usage.input_tokens_details, "cached_tokens", 0) or 0
            billed = ((usage.input_tokens - cached) * in_price + cached * cache_price
                      + usage.output_tokens * out_price) / PER_MILLION
        except Exception as error:
            with self.lock:
                self.failure_status = "unknown"
                self.costs[role] = None
                self.budget.record_failure(call_id, error)
            raise ProviderResultUnknown(type(error).__name__) from None
        counts = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                  "cached_input_tokens": cached}
        with self.lock:
            self.budget.settle(call_id, reserve, billed, response_id=response.id, **counts)
            if self.costs[role] is not None:
                self.costs[role] += billed
            for key, value in counts.items():
                self.usage[role][key] += value
        if response.status != "completed":
            raise RuntimeError(f"Responses status: {response.status}")
        function_calls = [
            {"id": part.call_id, "type": "function",
             "function": {"name": part.name, "arguments": part.arguments}}
            for part in response.output if part.type == "function_call"
        ]
        model_response = ModelResponse(
            id=response.id, model=model, choices=[{
                "index": 0, "finish_reason": "tool_calls" if function_calls else "stop",
                "message": {"role": "assistant", "content": response.output_text or None,
                            "tool_calls": function_calls or None},
            }],
            usage={"prompt_tokens": usage.input_tokens, "completion_tokens": usage.output_tokens,
                   "total_tokens": usage.input_tokens + usage.output_tokens},
        )
        model_response._hidden_params["aftercare_cost_usd"] = float(billed)
        return model_response
