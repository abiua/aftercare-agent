"""Source-bound bad cases; stale and missing evidence remain visible."""
import json
from hashlib import sha256
from pathlib import Path

from .investigations import authorize


def catalogue(root: Path, user):
    authorize(user)
    data = json.loads((root / 'data/bad-cases/catalog-v1.json').read_text())
    seen = set()
    for row in data['items']:
        if row['id'] in seen:
            raise ValueError('DUPLICATE_BAD_CASE')
        seen.add(row['id'])
        for ref in row['evidence']:
            path = (root / ref['path']).resolve()
            if not path.is_relative_to(root.resolve()) or Path(ref['path']).is_absolute():
                raise ValueError('BAD_CASE_EVIDENCE_OUT_OF_SCOPE')
            ref['verification'] = ('MISSING' if not path.is_file() else
                                   'MATCH' if sha256(path.read_bytes()).hexdigest() == ref['sha256'] else 'CHANGED')
    return data
