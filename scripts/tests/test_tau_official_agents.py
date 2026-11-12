"""Offline role and tool-path regression for the official Retail adapter."""

from contextvars import ContextVar
from importlib.util import find_spec
import json
from threading import Barrier
import unittest
from unittest.mock import patch

if find_spec("tau2") is None:
    raise unittest.SkipTest("Requires the pinned official τ-bench environment")

from tau2.data_model.message import AssistantMessage, ToolCall, ToolMessage, UserMessage
from tau2.domains.retail.environment import get_environment

from scripts.tau_official_agents import _authenticated_scope, create_agent


class OfficialAgentTests(unittest.TestCase):
    def test_public_scope_ignores_unusable_and_foreign_results(self):
        evidence = (
            ("find_user_id_by_email", {"email": "customer@example.com"}, "self"),
            ("get_user_details", {"user_id": "self"}, "malformed"),
            ("get_order_details", {"order_id": "foreign"},
             '{"user_id":"other","order_id":"foreign"}'),
            ("get_user_details", {"user_id": "self"},
             '{"user_id":"self","orders":["own",null,[]]}'),
        )
        messages = []
        for index, (name, arguments, result) in enumerate(evidence):
            call = ToolCall(id=str(index), name=name, arguments=arguments)
            messages.append(AssistantMessage(role="assistant", tool_calls=[call]))
            messages.append(ToolMessage(id=call.id, role="tool", content=result))
        self.assertEqual(_authenticated_scope(messages), ("self", {"own"}))

    def test_multi_role_reads_are_parallel_bounded_and_scoped(self):
        env = get_environment()
        tools = env.get_tools()
        db = env.tools.db
        user_id, user = next(iter(db.users.items()))
        before = user.model_dump()

        def call(tool, ident, **arguments):
            return ToolCall(id=ident, name=tool, arguments=arguments)

        auth = call("find_user_id_by_email", "auth", email=user.email)
        auth_result = env.get_response(auth)
        self.assertEqual(auth_result.content, user_id)

        history = [
            UserMessage(role="user", content="I need help with my order."),
            AssistantMessage(role="assistant", tool_calls=[auth]),
        ]
        agent = create_agent("multi_agent", tools, env.get_policy(), "openai/gpt-6.1-sol")
        state = agent.get_init_state(history)
        rendezvous = Barrier(2)
        row = ContextVar("row")
        row.set("test-row")
        roles = []

        def fake_generate(*, messages, tools, call_name, **_kwargs):
            self.assertEqual(row.get(), "test-row")
            roles.append(call_name)
            tool_names = {tool.name for tool in tools}
            prompt = messages[0].content
            if call_name == "agent_response":
                self.assertIn("cancel_pending_order", tool_names)
                return AssistantMessage(role="assistant", content="Public reply")
            self.assertNotIn("cancel_pending_order", tool_names)
            self.assertNotIn("transfer_to_human_agents", tool_names)
            if call_name in {"agent_fact_analysis", "agent_policy_review"}:
                rendezvous.wait(timeout=5)
                self.assertEqual(len([m for m in messages if m.role == "system"]), 1)
            if call_name == "agent_fact_analysis":
                self.assertNotIn("<policy>", prompt)
                actions = [
                    call("get_user_details", "facts_read", user_id=user_id),
                    call("get_user_details", "extra_read", user_id=user_id),
                ]
            elif call_name == "agent_policy_review":
                self.assertIn("<policy>", prompt)
                actions = [call("cancel_pending_order", "write", order_id="unknown")]
            else:
                self.assertIn("facts_read", messages[1].content)
                actions = [call("get_user_details", "verify_read", user_id="other-user")]
            return AssistantMessage(
                role="assistant", tool_calls=actions, usage={"total_tokens": 123}, cost=0.01
            )

        with patch("scripts.tau_official_agents.generate", fake_generate):
            _, state = agent.generate_next_message(auth_result, state)
            followup_result = env.get_response(
                call("get_user_details", "public_followup", user_id=user_id)
            )
            _, state = agent.generate_next_message(followup_result, state)

        self.assertEqual(roles.count("agent_fact_analysis"), 1)
        self.assertEqual(roles.count("agent_policy_review"), 1)
        self.assertEqual(roles.count("agent_counterexample"), 1)
        self.assertEqual(roles.count("agent_response"), 2)
        observations = {e["id"]: e for e in agent.private_trace if e["event"] == "private_tool"}
        self.assertEqual(observations["facts_read"]["status"], "executed")
        self.assertEqual(json.loads(observations["facts_read"]["result"])["user_id"], user_id)
        for call_id in ("extra_read", "write", "verify_read"):
            self.assertEqual(observations[call_id]["status"], "rejected")
        self.assertEqual(db.users[user_id].model_dump(), before)
        self.assertEqual(len(state.messages), 6)
        self.assertTrue(all(message.role != "system" for message in state.messages))
        costs = [e["cost_usd"] for e in agent.private_trace if e["event"] == "private_role"]
        self.assertEqual(costs, [0.01, 0.01, 0.01])


if __name__ == "__main__":
    unittest.main()
