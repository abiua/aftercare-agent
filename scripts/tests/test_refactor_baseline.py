from refactor_baseline import classify


def test_outcome_precedence_and_auth_errors():
    row={'status':'completed','official_reward':0}
    calls={'role':'assistant','tool_calls':[{'id':'a','name':'find_user_id_by_email'},{'id':'b','name':'modify_pending_order_items'}]}
    failed={'role':'tool','id':'a','content':'Error: user not found','error':False,'execution_status':'executed'}
    assert classify(row,[calls,failed])=='authentication_failed'
    assert classify(row,[calls,{**failed,'content':'user_1'}])=='inaction'
    write={'role':'tool','id':'b','execution_status':'executed'}
    assert classify(row,[calls,write])=='wrong_write'
    assert classify({**row,'status':'budget_exhausted'},[calls,write])=='unfinished'
    assert classify({**row,'official_reward':1},[calls,write])=='success'
