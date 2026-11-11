"""Read-only preflight for a separate, synthetic, real-model after-sales path."""

import argparse
import json
import os
import re
import socket
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from aftercare.business.rules import digest
from aftercare.intelligence import models, vision
from verify_chat_backup_restore import private_json
from verify_chat_p5_multimodal_source import GATE as VISION_GATE
from verify_chat_p5_multimodal_source import PINS, ROOT, _inputs
from verify_fresh_bootstrap import VerificationError, require
from verify_material_review_migration import _docker, _hash
from verify_streaming_compatibility import _configuration

PORT, OBJECT_PORT = 5203, 58365
STATE = ROOT / ".runtime/offline-browser-demo-live-business.json"
TEXT_GATE = ROOT / "scripts/chat_p4_wire_gate"
MAX_TEXT_CALLS = 8
MAX_TEXT_USD = Decimal("0.40")
MAX_VISION_USD = Decimal("0.05")
ISSUE_MICROUSD = 200_000
ISSUE_CALLS = 16
VISION_OUTPUT = 900


def _micro(value):
    return int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _budgets(spec, photos):
    text_output = models.P4_OUTPUT_RESERVE
    text_input = models.P4_INPUT_LIMIT
    photo_input, _ = models._estimate_input(vision._messages(photos), [], vision=True)
    vision_reserve = models._reserve_price(spec, spec.context_tokens, VISION_OUTPUT)
    text_reserve = models._reserve_price(spec, text_input, text_output)
    require(
        spec.alias == "economy"
        and spec.output_tokens == VISION_OUTPUT
        and spec.cache_write_ttl != "12h"
        and spec.cache_write_usd_per_million is not None
        and spec.context_tokens >= text_input + text_output
        and photo_input + VISION_OUTPUT < spec.context_tokens
        and vision_reserve is not None
        and vision_reserve > 0
        and text_reserve is not None
        and text_reserve > 0,
        "LIVE_BUSINESS_MODEL_SPEC_NOT_ADMITTED",
    )
    vision_micro, text_micro = _micro(vision_reserve), _micro(text_reserve)
    require(
        vision_micro <= _micro(MAX_VISION_USD)
        and text_micro * MAX_TEXT_CALLS <= _micro(MAX_TEXT_USD),
        "LIVE_BUSINESS_ESTIMATED_RESERVATION_CAP_EXCEEDED",
    )
    return {
        "provider": "openai",
        "model": spec.model,
        "source_model_spec_sha256": digest(spec.model_dump(mode="json")),
        "vision": {
            "maximum_posts": 1,
            "maximum_estimated_usd": str(MAX_VISION_USD),
            "reserved_microusd": vision_micro,
            "output_tokens": VISION_OUTPUT,
            "reserve_basis_input_tokens": spec.context_tokens,
        },
        "text": {
            "maximum_posts": MAX_TEXT_CALLS,
            "maximum_estimated_usd": str(MAX_TEXT_USD),
            "reserve_per_post_microusd": text_micro,
            "reserve_at_post_limit_microusd": text_micro * MAX_TEXT_CALLS,
            "input_tokens": text_input,
            "output_tokens": text_output,
        },
        "native_shared_issue": {
            "calls": ISSUE_CALLS,
            "microusd": ISSUE_MICROUSD,
            "upper_text_posts_if_vision_full_reserve_remains_booked": min(
                MAX_TEXT_CALLS, (ISSUE_MICROUSD - vision_micro) // text_micro
            ),
            "runtime_uses_remaining_native_ledger_headroom": True,
        },
        "automatic_transport_retries": 0,
        "unknown_delivery": "RECONCILE_ONLY_NO_REPLAY",
        "cost_basis": "CONSERVATIVE_REGISTRY_ESTIMATE_NOT_PROVIDER_BILL",
    }


def _image_pins(path):
    receipt = private_json(path)
    require(
        re.fullmatch(r"[0-9a-f]{40}", receipt["commit"])
        and receipt.get("runtime_updated") is False,
        "LIVE_BUSINESS_IMAGE_RECEIPT_INVALID",
    )
    images = {role: receipt[role] for role in ("backend", "frontend")}
    for image in images.values():
        require(
            re.fullmatch(r"sha256:[a-f0-9]{64}", image)
            and _docker("image", "inspect", "--format", "{{.Id}}", image) == image,
            "LIVE_BUSINESS_IMAGE_PIN_MISMATCH",
        )
    return {"declared_commit": receipt["commit"], "images": images, "receipt_sha256": _hash(path)}


def _preflight(image_receipt):
    require(not STATE.exists(), "LIVE_BUSINESS_STATE_ALREADY_EXISTS")
    endpoint = os.environ.get("DOCKER_HOST")
    require(
        (not endpoint or endpoint.startswith("unix://"))
        and _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").startswith(
            "unix://"
        ),
        "LIVE_BUSINESS_LOCAL_DOCKER_REQUIRED",
    )
    for port in (PORT, OBJECT_PORT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
    seed, assets, _, photos = _inputs()
    budget = _budgets(_configuration()["gpt-6.1-terra"], photos)
    return {
        "protocol": "chat-live-business-path-plan-v1",
        "target": {
            "profile": "test",
            "port": PORT,
            "object_port": OBJECT_PORT,
            "state": str(STATE),
            **_image_pins(image_receipt),
        },
        "protected_state": "5202_ORIGINAL_WAITING_FOR_INDEPENDENT_REVIEW",
        "fixture": {
            "provenance": "SYNTHETIC_CUP_AND_PARCEL",
            "order": "O9902",
            "customer": "C001",
            "product": "DEV-VISION-MUG-01",
            "seed_sha256": digest(seed),
            "seed_and_manifest_pins": PINS,
            "original_sha256s": [row["sha256"] for row in assets],
            "sent_jpeg_sha256s": [photo.sent_sha256 for photo in photos],
            "vision_transform_version": vision.TRANSFORM_VERSION,
        },
        "budget": budget,
        "gates": {
            "vision_gate_sha256": _hash(VISION_GATE),
            "text_transport_gate_sha256": _hash(TEXT_GATE / "sitecustomize.py"),
            "existing_paired_text_policy_sha256": _hash(TEXT_GATE / "p5_policy.py"),
            "script_sha256": _hash(Path(__file__)),
        },
        "phases": [
            {"stage": "PUBLIC_CHAT", "path": "CLIENT_TEXT_THEN_TWO_ORIGINALS_IN_ONE_MESSAGE"},
            {
                "stage": "P2",
                "path": "NATIVE_CELERY_EXACT_TWO_IMAGE_GATE",
                "success": "ONE_ATTEMPT_TWO_PUBLISHED_ORIGINAL_BOUND_OBSERVATIONS",
            },
            {
                "stage": "SUPPORT_MATERIAL",
                "path": "SUPPORT_NORMAL_HTTP_CONTROL",
                "success": "CONFIRMED_ITEM_AND_PARCEL_THEN_REVIEW_WITHOUT_STRUCTURED_HUMAN_LABELS",
            },
            {
                "stage": "P3",
                "path": "NATIVE_CELERY_V7_AUTO_SINGLE_ISSUE",
                "success": "REAL_TEXT_ATTEMPT_AND_NATIVE_ORIGINAL_CANDIDATE",
            },
            {"stage": "CONTROL_HITL", "path": "SUPPORT_ACCEPT_OR_MODIFY_WITH_PROGRAM_PASS"},
            {"stage": "CONTROL_CUSTOMER", "path": "CUSTOMER_CONFIRMS_EXACT_OFFER_VERSION"},
            {"stage": "CONTROL_APPROVAL", "path": "SEPARATE_APPROVER_ACCOUNT"},
            {
                "stage": "SIMULATED_EXECUTION",
                "path": "NO_APPROVAL_BLOCKED_THEN_IDEMPOTENT_SUPPORT_EXECUTE_WITH_CUSTOMER_RECEIPT",
            },
        ],
        "execution_gaps": [
            "A test-only, scope-bound Celery worker entry must inject one gated ModelRunner without enabling shared test live-hybrid.",
            "The exact two-image gate can be reused; staged worker cutover and zero-duplicate native queue admission remain to implement.",
            "The existing compact-v7 text transport expects paired runner active-row metadata. A separately versioned ordinary-run admission policy must bind the native case/issue/input/route.",
            "At most one authorized text investigation: keep it disabled during the vision stage, then release the reviewed case through the real investigation queue.",
            "No candidate, blocked gate, partial or unknown delivery is a retained outcome and must not trigger a second paid run.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-receipt", required=True, type=Path)
    args = parser.parse_args()
    plan = _preflight(args.image_receipt)
    print(
        json.dumps(
            {
                "status": "PREFLIGHT_ONLY_EXECUTION_NOT_IMPLEMENTED",
                "plan_sha256": digest(plan),
                "plan": plan,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (VerificationError, ValueError, OSError) as error:
        code = str(error)
        print(code if re.fullmatch(r"[A-Z0-9_]+", code) else "LIVE_BUSINESS_PREFLIGHT_BLOCKED")
        raise SystemExit(1) from None
