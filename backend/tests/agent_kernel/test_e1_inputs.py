import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'scripts'))
from run_review_benchmark import request_data


def test_no_labels_private_metadata_or_official_target_in_request():
    sample={'expected':'revise','kind':'wrong_write','task_id':'42','official_target':'secret',
        'messages':[{'role':'user','content':'Return mug','raw_data':{'secret':'hidden'}}],
        'proposal':{'id':'a','name':'cancel_pending_order','arguments':{'order_id':'o'}}}
    messages,proposal,guard,executor=request_data(sample,'public policy')
    assert messages==[{'id':'m0','role':'user','content':'Return mug'}]
    assert 'secret' not in str(executor)
    assert 'valid JSON only' in executor[0]['content']
    assert 'batch independent calls' in executor[0]['content']  # exact historical executor context
