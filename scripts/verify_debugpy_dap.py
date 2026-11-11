"""Verify the isolated business breakpoint using the installed debugpy DAP adapter.

Run with the project's prepared Python interpreter and the extension's bundled
libs directory. The JSON printed on success contains no credentials.
"""

import argparse
import concurrent.futures
import json
import os
import pathlib
import queue
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
from tempfile import TemporaryDirectory
import time
import urllib.error
import urllib.request

def verify(root, scratch, adapter_lib):
    config = json.loads((root / ".vscode/launch.json").read_text())["configurations"][0]
    if config["program"] != "${workspaceFolder}/scripts/debug_api.py" or config["args"] != ["--editor"]:
        raise RuntimeError("Refusing to run an unexpected debugger launch program or mode")
    def expand(value):
        return value.replace("${workspaceFolder}", str(root))

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "TMPDIR": str(scratch),
           "PYTHONPATH": adapter_lib, "PYTHONPYCACHEPREFIX": str(pathlib.Path.home() / ".cache/aftercare-agent/pycache")}
    adapter = subprocess.Popen(
        [sys.executable, "-m", "debugpy.adapter"], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    incoming = queue.Queue()
    backlog = []
    seq = 0
    child_pid = None

    def reader():
        while True:
            headers = {}
            while True:
                line = adapter.stdout.readline()
                if not line:
                    incoming.put(None)
                    return
                if not line.strip():
                    break
                key, value = line.decode().split(":", 1)
                headers[key.lower()] = value.strip()
            incoming.put(json.loads(adapter.stdout.read(int(headers["content-length"]))))

    threading.Thread(target=reader, daemon=True).start()
    def send(command, arguments=None):
        nonlocal seq
        seq += 1
        packet = json.dumps({"seq": seq, "type": "request", "command": command, "arguments": arguments or {}}).encode()
        adapter.stdin.write(f"Content-Length: {len(packet)}\r\n\r\n".encode() + packet)
        adapter.stdin.flush()
        return seq

    def wait(predicate, timeout=35):
        end = time.monotonic() + timeout
        while True:
            for i, value in enumerate(backlog):
                if predicate(value):
                    return backlog.pop(i)
            item = incoming.get(timeout=max(.01, end - time.monotonic()))
            if item is None:
                raise RuntimeError("debug adapter closed unexpectedly")
            if predicate(item):
                return item
            backlog.append(item)

    def response(request):
        result = wait(lambda msg: msg.get("request_seq") == request)
        if not result.get("success"):
            raise RuntimeError(f"DAP request failed: {result.get('command')}")
        return result.get("body", {})

    def event(kind):
        return wait(lambda msg: msg.get("event") == kind).get("body", {})

    def http(path, *, token=None, body=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api{path}",
            data=json.dumps(body).encode() if body else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Idempotency-Key": secrets.token_hex(16)} if token else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as result:
                return result.status, json.load(result)
        except urllib.error.HTTPError as error:
            return error.code, None

    try:
        response(send("initialize", {"adapterID": "python", "linesStartAt1": True,
                                      "columnsStartAt1": True, "pathFormat": "path",
                                      "supportsRunInTerminalRequest": False}))
        launch = send("launch", {
            "program": expand(config["program"]), "cwd": expand(config["cwd"]),
            "python": [sys.executable], "args": config["args"] + ["--port", str(port)],
            "console": "internalConsole", "redirectOutput": True, "env": {k: expand(v) for k,v in config["env"].items()},
        })
        event("initialized")
        source = root / "backend/src/aftercare/business/service.py"
        source_lines = source.read_text().splitlines()
        def_line = next(i for i, value in enumerate(source_lines) if value == "    def create_case(")
        breakpoint_line = next(i+1 for i in range(def_line,len(source_lines)) if '_require_role(user, "customer", "support")' in source_lines[i])
        response(send("setBreakpoints", {"source": {"path": str(source)}, "breakpoints": [{"line": breakpoint_line}]}))
        response(send("configurationDone"))
        response(launch)
        child_pid = event("process")["systemProcessId"]
        # Do not send HTTP to an unrelated listener if the ephemeral port was taken before uvicorn bound it.
        wait(lambda msg: msg.get("event") == "output" and
             f"Uvicorn running on http://127.0.0.1:{port}" in msg.get("body", {}).get("output", ""))
        deadline = time.monotonic()+20
        credential_file = None
        while time.monotonic() < deadline:
            credential_file = next(scratch.glob("aftercare-debug-*/users-debug.json"), None)
            if credential_file and credential_file.stat().st_size:
                try:
                    if http("/me")[0] == 401:
                        break
                except OSError:
                    pass
            time.sleep(.1)
        else:
            raise RuntimeError("isolated API not ready")
        assert stat.S_IMODE(credential_file.stat().st_mode) == 0o600
        own, other = json.loads(credential_file.read_text())
        assert http("/runtime", token=own["token"])[1]["mode"] == "offline"
        status, orders = http("/orders", token=own["token"])
        assert status == 200 and orders["items"]
        order = orders["items"][0]
        assert http("/evaluation/datasets", token=own["token"])[0] == 404
        with concurrent.futures.ThreadPoolExecutor() as executor:
            pending = executor.submit(http, "/cases", token=own["token"], body={
                "order_id": order["id"], "expected_version": order["version"],
                "intent": "damaged", "description": "隔离编辑器断点验收合成工单"})
            stopped = event("stopped")
            assert stopped["reason"] == "breakpoint"
            assert not pending.done()
            stack = response(send("stackTrace", {"threadId": stopped["threadId"]}))["stackFrames"]
            assert pathlib.Path(stack[0]["source"]["path"]).resolve() == source.resolve()
            assert stack[0]["line"] == breakpoint_line and stack[0]["name"] == "create_case"
            assert any(frame.get("name") == "create_case" and frame.get("source",{}).get("path","").endswith("/business/api.py") for frame in stack[1:])
            response(send("continue", {"threadId": stopped["threadId"]}))
            created_status, case = pending.result(15)
        assert created_status == 201
        assert http(f'/cases/{case["id"]}', token=own["token"])[0] == 200
        assert http(f'/cases/{case["id"]}', token=other["token"])[0] == 404
        os.kill(child_pid, signal.SIGINT)
        event("terminated")
        child_pid = None
        assert not credential_file.parent.exists()
        print(json.dumps({"status": "PASS_DEBUGPY_DAP", "scope": "installed VS Code debugpy adapter over DAP and real loopback HTTP, no GUI",
            "api_port": port, "bound_host": "127.0.0.1", "breakpoint": f"backend/src/aftercare/business/service.py:{breakpoint_line}",
            "production_route_on_stack": True, "request_waits_on_breakpoint": True, "created": 201,
            "owner_read": 200, "other_customer_read": 404, "private_token_mode": "0600", "temporary_runtime_cleaned": True,
            "model_calls": 0, "shared_services_accessed": False}))
    finally:
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if adapter.poll() is None:
            adapter.terminate()
            try:
                adapter.wait(timeout=10)
            except subprocess.TimeoutExpired:
                adapter.kill()
                adapter.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debugpy-lib", required=True, type=pathlib.Path,
                        help="Python Debugger extension's bundled/libs directory")
    args = parser.parse_args()
    adapter_lib = args.debugpy_lib.resolve()
    if not (adapter_lib / "debugpy" / "adapter" / "__main__.py").is_file():
        parser.error("The supplied directory does not contain debugpy.adapter")
    root = pathlib.Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="aftercare-verify-dap-") as scratch:
        verify(root, pathlib.Path(scratch), str(adapter_lib))


if __name__ == "__main__":
    main()
