import unittest
from unittest.mock import patch
from tau2.data_model.message import AssistantMessage, UserMessage
from tau2.domains.retail.environment import get_environment
from tau_official_agents import create_agent


class KernelOfficialAdapterTests(unittest.TestCase):
    def test_s1_retries_bad_auth_reply_and_keeps_plain_customer_text(self):
        env=get_environment()
        agent=create_agent('S1',env.get_tools(),env.get_policy(),'gpt-6.1-sol')
        state=agent.get_init_state()
        calls=[]
        def fake(**kwargs):
            calls.append(kwargs)
            return AssistantMessage(role='assistant',content='Your username?' if len(calls)==1 else 'Your email, or first and last name and ZIP code?')
        with patch('tau_official_agents.generate',fake):
            reply,state=agent.generate_next_message(UserMessage(role='user',content='Return my order'),state)
        self.assertEqual(len(calls),2)
        self.assertEqual(reply.content,'Your email, or first and last name and ZIP code?')
        self.assertEqual(len(state.messages),2)
        self.assertNotIn('valid JSON only',calls[0]['messages'][0].content)
        self.assertEqual(calls[0]['aftercare_role'],'executor')
