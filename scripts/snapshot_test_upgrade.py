"""Read-only old-column hashes across the guarded test database upgrade."""

import hashlib
import json
import sys
from pathlib import Path

from sqlalchemy import text

from aftercare.config import ROOT, load_settings
from aftercare.db import guarded_engine


def main():
    mode, destination = sys.argv[1:]
    report_path = Path(destination)
    assert mode in {"before", "after"}
    engine = guarded_engine(load_settings("test"), test_only=True)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            with db.begin():
                db.execute(text("SET TRANSACTION READ ONLY"))
                if mode == "before":
                    assert not report_path.exists()
                    columns = db.execute(text(
                        "SELECT table_schema,table_name,json_agg(column_name ORDER BY ordinal_position) "
                        "FROM information_schema.columns WHERE "
                        "(table_schema='public' OR table_schema ~ '^eval_[0-9a-f]{32}$') "
                        "AND table_name <> 'alembic_version' GROUP BY table_schema,table_name"
                    )).all()
                    report = {"tables": [{"schema": s, "name": n, "columns": c} for s, n, c in columns]}
                else:
                    report = json.loads(report_path.read_text())
                snapshot = {}
                quote = db.dialect.identifier_preparer.quote_identifier
                for table in report["tables"]:
                    name = f'{quote(table["schema"])}.{quote(table["name"])}'
                    projection = ",".join(map(quote, table["columns"]))
                    result = db.execute(text(
                        "SELECT count(*),md5(coalesce(string_agg(md5(to_jsonb(t)::text),'' "
                        f"ORDER BY md5(to_jsonb(t)::text)),'')) FROM (SELECT {projection} FROM {name}) t"
                    )).one()
                    snapshot[name] = {"count": result[0], "digest": result[1]}
                credentials = ROOT / ".runtime/users-test.json"
                snapshot["credentials"] = {
                    "sha256": hashlib.sha256(credentials.read_bytes()).hexdigest(),
                    "mode": oct(credentials.stat().st_mode & 0o777),
                    "mtime_ns": credentials.stat().st_mtime_ns,
                }
                report[mode] = snapshot
                report[mode + "_migration"] = db.scalar(text("SELECT version_num FROM alembic_version"))
                if mode == "after":
                    report["preserved"] = report["before"] == snapshot
                report_path.write_text(json.dumps(report, indent=2))
                print(json.dumps({"mode": mode, "tables": len(report["tables"]),
                                  "migration": report[mode + "_migration"],
                                  "preserved": report.get("preserved")}))
                if mode == "after":
                    assert report["preserved"], "Existing test data changed"
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
