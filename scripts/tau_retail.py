"""Offline, isolated retail fixtures from a pinned τ²-bench revision."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data/tau/manifest.json"
WORKSPACE = ROOT / ".runtime/tau-retail"
COLLECTIONS = ("products", "users", "orders")
PROBE_ORDER_STATUS = "exchange requested"


def read_json(path: Path):
    return json.loads(path.read_bytes())


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def verify_asset(name: str, entry: dict, value: bytes) -> bytes:
    if len(value) != entry["size_bytes"] or digest(value) != entry["sha256"]:
        raise ValueError(f"Upstream source does not match the manifest: {name}")
    return value


def fetch_source(destination: Path) -> dict:
    manifest = read_json(MANIFEST)
    destination.mkdir(parents=True, exist_ok=True)
    for name, entry in manifest["files"].items():
        path = destination / name
        if path.exists():
            verify_asset(name, entry, path.read_bytes())
            continue
        with urlopen(entry["source_url"], timeout=30) as response:
            value = verify_asset(name, entry, response.read(entry["size_bytes"] + 1))
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as temporary:
            temporary.write(value)
        staged = Path(temporary.name)
        try:
            os.link(staged, path)
        except FileExistsError:
            verify_asset(name, entry, path.read_bytes())
        finally:
            staged.unlink(missing_ok=True)
    return {"revision": manifest["revision"], "verified_files": len(manifest["files"])}



def bundle_entries(bundle: Path) -> set[str]:
    return {path.relative_to(bundle).as_posix() for path in bundle.rglob("*")}


def verify_bundle(bundle: Path) -> None:
    manifest = read_json(MANIFEST)
    directories = {"agent", "runtime", "simulator", "evaluation"}
    fixed = {
        "agent/policy.md": "policy.md",
        "runtime/baseline.json": "db.json",
        "LICENSE": "LICENSE",
    }
    derived = {"runtime/task_index.json", "simulator/scenarios.json", "evaluation/references.json"}
    expected = directories | set(fixed) | derived | {"manifest.json"}
    if bundle.is_symlink() or bundle_entries(bundle) != expected:
        raise ValueError("Unexpected retail bundle contents")
    for name in expected:
        path = bundle / name
        if path.is_symlink() or (not path.is_dir() if name in directories else not path.is_file()):
            raise ValueError(f"Unexpected retail bundle entry: {name}")
    if read_json(bundle / "manifest.json") != manifest:
        raise ValueError("Retail bundle manifest differs from the pinned source")
    for name, source in fixed.items():
        verify_asset(name, manifest["files"][source], (bundle / name).read_bytes())
    integrity = manifest["derived_files"]
    if set(integrity) != derived:
        raise ValueError("Unexpected retail bundle integrity entries")
    for name, entry in integrity.items():
        verify_asset(name, entry, (bundle / name).read_bytes())

def import_source(source: Path, workspace: Path) -> dict:
    manifest = read_json(MANIFEST)
    assets = {
        name: verify_asset(name, entry, (source / name).read_bytes())
        for name, entry in manifest["files"].items()
    }

    database = json.loads(assets["db.json"])
    tasks = {task["id"]: task for task in json.loads(assets["tasks.json"])}
    splits = json.loads(assets["split_tasks.json"])
    split_by_task = {task_id: split for split in ("train", "test") for task_id in splits[split]}
    if (
        len(tasks) != 114
        or len(splits["train"]) != 74
        or len(splits["test"]) != 40
        or len(split_by_task) != 114
        or set(splits["base"]) != set(tasks)
        or set(split_by_task) != set(tasks)
        or any(task["initial_state"] is not None for task in tasks.values())
        or tuple(len(database[k]) for k in COLLECTIONS) != (50, 500, 1000)
    ):
        raise ValueError("Unexpected upstream retail schema or split coverage")

    if workspace.is_symlink():
        raise ValueError("Retail workspace must be local")
    workspace.mkdir(parents=True, exist_ok=True)
    bundle = workspace / "bundle"
    # Stage the entire bundle before publishing it. A concurrent import can
    # publish the same bundle first, in which case its files must match ours.
    with tempfile.TemporaryDirectory(dir=workspace) as temporary:
        staged = Path(temporary) / "bundle"
        for directory in ("agent", "runtime", "simulator", "evaluation"):
            (staged / directory).mkdir(parents=True)
        (staged / "agent/policy.md").write_bytes(assets["policy.md"])
        (staged / "LICENSE").write_bytes(assets["LICENSE"])
        (staged / "runtime/baseline.json").write_bytes(assets["db.json"])
        write_json(staged / "runtime/task_index.json", split_by_task)
        write_json(
            staged / "simulator/scenarios.json",
            {task_id: task["user_scenario"] for task_id, task in tasks.items()},
        )
        write_json(
            staged / "evaluation/references.json",
            {task_id: task["evaluation_criteria"] for task_id, task in tasks.items()},
        )
        write_json(staged / "manifest.json", manifest)
        verify_bundle(staged)
        try:
            staged.rename(bundle)
        except OSError:
            if not bundle.is_dir():
                raise
            verify_bundle(bundle)
            for path in staged.rglob("*"):
                existing = bundle / path.relative_to(staged)
                if path.is_file() and (
                    not existing.is_file() or path.read_bytes() != existing.read_bytes()
                ):
                    raise ValueError("Existing retail bundle differs from the verified import")
    return {
        "revision": manifest["revision"],
        "counts": {key: len(database[key]) for key in COLLECTIONS},
        "splits": {split: len(splits[split]) for split in ("train", "test")},
    }


@contextmanager
def session_db(
    workspace: Path, session: str, *, create: bool = False
) -> Iterator[sqlite3.Connection]:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", session):
        raise ValueError("Session name must contain only letters, numbers, hyphens or underscores")
    directory = workspace / "sessions"
    path = directory / f"{session}.sqlite3"
    if workspace.is_symlink() or directory.is_symlink() or path.is_symlink():
        raise ValueError("Retail workspace, session directory and session file must be local")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("Retail session must be a regular file without other links")
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    mode = "rwc" if create else "rw"
    uri = path.absolute().as_uri() + f"?mode={mode}"
    with closing(sqlite3.connect(uri, uri=True)) as connection, connection:
        yield connection


def initialize(workspace: Path, session: str, task_id: str) -> dict:
    bundle = workspace / "bundle"
    verify_bundle(bundle)
    split = read_json(bundle / "runtime/task_index.json").get(task_id)
    if split is None:
        raise ValueError("Unknown retail task ID")
    baseline = read_json(bundle / "runtime/baseline.json")
    with session_db(workspace, session, create=True) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS task (id TEXT PRIMARY KEY)")
        db.execute("""CREATE TABLE IF NOT EXISTS records (
            collection TEXT, id TEXT, payload TEXT, PRIMARY KEY (collection, id)
        )""")
        previous = db.execute("SELECT id FROM task").fetchone()
        if previous is not None and previous[0] != task_id:
            raise ValueError("Session already belongs to a different retail task")
        if previous is None:
            db.execute("INSERT INTO task VALUES (?)", (task_id,))
            restore(db, baseline)
        return {"task_id": task_id, "split": split, "state_sha256": state_digest(db)}


def restore(db: sqlite3.Connection, baseline: dict) -> None:
    db.execute("DELETE FROM records")
    for collection in COLLECTIONS:
        db.executemany(
            "INSERT INTO records VALUES (?, ?, ?)",
            (
                (collection, key, json.dumps(record, sort_keys=True))
                for key, record in baseline[collection].items()
            ),
        )


def state_digest(db: sqlite3.Connection) -> str:
    rows = db.execute("SELECT collection, id, payload FROM records ORDER BY collection, id")
    return digest(json.dumps(list(rows)).encode("utf-8"))


def reset(workspace: Path, session: str) -> dict:
    bundle = workspace / "bundle"
    verify_bundle(bundle)
    baseline = read_json(bundle / "runtime/baseline.json")
    with session_db(workspace, session) as db:
        db.execute("BEGIN IMMEDIATE")
        restore(db, baseline)
        return {"state_sha256": state_digest(db)}


def get_record(db: sqlite3.Connection, collection: str, record_id: str) -> dict | None:
    row = db.execute(
        "SELECT payload FROM records WHERE collection = ? AND id = ?",
        (collection, record_id),
    ).fetchone()
    return json.loads(row[0]) if row else None


class RetailAgentTools:
    """Policy and scoped record lookups for a future Agent integration."""

    def __init__(self, workspace: Path, session: str, customer_id: str):
        self._workspace = workspace
        self._session = session
        self._customer_id = customer_id

    def policy(self) -> str:
        bundle = self._workspace / "bundle"
        verify_bundle(bundle)
        return (bundle / "agent/policy.md").read_text(encoding="utf-8")

    def get_product_details(self, product_id: str) -> dict | None:
        return self._lookup("products", product_id)

    def get_user_details(self, user_id: str) -> dict | None:
        if user_id == self._customer_id:
            return self._lookup("users", user_id)
        return None

    def get_order_details(self, order_id: str) -> dict | None:
        order = self._lookup("orders", order_id)
        if order is not None and order["user_id"] == self._customer_id:
            return order
        return None

    def _lookup(self, collection: str, record_id: str) -> dict | None:
        with session_db(self._workspace, self._session) as db:
            return get_record(db, collection, record_id)


def probe_exchange(workspace: Path, session: str, order_id: str) -> dict:
    """Runner-only write probe for a delivered order, without executing an exchange."""
    with session_db(workspace, session) as db:
        db.execute("BEGIN IMMEDIATE")
        order = get_record(db, "orders", order_id)
        if order is None or order["status"] != "delivered":
            raise ValueError("Probe requires an existing delivered order")
        order["status"] = PROBE_ORDER_STATUS
        db.execute(
            "UPDATE records SET payload = ? WHERE collection = 'orders' AND id = ?",
            (json.dumps(order, sort_keys=True), order_id),
        )
        return {"state_sha256": state_digest(db)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=WORKSPACE)
    commands = parser.add_subparsers(dest="command", required=True)
    fetcher = commands.add_parser("fetch", help="Download and verify pinned upstream files")
    fetcher.add_argument("--destination", type=Path)
    importer = commands.add_parser("import", help="Verify and import local pinned source files")
    importer.add_argument("--source", type=Path, required=True)
    initializer = commands.add_parser("init", help="Initialize an isolated task session")
    initializer.add_argument("session")
    initializer.add_argument("task_id")
    resetter = commands.add_parser("reset", help="Restore the session's baseline")
    resetter.add_argument("session")
    probe = commands.add_parser(
        "probe-exchange", help="Set a delivered order's status for the reset check"
    )
    probe.add_argument("session")
    probe.add_argument("order_id")
    view = commands.add_parser("agent-view", help="Read the policy or a scoped record")
    view.add_argument("session")
    view.add_argument("customer_id", help="Trusted runner identity for this local inspection")
    selection = view.add_mutually_exclusive_group(required=True)
    selection.add_argument("--policy", action="store_true")
    selection.add_argument("--product")
    selection.add_argument("--user")
    selection.add_argument("--order")
    args = parser.parse_args()
    try:
        if args.command == "fetch":
            result = fetch_source(args.destination or args.workspace / "source")
        elif args.command == "import":
            result = import_source(args.source, args.workspace)
        elif args.command == "init":
            result = initialize(args.workspace, args.session, args.task_id)
        elif args.command == "reset":
            result = reset(args.workspace, args.session)
        elif args.command == "probe-exchange":
            result = probe_exchange(args.workspace, args.session, args.order_id)
        else:
            agent = RetailAgentTools(args.workspace, args.session, args.customer_id)
            if args.policy:
                result = agent.policy()
            elif args.product is not None:
                result = agent.get_product_details(args.product)
            elif args.user is not None:
                result = agent.get_user_details(args.user)
            else:
                result = agent.get_order_details(args.order)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
