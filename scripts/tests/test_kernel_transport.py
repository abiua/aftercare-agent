"""No paid calls: explicit roles, shared budget and provider wire shape."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as N
from tau_official_budget import Budget, RowTransport, BudgetExhausted
from scripts.tests.test_tau_official_campaign import LIMITS, fake_response


class KernelTransportTests(unittest.TestCase):
    def test_same_model_different_roles_and_shared_agent_budget(self):
        with TemporaryDirectory() as tmp:
            budget=Budget(20,5,Path(tmp)/'calls.jsonl')
            client=N(responses=N(create=lambda **kwargs: fake_response('OK')))
            row=RowTransport('r',{**LIMITS,'max_agent_calls_per_run':2},budget,client)
            for actor in ('executor','reviewer','user'):
                row.completion(model='gpt-6.1-sol',messages=[{'role':'user','content':'test'}],
                    tools=None,tool_choice=None,aftercare_role=actor)
            with self.assertRaises(BudgetExhausted):
                row.completion(model='gpt-6.1-sol',messages=[],tools=None,tool_choice=None,aftercare_role='fact_expert')
            self.assertEqual(row.calls,{'agent':2,'user':1,'review':0})
            self.assertEqual(row.actor_calls,{'executor':1,'reviewer':1,'user':1})

    def test_deepseek_executor_is_not_simulated_user(self):
        with TemporaryDirectory() as tmp:
            captured=[]
            def create(**kwargs):
                captured.append(kwargs)
                return N(id='fake',choices=[N(finish_reason='stop',message=N(content='OK',tool_calls=None))],
                    usage=N(prompt_tokens=100,completion_tokens=10,prompt_cache_hit_tokens=50))
            ds=N(chat=N(completions=N(create=create)))
            row=RowTransport('r',LIMITS,Budget(5,1,Path(tmp)/'calls.jsonl'),None,deepseek_client=ds)
            row.completion(model='deepseek-flash',messages=[{'role':'user','content':'test'}],
                tools=None,tool_choice=None,aftercare_role='executor')
            self.assertEqual(row.calls['agent'],1)
            self.assertEqual(row.calls['user'],0)
            self.assertEqual(captured[0]['extra_body'],{'thinking':{'type':'disabled'}})
            self.assertEqual(row.usage['agent']['cached_input_tokens'],50)

    def test_unknown_role_rejected_before_reservation(self):
        with TemporaryDirectory() as tmp:
            budget=Budget(5,1,Path(tmp)/'calls.jsonl')
            row=RowTransport('r',LIMITS,budget,None)
            with self.assertRaises(ValueError):
                row.completion(model='gpt-6.1-sol',messages=[],tools=None,tool_choice=None)
            self.assertEqual(budget.calls,0)

    def test_incomplete_responses_are_settled_and_raise_for_every_actor(self):
        from tau_official_budget import IncompleteResponse
        for provider in ('responses', 'deepseek'):
            for actor in ('executor', 'fact_expert', 'reviewer'):
                with self.subTest(provider=provider, actor=actor), TemporaryDirectory() as tmp:
                    response = fake_response('partial')
                    response.status = 'incomplete'
                    ds = N(chat=N(completions=N(create=lambda **kw: N(id='fake',
                        choices=[N(finish_reason='length', message=N(content='partial', tool_calls=None))],
                        usage=N(prompt_tokens=100, completion_tokens=1500)))))
                    budget = Budget(5, 1, Path(tmp)/'calls.jsonl')
                    row = RowTransport('r', LIMITS, budget, N(responses=N(create=lambda **kw: response)), ds)
                    with self.assertRaisesRegex(IncompleteResponse, 'Responses status: incomplete'):
                        row.completion(model='deepseek-flash' if provider == 'deepseek' else 'gpt-6.1-sol',
                            messages=[], tools=None, tool_choice=None, aftercare_role=actor)
                    self.assertIsNone(row.failure_status)
                    self.assertEqual(row.calls['agent'], 1)
                    self.assertGreater(row.costs['agent'], 0)
                    self.assertIn('actual_usd', budget.journal.read_text())
