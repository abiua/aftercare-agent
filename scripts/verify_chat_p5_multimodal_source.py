"""Prepare one owned offline P5 chat with two synthetic originals for bounded P2."""

import argparse
import json
import os
import re
import socket
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import offline_demo
from aftercare.business.rules import digest
from aftercare.intelligence import vision
from verify_chat_backup_restore import private_json
from verify_chat_p1_http import call, message, read, request_ids
from verify_chat_p2_offline import P2Stack, _begin, owned_container
from verify_fresh_bootstrap import ROOT, VerificationError, private_write, require
from verify_material_review_migration import _docker
from verify_streaming_compatibility import _save

PORT, OBJECT_PORT = 5202, 58364
URL = f"http://127.0.0.1:{PORT}"
STATE = ROOT / ".runtime/offline-browser-demo-p5-multimodal.json"
BACKEND = "sha256:1c490df0b02dfaad1e88b0d7226ecb7ed5d88fbb4119e2f026701bcaec4a5775"
FRONTEND = "sha256:88f40f1cb0bc812889f1efec9d24bb6bdeb6291b2b65937f50acadba8aac1929"
ORDER = "O9902"
PRODUCT = "DEV-VISION-MUG-01"
ASSETS = ("VIS-01", "VIS-03")
GATE = ROOT / "scripts/chat_vision_wire_gate/sitecustomize.py"
PINS = {
    "data/v0.1/agent/business.json": "869df87d59dd5c6c5d4a130ec0fcbd900e5c309efd30c27979353d705bc2562c",
    "data/chat-eval-v1/development/multimodal-order.json": "8cbea06eab2e0b24a6faa6d1fe76325165a2dbed6c7745aa10f0f8b0be9fe9a9",
    "data/vision-v1/manifest.json": "9e3069ccfeb95b70bc5c2814893edb3d7686f51ecd660bfb4c54ac452e411dfe",
}


def _inputs():
    seed, addition, manifest = [
        json.loads(_verified(ROOT / name, checksum)) for name, checksum in PINS.items()
    ]
    order, product = addition["order"], addition["product"]
    require(
        order["id"] == ORDER
        and order["customer_id"] == "C001"
        and order["items"][0]["product_id"] == product["id"] == PRODUCT
        and not any(row["id"] == ORDER for row in seed["orders"])
        and not any(row["id"] == PRODUCT for row in seed["products"]),
        "P5_MULTIMODAL_SEED_CONFLICT",
    )
    seed["orders"].append(order)
    seed["products"].append(product)
    assets = {row["id"]: row for row in manifest["assets"]}
    rows = [assets[ident] for ident in ASSETS]
    originals = {}
    for row in rows:
        raw = _verified(ROOT / "data/vision-v1" / row["file"], row["sha256"])
        require(
            len(raw) == row["bytes"] and row["source_kind"] == "AI_GENERATED_SYNTHETIC",
            "P5_MULTIMODAL_ASSET_CHANGED",
        )
        originals[row["id"]] = raw
    photos = vision.prepare_photos(
        [
            vision.PhotoSource(row["id"], "p5-multimodal", row["sha256"], "image/png", row["bytes"])
            for row in rows
        ],
        originals.__getitem__,
    )
    return seed, rows, originals, photos


def _verified(path, checksum):
    raw = path.read_bytes()
    require(sha256(raw).hexdigest() == checksum, "P5_MULTIMODAL_PIN_CHANGED")
    return raw


def _plan(seed, assets, photos):
    require(not STATE.exists(), "P5_MULTIMODAL_STATE_EXISTS")
    endpoint = os.environ.get("DOCKER_HOST")
    require(
        (not endpoint or endpoint.startswith("unix://"))
        and _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").startswith(
            "unix://"
        ),
        "P5_MULTIMODAL_LOCAL_DOCKER_REQUIRED",
    )
    for port in (PORT, OBJECT_PORT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
    for image in (BACKEND, FRONTEND):
        require(
            _docker("image", "inspect", "--format", "{{.Id}}", image) == image,
            "P5_MULTIMODAL_IMAGE_MISMATCH",
        )
    require(
        sum(len(photo.content) for photo in photos) * 4 // 3 + 20_000 < 300_000,
        "P5_MULTIMODAL_P2_BODY_LIMIT",
    )
    return {
        "protocol": "p5-v7-live-source-offline-preparation-v1",
        "revision": "e3c8baa",
        "profile": "test",
        "url": URL,
        "object_port": OBJECT_PORT,
        "images": {"backend": BACKEND, "frontend": FRONTEND},
        "pins": PINS,
        "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "vision_gate_sha256": sha256(GATE.read_bytes()).hexdigest(),
        "seed_sha256": digest(seed),
        "order_id": ORDER,
        "product_id": PRODUCT,
        "original_image_sha256s": [row["sha256"] for row in assets],
        "sent_sha256s": [photo.sent_sha256 for photo in photos],
        "transform_version": vision.TRANSFORM_VERSION,
        "maximum_provider_calls": 0,
    }


def _prepare(seed, assets, originals, plan, report, output):
    class Stack(P2Stack):
        def __init__(self, directory):
            super().__init__(directory)
            self.seed_path = self.directory / "multimodal-business.json"
            require(digest(seed) == plan["seed_sha256"], "P5_MULTIMODAL_SEED_CHANGED")
            private_write(self.seed_path, json.dumps(seed, ensure_ascii=False))
            config_path = self.runtime / "local.json"
            config = private_json(config_path)
            config["app"].update(
                chat_analysis_enabled=True,
                chat_vision_enabled=False,
                chat_decisions_enabled=True,
                chat_investigation_enabled=False,
                chat_investigation_protocol="compact-v7",
            )
            config_path.write_text(json.dumps(config))

        def run(self, image, suffix, network, options=(), command=()):
            if suffix == "init":
                require(image == BACKEND, "P5_MULTIMODAL_BOOTSTRAP_IMAGE_MISMATCH")
                options = (
                    *options,
                    "--mount",
                    (
                        f"type=bind,source={self.seed_path},"
                        "target=/workspace/data/v0.1/agent/business.json,readonly"
                    ),
                )
            return super().run(image, suffix, network, options, command)

    import verify_chat_p5_pg as pg

    with patch.multiple(offline_demo, STATE=STATE, FreshStack=Stack):
        offline_demo.start(
            SimpleNamespace(
                port=PORT,
                object_port=OBJECT_PORT,
                profile="test",
                backend_image=BACKEND,
                frontend_image=FRONTEND,
            )
        )
        state = private_json(STATE)
        stack = offline_demo.restore(state)
        with patch.multiple(pg, PORT=PORT, OBJECT_PORT=OBJECT_PORT):
            _, identity = pg._topology(stack, {pg.BACKEND: BACKEND, pg.FRONTEND: FRONTEND})
        report.update(owned_stack=stack.run_id, owned_url=URL, system_identifier=identity)
        _save(output, report)
        rows = private_json(stack.credentials_path)
        (owner,) = [
            row for row in rows if row["role"] == "customer" and row["customer_id"] == "C001"
        ]
        (support_actor,) = [row for row in rows if row["role"] == "support"]
        beat = owned_container(stack, "beat")
        stack.docker("P5_MULTIMODAL_PAUSE_OWN_BEAT", "pause", beat, log_output=False)
        report["beat_paused_for_pending_p2"] = True
        _save(output, report)
        with httpx.Client(
            base_url=URL + "/api/",
            headers={"Authorization": f"Bearer {owner['token']}"},
            follow_redirects=False,
            trust_env=False,
            timeout=12,
        ) as client:
            require(read(client, "me")["customer_id"] == "C001", "P5_MULTIMODAL_WRONG_ACTOR")
            require(read(client, "runtime")["mode"] == "offline", "P5_MULTIMODAL_API_NOT_OFFLINE")
            (order,) = [row for row in read(client, "orders")["items"] if row["id"] == ORDER]
            require(
                order["status"] == "DELIVERED"
                and order["payment_status"] == "CONFIRMED"
                and len(order["items"]) == 1
                and order["items"][0]["product_id"] == PRODUCT
                and order["items"][0]["quantity"] == 1,
                "P5_MULTIMODAL_ORDER_SCOPE_CHANGED",
            )
            conversation, case, _ = _begin(
                client,
                report,
                "conversation_id",
                order,
                f"订单 {ORDER} 的白色马克杯杯口有缺口，包装有破损，想申请售后。",
            )
            report["case_id"] = case
            _save(output, report)
            ids = request_ids(report, "two_originals")
            _save(output, report)
            uploaded = message(
                client,
                conversation,
                "",
                ids,
                [(row["file"], originals[row["id"]]) for row in assets],
            )
            customer = next(row for row in uploaded["messages"] if row["role"] == "customer")
            attachments = customer["attachments"]
            require(len(attachments) == 2, "P5_MULTIMODAL_TWO_ORIGINALS_REQUIRED")
            for asset, attachment in zip(assets, attachments, strict=True):
                fetched = call(client, "GET", attachment["content_path"].removeprefix("/api/"), 200)
                require(
                    attachment["sha256"] == asset["sha256"]
                    and fetched.content == originals[asset["id"]],
                    "P5_MULTIMODAL_ORIGINAL_READBACK_MISMATCH",
                )
            with httpx.Client(
                base_url=URL + "/api/",
                headers={"Authorization": f"Bearer {support_actor['token']}"},
                follow_redirects=False,
                trust_env=False,
                timeout=12,
            ) as support:
                require(read(support, "me")["role"] == "support", "P5_MULTIMODAL_SUPPORT_REQUIRED")
                context = read(support, f"conversations/{conversation}/decision-materials")
            require(
                context["case_id"] == case
                and {row["attachment_id"] for row in context["items"]}
                == {row["id"] for row in attachments},
                "P5_MULTIMODAL_MATERIAL_SCOPE_CHANGED",
            )
            report.update(
                issue_id=context["issue_id"],
                attachment_ids=[row["id"] for row in attachments],
                original_image_sha256s=plan["original_image_sha256s"],
            )

        native = stack.json_exec(
            "P5_MULTIMODAL_OFFLINE_READBACK",
            owned_container(stack, "api"),
            f"""
import json
from sqlalchemy import text
from aftercare.config import load_settings
from aftercare.db import guarded_engine
settings=load_settings('test')
assert settings.container and settings.chat_analysis_enabled and settings.chat_decisions_enabled
assert settings.chat_investigation_protocol == 'compact-v7'
assert not any((settings.chat_vision_enabled,settings.chat_investigation_enabled,settings.model_enabled,settings.embedding_enabled))
engine=guarded_engine(settings,test_only=True)
with engine.connect() as db, db.begin():
    db.execute(text('SET TRANSACTION READ ONLY'))
    assert db.scalar(text('SELECT system_identifier::text FROM pg_control_system()')) == {identity!r}
    tables=('chat_model_attempts','chat_observations','agent_runs','agent_model_attempts','intake_model_audits',
            'chat_decision_snapshots','business_approvals','business_executions','business_simulated_payments')
    counts={{name:db.scalar(text('SELECT count(*) FROM public.'+name)) for name in tables}}
    turn=db.execute(text(
        'SELECT id,state,attachments FROM public.chat_analysis_turns '
        'WHERE conversation_id=:conversation ORDER BY generation DESC LIMIT 1'
    ),{{'conversation':{conversation!r}}}).one()
print(json.dumps(dict(counts=counts,turn_id=turn.id,turn_state=turn.state,
    attachment_ids=[item['id'] for item in turn.attachments],
    original_image_sha256s=[item['sha256'] for item in turn.attachments])))
engine.dispose()
""",
        )
        require(
            native["turn_state"] == "PENDING"
            and native["attachment_ids"] == report["attachment_ids"]
            and native["original_image_sha256s"] == report["original_image_sha256s"]
            and not any(native["counts"].values()),
            "P5_MULTIMODAL_PENDING_P2_SCOPE_CHANGED",
        )
        report["offline_readback"] = native


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-offline", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.prepare_offline and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--prepare-offline requires an exact plan digest and a new report")
    seed, assets, originals, photos = _inputs()
    with offline_demo.lifecycle_lock(STATE, shared=not args.prepare_offline):
        plan = _plan(seed, assets, photos)
        checksum = digest(plan)
        if not args.prepare_offline:
            print(json.dumps({"status": "PREFLIGHT_ONLY", "plan": plan, "plan_sha256": checksum}))
            return 0
        require(checksum == args.expected_plan_sha256, "P5_MULTIMODAL_PLAN_CHANGED")
        report = {
            "status": "PREPARING_OFFLINE",
            "plan": plan,
            "plan_sha256": checksum,
            "request_ids": {},
        }
        fd = os.open(args.report, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            _prepare(seed, assets, originals, plan, report, args.report)
            report["status"] = "OFFLINE_ORIGINALS_READY"
        except VerificationError as error:
            code = str(error)
            report.update(
                status="FAILED",
                failure_code=code if code.isupper() and " " not in code else "VERIFICATION_ERROR",
            )
        except Exception as error:  # noqa: BLE001 - private environment must stay out of reports
            code = str(error)
            report.update(status="FAILED", failure_type=type(error).__name__)
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,120}", code):
                report["failure_code"] = code
        finally:
            _save(args.report, report)
        print(report["status"])
        return int(report["status"] != "OFFLINE_ORIGINALS_READY")


if __name__ == "__main__":
    raise SystemExit(main())
