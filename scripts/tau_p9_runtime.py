"""Task-routed demo configuration; offline by default, explicit --live for API use."""
import argparse
import json
import os

from aftercare_runtime_support import registry
from tau_p5_runtime import API_PORT, ROOT, configure

DESTINATION=ROOT/'.runtime/p9-task-models.json'
VISION_SOURCE=ROOT/'.runtime/browser-demo-a8aldd4a/multimodal-vision-v1/models.json'


def task_registry():
    from aftercare.intelligence.models import Registry
    if DESTINATION.exists():
        existing=Registry.read(DESTINATION)
        if existing.version not in ('p9-text-vision-routing-v1', 'p9-deepseek-flash-20261113') or not any(m.alias=='economy' and m.vision for m in existing.models):
            raise ValueError('TASK_REGISTRY_VERSION_OR_VISION_CHANGED')
        return existing
    base=registry().model_dump(mode='json')
    economy=next(m for m in base['models'] if m['alias']=='economy')
    if economy['model'] == 'deepseek-flash' and economy.get('vision'):
        economy['data_scopes'].append('customer_attachment')
        base['version']='p9-deepseek-flash-20261113'
        return Registry.model_validate(base)
    vision=Registry.read(VISION_SOURCE).models[0]
    economy=next(m for m in base['models'] if m['alias']=='economy')
    if not vision.vision or vision.compatibility!='VERIFIED' or any(economy[k]!=getattr(vision,k) for k in ('provider','model','endpoint','interface')):
        raise ValueError('VISION_MODEL_IDENTITY_CHANGED')
    for key in ('vision','vision_checked_at','vision_compatibility_version','vision_input_tokens_per_image'):
        economy[key]=vision.model_dump(mode='json')[key]
    economy['data_scopes'].append('customer_attachment')
    base['version']='p9-text-vision-routing-v1'
    return Registry.model_validate(base)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['models','api','worker']);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    model=task_registry()
    encoded=model.model_dump_json(indent=2)
    if DESTINATION.exists() and DESTINATION.read_text()!=encoded:
        raise SystemExit('Existing routing registry differs; preserve it and use a new version.')
    if not DESTINATION.exists():
        fd=os.open(DESTINATION,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as output:output.write(encoded)
    if args.action=='models':
        print(json.dumps({'path':str(DESTINATION),'models':[{'alias':m.alias,'model':m.model,'vision':m.vision} for m in model.models]}));return
    configure()
    from aftercare import config
    previous=config.load_settings
    def settings(profile='test'):
        result=previous(profile)
        return config.Settings.model_validate({**result.model_dump(),
            'model_enabled':args.live, 'model_config_path':DESTINATION,
            'chat_vision_enabled':args.live, 'chat_vision_alias':'economy',
            'chat_analysis_max_microusd':100000,
            'chat_investigation_run_max_calls':12,
            'chat_investigation_issue_max_calls':32,
            'chat_investigation_issue_max_microusd':1500000,
            'chat_investigation_direct_recheck_request':True,
        })
    config.load_settings=settings
    if args.live:
        os.environ['AFTERCARE_OPENAI_KEY']=json.loads((ROOT/'.runtime/model-secret.json').read_text())['api_key']
        if any(m.provider == 'deepseek' for m in model.models):
            os.environ['AFTERCARE_DEEPSEEK_KEY']=json.loads((ROOT/'.runtime/deepseek-secret.json').read_text())['api_key']
    if args.action=='worker':
        from aftercare.queue import make_queue
        make_queue('test').worker_main(['worker','--pool=solo','--loglevel=INFO'])
        return
    import uvicorn
    from aftercare.api import create_app
    uvicorn.run(create_app('test'),host='127.0.0.1',port=API_PORT)


if __name__=='__main__':main()
