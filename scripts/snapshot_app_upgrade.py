"""Record counts and hashes, never row contents, across an app-only migration."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'reports/deployment/app-upgrade-preservation.json'


def sql(query):
    return subprocess.check_output([
        '/opt/homebrew/bin/docker', 'exec', 'aftercare-v2-pg-app-1',
        'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'ac_app', '-d', 'ac_app',
        '-Atc', query], text=True).strip()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def main():
    mode = sys.argv[1]
    if mode == 'before':
        assert not REPORT.exists(), 'Use a fresh report, preserve previous evidence'
        columns = json.loads(sql("SELECT json_object_agg(table_name, columns) FROM "
            "(SELECT table_name,json_agg(column_name ORDER BY ordinal_position) columns "
            "FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name<>'alembic_version' GROUP BY table_name) s"))
        report = {'columns': columns}
    else:
        assert mode == 'after'
        report = json.loads(REPORT.read_text())
    snapshot = {}
    for table, columns in report['columns'].items():
        projection = ','.join(map(quote, columns))
        query = ("SELECT json_build_object('count',count(*),'digest',"
                 "md5(coalesce(string_agg(md5(to_jsonb(t)::text),'' ORDER BY "
                 "md5(to_jsonb(t)::text)),''))) FROM "
                 f"(SELECT {projection} FROM public.{quote(table)}) t")
        snapshot[table] = json.loads(sql(query))
    credentials = ROOT / '.runtime/users-app.json'
    snapshot['credential_file'] = {
        'sha256': hashlib.sha256(credentials.read_bytes()).hexdigest(),
        'mode': oct(credentials.stat().st_mode & 0o777),
        'mtime_ns': credentials.stat().st_mtime_ns,
    }
    report[mode] = snapshot
    report[mode + '_migration'] = sql('SELECT version_num FROM public.alembic_version')
    if mode == 'after':
        report['preserved'] = report['before'] == snapshot
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'mode': mode, 'tables': len(snapshot)-1,
                      'migration': report[mode + '_migration'],
                      'preserved': report.get('preserved')}))
    if mode == 'after':
        assert report['preserved'], 'Existing app data changed during migration'


if __name__ == '__main__':
    main()
