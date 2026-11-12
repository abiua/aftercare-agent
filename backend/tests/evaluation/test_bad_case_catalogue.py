import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from aftercare.evaluation.bad_cases import catalogue


def test_catalogue_keeps_missing_and_changed_evidence(tmp_path):
    folder = tmp_path / 'data/bad-cases'
    folder.mkdir(parents=True)
    file = tmp_path / 'original.json'
    file.write_text('{}')
    refs = [{'path':'original.json','sha256':sha256(b'{}').hexdigest()},
            {'path':'original.json','sha256':'wrong'}, {'path':'missing.json','sha256':'none'}]
    path = folder / 'catalog-v1.json'
    path.write_text(json.dumps({'items':[{'id':'one','evidence':refs,'status':'OPEN'}]}))
    result = catalogue(tmp_path, SimpleNamespace(role='tester'))
    assert [r['verification'] for r in result['items'][0]['evidence']] == ['MATCH','CHANGED','MISSING']
    assert result['items'][0]['status'] == 'OPEN'
    assert 'verification' not in path.read_text()
    refs[0]['path'] = '../outside.json'
    path.write_text(json.dumps({'items':[{'id':'one','evidence':refs}]}))
    with pytest.raises(ValueError, match='OUT_OF_SCOPE'):
        catalogue(tmp_path, SimpleNamespace(role='tester'))
