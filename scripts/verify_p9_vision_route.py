"""One bounded real vision request through the combined task registry."""
import json
import os
from decimal import Decimal
from hashlib import sha256

from aftercare_runtime_support import ROOT, write_new
from tau_p9_runtime import task_registry
from verify_tau_p5_controlled_live import open_ledger, read_ledger


def main():
    from aftercare.db import Base
    from aftercare.intelligence.models import ModelRunner
    from aftercare.intelligence.vision import PhotoSource, observe, prepare_photos
    folder=ROOT/'.runtime/p9-vision-route-20261112'
    folder.mkdir(mode=0o700,exist_ok=False)
    model=ModelRunner(task_registry()).with_evaluation_limits(calls=1,budget=Decimal('0.10'))
    originals={name:(ROOT/'data/vision-v1'/name).read_bytes() for name in ('synthetic-cracked-mug.png','synthetic-torn-parcel.png')}
    sources=[PhotoSource(name,'synthetic-message',sha256(data).hexdigest(),'image/png',len(data)) for name,data in originals.items()]
    photos=prepare_photos(sources,originals.__getitem__)
    write_new(folder/'admission.json',{'calls':1,'max_usd':'0.10','images':[p.audit_ref() for p in photos],'registry':model.registry.model_dump(mode='json')})
    engine,audit=open_ledger(folder/'ledger.sqlite',deadline=240)
    Base.metadata.create_all(engine)
    os.environ['AFTERCARE_OPENAI_KEY']=json.loads((ROOT/'.runtime/model-secret.json').read_text())['api_key']
    result={'status':'FAILED','task':'vision','preferred':'economy','source':'two synthetic original images','business_writes':False}
    try:
        result['observations']=observe(model,audit,photos,preferred='economy',input_binding='p9-vision-route-20261112',data_scope='customer_attachment')
        result['status']='SUCCEEDED'
    except Exception as error:  # noqa: BLE001 - preserve failure without another call
        result['error']=getattr(error,'code',type(error).__name__)
    finally:
        attempts,events=read_ledger(engine,audit.run_id,result['status'])
        result.update(attempts=attempts,events=events)
        write_new(folder/'result.json',result);engine.dispose()
    print(result['status'],len(attempts))


if __name__=='__main__':main()
