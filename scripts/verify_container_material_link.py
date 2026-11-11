"""Verify container app material proxy and browser-reachable signed object URLs.

Run after deploying the signing fix:
    python3 scripts/verify_container_material_link.py --run-app --wait-expiry \
        --output reports/deployment/container-material-link.json
The private progress file permits retries with a new report path.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
APP = "http://127.0.0.1:5186"
SIGNING_HOST = "127.0.0.1:58350"
IMAGE = ROOT / "reports/browser-material.png"
STATE = RUNTIME / "container-material-link-state.json"
LOCK = RUNTIME / "container-material-link.lock"
MAX_BODY = 8 * 1024 * 1024
DESCRIPTION = "容器材料链路验收：此工单仅用于合成图片取件测试，图片不是商品破损证据，请勿审批、执行或启动助手。"


class VerificationError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def checksum(body):
    return hashlib.sha256(body).hexdigest()


def request(opener, url, *, method="GET", headers=None, body=None):
    message = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with opener.open(message, timeout=15) as response:
            data = response.read(MAX_BODY + 1)
            require(len(data) <= MAX_BODY, "HTTP_BODY_LIMIT_EXCEEDED")
            return response.status, data, response.headers
    except HTTPError as error:
        with error:
            return error.code, b"", error.headers


def api(opener, token, path, *, expected=200, payload=None, key=None, multipart=None):
    headers = {"Authorization": f"Bearer {token}"}
    method, body = "GET", None
    if payload is not None or multipart is not None:
        method = "POST"
        headers["Idempotency-Key"] = key
        if multipart is None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode()
        else:
            headers["Content-Type"], body = multipart
    status, data, _ = request(opener, APP + "/api" + path, method=method, headers=headers, body=body)
    require(status == expected, f"APP_{method}_EXPECTED_{expected}_GOT_{status}")
    return json.loads(data)


def items(opener, token, path):
    for offset in range(0, 6000, 300):
        page = api(opener, token, f"{path}?limit=300&offset={offset}")["items"]
        yield from page
        if len(page) < 300:
            return
    raise VerificationError("APP_LIST_LIMIT_REACHED")


def save_state(state):
    descriptor, temporary = tempfile.mkstemp(prefix=".container-material-link-", dir=RUNTIME)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE)
    finally:
        Path(temporary).unlink(missing_ok=True)


def multipart_upload(image, version):
    boundary = f"aftercare-{uuid4().hex}"
    segments = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="kind"\r\n\r\nITEM\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="expected_version"\r\n\r\n{version}\r\n'.encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
         'filename="browser-material.png"\r\nContent-Type: image/png\r\n\r\n').encode(),
        image,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return f"multipart/form-data; boundary={boundary}", b"".join(segments)


def init_state(opener, token, image_hash):
    used = {case["order_id"] for case in items(opener, token, "/cases")}
    order = next(
        (order for order in items(opener, token, "/orders")
         if order["id"] not in used and order["status"] == "DELIVERED"
         and order.get("payment_status") == "CONFIRMED" and order.get("items")),
        None,
    )
    require(order is not None, "NO_UNUSED_DELIVERED_ORDER")
    run_id = uuid4().hex
    state = {
        "fixture_id": run_id, "image_sha256": image_hash,
        "create_payload": {
            "order_id": order["id"], "expected_version": order["version"], "intent": "damaged",
            "description": f"{DESCRIPTION} 验收标识 {run_id}。",
        },
    }
    save_state(state)
    return state


def ensure_fixture(opener, token, image, report):
    image_hash = checksum(image)
    state = json.loads(STATE.read_text()) if STATE.exists() else init_state(opener, token, image_hash)
    fixture_id = UUID(state["fixture_id"]).hex
    payload = state["create_payload"]
    require(state["image_sha256"] == image_hash, "SYNTHETIC_IMAGE_CHANGED_SINCE_FIRST_RUN")
    require(
        payload["description"] == f"{DESCRIPTION} 验收标识 {fixture_id}。"
        and payload["intent"] == "damaged", "UNRECOGNIZED_FIXTURE_STATE",
    )
    report.update(order_id=payload["order_id"], fixture_id=fixture_id)
    if "case_id" not in state:
        # An interrupted POST can have committed. Recover the exact marked case
        # or send the original payload and key before ever selecting another order.
        current = [row for row in items(opener, token, "/cases") if row["order_id"] == payload["order_id"]]
        require(not current or (
            len(current) == 1 and current[0]["description"] == payload["description"]
            and current[0]["intent"] == "damaged"
        ), "PLANNED_ORDER_NO_LONGER_UNUSED")
        case = current[0] if current else api(
            opener, token, "/cases", expected=201, payload=payload, key=f"fresh-material-case-{fixture_id}",
        )
        require(case["description"] == payload["description"], "CASE_NOT_OWN_SYNTHETIC_FIXTURE")
        state["case_id"] = str(UUID(case["id"]))
        save_state(state)
    case_id = str(UUID(state["case_id"]))
    report["case_id"] = case_id
    path = f"/cases/{case_id}"
    detail = api(opener, token, path)
    case = detail["case"]
    require(
        case["description"] == payload["description"] and case["order_id"] == payload["order_id"]
        and case["intent"] == "damaged", "CASE_NOT_OWN_SYNTHETIC_FIXTURE",
    )
    require(
        not any(detail[key] for key in ("reviews", "proposals", "approvals", "ledger"))
        and api(opener, token, f"{path}/runs") == [], "FIXTURE_HAS_BUSINESS_OR_AGENT_ACTIVITY",
    )
    materials = detail["materials"]
    require(len(materials) <= 1, "FIXTURE_HAS_EXTRA_MATERIALS")
    if materials:
        material = materials[0]
    else:
        require("material_id" not in state, "PREVIOUS_FIXTURE_MATERIAL_MISSING")
        if "upload_version" not in state:
            state["upload_version"] = case["version"]
            save_state(state)
        material = api(
            opener, token, f"{path}/materials", expected=201,
            multipart=multipart_upload(image, state["upload_version"]),
            key=f"fresh-material-item-{fixture_id}",
        )
    require(
        material["kind"] == "ITEM" and material["sha256"] == image_hash
        and material["content_type"] == "image/png" and material["size"] == len(image),
        "FIXTURE_MATERIAL_METADATA_MISMATCH",
    )
    material_id = str(UUID(material["id"]))
    require(state.get("material_id", material_id) == material_id, "FIXTURE_MATERIAL_ID_MISMATCH")
    if "material_id" not in state:
        state["material_id"] = material_id
        save_state(state)
    report.update(material_id=material_id, bytes=len(image), sha256=image_hash)
    return f"{path}/materials/{material_id}"


def verify(opener, token, material_path, image, wait_expiry, report):
    status, content, headers = request(
        opener, APP + "/api" + material_path + "/content",
        headers={"Authorization": f"Bearer {token}"},
    )
    report["same_origin_http"] = status
    require(status == 200 and content == image and headers.get("Content-Type") == "image/png",
            "SAME_ORIGIN_CONTENT_MISMATCH")
    require("no-store" in headers.get("Cache-Control", "").lower(), "SAME_ORIGIN_CONTENT_CACHEABLE")
    report["same_origin_bytes_match"] = True

    link = api(opener, token, material_path + "/link")
    signed_at = monotonic()
    url = link["url"]
    address = urlsplit(url)
    require(
        address.scheme == "http" and address.netloc == SIGNING_HOST
        and address.path.startswith("/") and not address.fragment
        and not re.search(r"[\x00-\x20\x7f]", url), "SIGNING_URL_NOT_BROWSER_REACHABLE_LOOPBACK",
    )
    query = parse_qs(address.query)
    require(
        link["expires_in"] == 60 and query.get("X-Amz-Expires") == ["60"]
        and query.get("X-Amz-Algorithm") == ["AWS4-HMAC-SHA256"]
        and query.get("X-Amz-SignedHeaders") == ["host"] and "X-Amz-Signature" in query,
        "UNEXPECTED_SIGNING_CONTRACT",
    )
    report["signing_host"] = SIGNING_HOST
    report["configured_expiry_seconds"] = 60
    # Pass the original signed URL directly. No application token, proxy, custom
    # Host header or redirects are involved in the signed or unsigned requests.
    signed_status, signed_content, signed_headers = request(opener, url)
    report["signed_http"] = signed_status
    require(
        signed_status == 200 and signed_content == image
        and signed_headers.get("Content-Type") == "image/png", "SIGNED_CONTENT_MISMATCH",
    )
    report["signed_bytes_match"] = True
    unsigned_url = urlunsplit(address._replace(query=""))
    anonymous, _, _ = request(opener, unsigned_url)
    report["anonymous_http"] = anonymous
    require(anonymous == 403, "ANONYMOUS_S3_OBJECT_NOT_FORBIDDEN")
    if wait_expiry:
        print("Signed object verified. Waiting for the original URL to expire.", flush=True)
        while monotonic() - signed_at < 65:
            sleep(min(5, max(0, 65 - (monotonic() - signed_at))))
        expired, _, _ = request(opener, url)
        report["expiry"] = {"status": "PASSED" if expired == 403 else "FAILED", "http": expired,
                            "elapsed_seconds": round(monotonic() - signed_at, 3)}
        require(expired == 403, "SIGNED_URL_DID_NOT_EXPIRE")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-app", action="store_true", help="Explicitly permit synthetic app case/upload")
    parser.add_argument("--wait-expiry", action="store_true", help="Wait 65 seconds and verify natural expiry")
    parser.add_argument("--output", type=Path, required=True, help="New evidence JSON file")
    args = parser.parse_args()
    if not args.run_app:
        parser.error("--run-app is required")
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        parser.exit(1, "Could not create a new evidence file.\n")
    report = {
        "status": "FAILED", "source": "real_container_app_http_s3_with_synthetic_image",
        "scope": "synthetic_case_material_upload_and_http_reads_only", "app_origin": APP,
        "image_fixture": str(IMAGE.relative_to(ROOT)), "agent_runs": 0,
        "reviews_approvals_executions_requested": 0, "expiry": {"status": "NOT_RUN"},
    }
    with os.fdopen(descriptor, "w") as output:
        try:
            require(RUNTIME.is_dir() and not RUNTIME.is_symlink(), "PRIVATE_RUNTIME_DIRECTORY_REQUIRED")
            image = IMAGE.read_bytes()
            require(image.startswith(b"\x89PNG\r\n\x1a\n") and len(image) <= MAX_BODY, "INVALID_IMAGE_FIXTURE")
            users = json.loads((RUNTIME / "users-app.json").read_text())
            token = next(user["token"] for user in users if user["role"] == "support")
            opener = build_opener(ProxyHandler({}), NoRedirect())
            with os.fdopen(os.open(LOCK, os.O_WRONLY | os.O_CREAT, 0o600), "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                identity = api(opener, token, "/me")
                require(identity.get("role") == "support", "APP_SUPPORT_IDENTITY_REQUIRED")
                material_path = ensure_fixture(opener, token, image, report)
                verify(opener, token, material_path, image, args.wait_expiry, report)
            report["status"] = "PASSED"
        except VerificationError as error:
            report["failure"] = str(error)
        except (Exception, KeyboardInterrupt) as error:
            # Never include exception details, response bodies or URLs in evidence.
            report["failure"] = type(error).__name__
        finally:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
    print(json.dumps(report, ensure_ascii=False))
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
