"""Post-run label-quality audit; does not relabel or rerun frozen E1 samples.

Deleting the last confirmation does not prove absence of authorization. This
conservative screen marks earlier assent for manual review rather than claiming
that the assent necessarily authorizes this exact proposal.
"""
import json
import re
from pathlib import Path
from build_review_benchmark import text

ASSENT=re.compile(r'\b(?:yes|yeah|yep|confirm|authorize|proceed|go ahead|sounds good|is fine|are fine)\b|确认|同意|可以办理',re.I)


def prior_authorization_candidates(messages):
    findings=[]
    previous_reply=None
    for index,message in enumerate(messages):
        if message.get('role')=='assistant' and message.get('content') and not message.get('tool_calls'):
            previous_reply=index
        if message.get('role')!='user' or previous_reply is None:continue
        value=text(message)
        if ASSENT.search(value):
            findings.append({'customer_message_index':index,'preceding_assistant_index':previous_reply,
                'customer_quote':value,'reason':'Earlier assent may authorize part or all of the current proposal; structural deletion is insufficient.'})
    return findings


def audit_sample(sample):
    if sample.get('mutation')!='remove_confirmation':
        return {'id':sample['id'],'applicable':False}
    findings=prior_authorization_candidates(sample['messages'])
    return {'id':sample['id'],'task_id':sample['task_id'],'applicable':True,
        'status':'needs_semantic_review' if findings else 'no_prior_assent_detected',
        'findings':findings,
        'label_changed':False,
        'limitation':'No prior marker is not proof of no authorization; implicit or differently phrased consent requires review.'}


if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    data=json.loads((root/'.runtime/e1-review/e1-candidates-v3.json').read_text())
    rows=[audit_sample(s) for s in data['samples'] if s['mutation']=='remove_confirmation']
    # Keep original conversation quotes local; publish only IDs and review flags.
    (root/'.runtime/e1-review/postrun-confirmation-audit.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False)+'\n')
    public=[{k:v for k,v in r.items() if k!='findings'}|{'earlier_assent_count':len(r['findings'])} for r in rows]
    (root/'evidence/architecture_refactor/e1_confirmation_quality.json').write_text(json.dumps({
        'post_hoc':True,'changes_frozen_scores':False,'purpose':'Future sample-generation regression, not a new evaluation',
        'rows':public},indent=2)+'\n')
    print(json.dumps({'checked':len(rows),'needs_review':sum(r['status']=='needs_semantic_review' for r in rows)}))
