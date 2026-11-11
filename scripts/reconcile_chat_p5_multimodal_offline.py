"""Reconcile a committed two-original upload without resubmitting its HTTP request."""

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import offline_demo
import verify_chat_p5_pg as pg
from aftercare.business.rules import digest
from verify_chat_backup_restore import private_json
from verify_chat_p1_http import call, read
from verify_chat_p2_offline import owned_container
from verify_chat_p5_multimodal_source import (
    BACKEND,
    FRONTEND,
    GATE,
    OBJECT_PORT,
    PORT,
    STATE,
    URL,
    _inputs,
)
from verify_fresh_bootstrap import require
from verify_material_review_migration import _hash


def _client(role, credentials):
    (actor,) = [row for row in credentials if row["role"] == role]
    return httpx.Client(
        base_url=URL + "/api/",
        follow_redirects=False,
        trust_env=False,
        timeout=12,
        headers={"Authorization": "Bearer " + actor["token"]},
    )


def _readback(stack, expected):
    code = (
        f"P={expected!r}\n"
        + """
import json
from sqlalchemy import text
from aftercare.config import load_settings
from aftercare.db import guarded_engine
settings=load_settings('test')
assert settings.container and settings.chat_analysis_enabled and settings.chat_decisions_enabled
assert settings.chat_investigation_protocol=='compact-v7'
assert not any((settings.chat_vision_enabled,settings.chat_investigation_enabled,settings.model_enabled,settings.embedding_enabled))
engine=guarded_engine(settings,test_only=True)
with engine.begin() as db:
    db.execute(text('SET TRANSACTION READ ONLY'))
    assert db.scalar(text('SELECT system_identifier::text FROM pg_control_system()'))==P['system_identifier']
    c=db.execute(text('SELECT active_case_id,active_order_id,analysis_issue_id,status FROM chat_conversations WHERE id=:conversation_id'),P).one()
    assert tuple(c)==(P['case_id'],'O9902',P['issue_id'],'OPEN')
    m=db.execute(text(
        "SELECT id,sequence,actor_id FROM chat_messages WHERE conversation_id=:conversation_id "
        "AND client_message_id=:message AND role='customer' AND discarded_at IS NULL"
    ),P).one()
    assert m.sequence is not None
    a=list(db.execute(text(
        'SELECT id,position,sha256,size FROM chat_attachments WHERE message_id=:id ORDER BY position'
    ),{'id':m.id}).mappings())
    i=set(db.scalars(text('SELECT scope FROM business_idempotency WHERE actor_id=:actor AND key=:key'),
        {'actor':m.actor_id,'key':P['key']}))
    assert i=={f"chat:{kind}:{P['conversation_id']}" for kind in ('stage','message')}
    t=db.execute(text(
        'SELECT id,state,attachments, deadline <= now() AS expired FROM chat_analysis_turns '
        'WHERE conversation_id=:conversation_id ORDER BY generation DESC LIMIT 1'
    ),P).one()
    tables=('chat_model_attempts','chat_observations','agent_runs','agent_model_attempts','intake_model_audits',
            'chat_decision_snapshots','business_approvals','business_executions','business_simulated_payments')
    counts={table:db.scalar(text('SELECT count(*) FROM public.'+table)) for table in tables}
    assert t.state=='PENDING' and not any(counts.values())
    assert [(x['id'],x['sha256']) for x in a]==[(x['id'],x['sha256']) for x in t.attachments]
    result=dict(turn_id=t.id,turn_state=t.state,turn_expired=t.expired,counts=counts,
        upload_message_id=m.id,upload_sequence=m.sequence,attachments=[dict(x) for x in a])
print(json.dumps(result))
engine.dispose()
"""
    )
    return stack.json_exec("P5_MULTIMODAL_RECONCILE_READ_ONLY", owned_container(stack, "api"), code)


def _reconcile(path):
    failed = private_json(path)
    failed_sha = _hash(path)
    original = failed["plan"]
    state = private_json(STATE)
    require(
        failed["status"] == "FAILED"
        and failed["failure_type"] == "VerificationError"
        and failed["plan_sha256"] == digest(original)
        and original["protocol"] == "p5-v7-live-source-offline-preparation-v1"
        and original["images"] == {"backend": BACKEND, "frontend": FRONTEND}
        and original["maximum_provider_calls"] == 0
        and original["vision_gate_sha256"] == _hash(GATE)
        and failed["owned_stack"] == state["run_id"]
        and failed["owned_url"] == state["url"] == URL
        and failed["beat_paused_for_pending_p2"]
        and state["profile"] == "test"
        and state["status"] == "READY",
        "P5_MULTIMODAL_RECONCILE_SCOPE_MISMATCH",
    )
    stack = offline_demo.restore(state)
    with patch.multiple(pg, PORT=PORT, OBJECT_PORT=OBJECT_PORT):
        _, system_id = pg._topology(stack, {pg.BACKEND: BACKEND, pg.FRONTEND: FRONTEND})
    beat = owned_container(stack, "beat")
    require(
        system_id == failed["system_identifier"]
        and stack.docker(
            "P5_MULTIMODAL_RECONCILE_BEAT",
            "inspect",
            "--format",
            "{{.State.Paused}}",
            beat,
            log_output=False,
        ).stdout.strip()
        == "true",
        "P5_MULTIMODAL_RECONCILE_STACK_CHANGED",
    )
    conversation_id = failed["conversation_id"]
    credentials = private_json(stack.credentials_path)
    with _client("support", credentials) as support:
        require(
            read(support, "me")["role"] == "support", "P5_MULTIMODAL_RECONCILE_SUPPORT_REQUIRED"
        )
        listed = read(support, f"conversations/{conversation_id}/decision-materials")
    ids = failed["request_ids"]["two_originals"]
    native = _readback(
        stack,
        {
            "system_identifier": system_id,
            "conversation_id": conversation_id,
            "case_id": failed["case_id"],
            "issue_id": listed["issue_id"],
            "message": ids["client_message_id"],
            "key": ids["idempotency_key"],
        },
    )
    _, assets, raw, prepared = _inputs()
    attachments = native.pop("attachments")
    originals = [row["sha256"] for row in attachments]
    require(
        listed["case_id"] == failed["case_id"]
        and listed["materials_version"] == 0
        and len(listed["items"]) == len(attachments) == 2
        and {row["attachment_id"] for row in listed["items"]} == {row["id"] for row in attachments}
        and all(row["material_id"] is None for row in listed["items"])
        and originals == original["original_image_sha256s"] == [row["sha256"] for row in assets]
        and [row["size"] for row in attachments] == [row["bytes"] for row in assets]
        and [photo.sent_sha256 for photo in prepared] == original["sent_sha256s"],
        "P5_MULTIMODAL_RECONCILE_ORIGINALS_CHANGED",
    )
    with _client("customer", credentials) as customer:
        require(
            read(customer, "me")["customer_id"] == "C001", "P5_MULTIMODAL_RECONCILE_OWNER_REQUIRED"
        )
        for asset, attachment in zip(assets, attachments, strict=True):
            path_get = f"conversations/{conversation_id}/attachments/{attachment['id']}/content"
            require(
                call(customer, "GET", path_get, 200).content == raw[asset["id"]],
                "P5_MULTIMODAL_RECONCILE_BYTES_CHANGED",
            )
    require(_hash(path) == failed_sha, "P5_MULTIMODAL_RECONCILE_FAILED_REPORT_CHANGED")
    attachment_ids = [row["id"] for row in attachments]
    return {
        **{k: v for k, v in failed.items() if k not in {"failure_type", "status"}},
        "status": "OFFLINE_ORIGINALS_READY",
        "issue_id": listed["issue_id"],
        "attachment_ids": attachment_ids,
        "original_image_sha256s": originals,
        "offline_readback": {
            **native,
            "attachment_ids": attachment_ids,
            "original_image_sha256s": originals,
        },
        "reconciled_from": {
            "path": str(path.resolve()),
            "sha256": failed_sha,
            "http_upload_replayed": False,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--failed-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    with (
        offline_demo.lifecycle_lock(STATE, shared=True),
        patch.object(offline_demo, "STATE", STATE),
    ):
        report = _reconcile(args.failed_report)
        fd = os.open(args.report, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    print(report["status"])


if __name__ == "__main__":
    main()
