import json
from summarize_review_benchmark import summarize,wilson,paired


def test_missing_rows_never_pass_gate(tmp_path):
    data={'samples':[{'id':'x','task_id':'1','kind':'wrong_write','split':'holdout','expected':'revise'}]}
    report=summarize(data,tmp_path)
    assert report['gate']=='incomplete' and not report['H4'] and not report['H5']
    assert report['models']['M1']['wrong_write']['missing']==1
    assert report['models']['M1']['wrong_write']['rate'] is None


def test_default_allow_counts_as_missed_error_not_dropped(tmp_path):
    sample={'id':'x','task_id':'1','kind':'wrong_write','split':'holdout','expected':'revise'}
    (tmp_path/'x-M1.json').write_text(json.dumps({'id':'x','mode':'M1','status':'complete','expected':'revise',
        'correct':True,'result':{'verdict':'allow','fallback_reason':'TimeoutError'}}))
    report=summarize({'samples':[sample]},tmp_path)
    stats=report['models']['M1']['wrong_write']
    assert stats['rate']==0 and stats['technical_fallbacks']==1


def test_intervals_and_paired_equal_results():
    assert wilson(0,0) is None
    assert wilson(0,10)[1]>0 and wilson(10,10)[0]<1
    rows=[({'kind':'wrong_write','task_id':str(i)},{'correct':True},{'correct':True}) for i in range(5)]
    result=paired(rows,'wrong_write')
    assert result['difference']==0 and result['ci95']==[0,0] and result['mcnemar_p']==1
