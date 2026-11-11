"""Check a real authorized S3 URL before and after its natural expiry."""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    destination = ROOT / 'reports/material-expiry-http.json'
    if destination.exists():
        raise SystemExit('Preserve existing evidence; use a new report path.')
    users = json.loads((ROOT / '.runtime/users-test.json').read_text())
    token = next(user['token'] for user in users if user['role'] == 'support')
    case_id = json.loads((ROOT / 'reports/business-full-flow.json').read_text())['case_id']

    def api(path):
        request = urllib.request.Request('http://127.0.0.1:8022/api' + path,
                                        headers={'Authorization': 'Bearer ' + token})
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def get(url):
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    material = api(f'/cases/{case_id}/materials')['items'][0]
    before = time.monotonic()
    link = api(f"/cases/{case_id}/materials/{material['id']}/link")
    parsed = urllib.parse.urlsplit(link['url'])
    assert parsed.hostname == '127.0.0.1' and parsed.port == 58351
    assert link['expires_in'] == 60
    assert urllib.parse.parse_qs(parsed.query)['X-Amz-Expires'] == ['60']
    initial, content, headers = get(link['url'])
    assert initial == 200
    assert hashlib.sha256(content).hexdigest() == material['sha256']
    anonymous, _, _ = get(urllib.parse.urlunsplit(parsed._replace(query='')))
    assert anonymous in (401, 403)
    print('Signed object bytes verified; waiting for the original 60-second URL to expire.', flush=True)
    while time.monotonic() - before < 65:
        time.sleep(min(5, 65 - (time.monotonic() - before)))
    expired, _, _ = get(link['url'])
    report = {'status': 'PASSED' if expired in (401, 403) else 'FAILED',
              'source': 'real_test_http_s3_natural_expiry', 'case_id': case_id,
              'material_id': material['id'], 'initial_http': initial,
              'anonymous_http': anonymous, 'expired_http': expired,
              'configured_expiry_seconds': 60,
              'elapsed_seconds': round(time.monotonic() - before, 3),
              'initial_bytes_match': True, 'cache_control': headers.get('Cache-Control'),
              'clock_or_signature_modified': False}
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    assert report['status'] == 'PASSED'


if __name__ == '__main__':
    main()
