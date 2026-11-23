"""Start the local customer demo against its existing P5 business data and policy catalog."""

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path.home() / ".cache/aftercare-agent"
URL = "http://127.0.0.1:5225"
CONTEXT = "colima-tau-rag"


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def ready():
    try:
        with urlopen(URL + "/api/readyz", timeout=2) as res:
            return json.load(res).get("status") == "ready"
    except (URLError, OSError, ValueError):
        return False


def start_storage(env):
    docker = ["docker", "--context", CONTEXT]
    if subprocess.run(
        [*docker, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
    ).returncode:
        subprocess.run(
            ["colima", "start", "tau-rag", "--activate=false", "--ssh-config=false"], check=True
        )
    for volume in ("tau-chat-p5_business", "tau-chat-p5_objects", "tau-rag-p3_milvus"):
        subprocess.run(
            [*docker, "volume", "inspect", volume], stdout=subprocess.DEVNULL, check=True
        )
    compose = ["docker-compose", "-f"]
    subprocess.run(
        [*compose, "infra/compose.tau-p5.yml", "up", "-d", "--wait"], check=True, env=env
    )
    if not listening(19538):
        # Never open a catalog backed by a volume another Milvus instance already has open.
        users = subprocess.check_output(
            [
                *docker,
                "ps",
                "--filter",
                "volume=tau-rag-p3_milvus",
                "--format",
                '{{.Label "com.docker.compose.project"}}/{{.Names}}',
            ],
            text=True,
        )
        if any(not line.startswith("aftercare-customer-search/") for line in users.splitlines()):
            raise RuntimeError("政策检索卷正由其他服务使用，请检查该服务的就绪状态。")
        subprocess.run(
            [*compose, "infra/compose.customer-search.yml", "up", "-d", "--wait"],
            check=True,
            env=env,
        )


def main():
    os.chdir(ROOT)
    work = ROOT / ".runtime/live-customer"
    work.mkdir(mode=0o700, exist_ok=True)
    with (work / "launcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("实时售后服务已启动或正在启动。", URL)
            return
        if any(listening(port) for port in (5225, 18225)):
            raise RuntimeError("5225 或 18225 已由另一个服务占用。请先关闭该服务，再启动实时演示。")
        env = {
            **os.environ,
            "DOCKER_CONTEXT": CONTEXT,
            "PYTHONPATH": os.pathsep.join(
                map(str, [ROOT / "scripts", ROOT / "backend/src", ROOT, CACHE / "tau-p4-rag-deps"])
            ),
        }
        env["AFTERCARE_MONITORING_SCOPE"] = "customer-demo"
        env["AFTERCARE_PROMETHEUS_URL"] = "http://127.0.0.1:19095"
        env.pop("DOCKER_HOST", None)
        start_storage(env)
        runner = [str(CACHE / "tau-p4-venv/bin/python"), "scripts/tau_p9_runtime.py"]
        vite = """import {createServer} from 'vite';
const s=await createServer({server:{host:'127.0.0.1',port:5225,strictPort:true,proxy:{'/api':{target:'http://127.0.0.1:18225'}}}});
await s.listen();"""
        commands = [
            (name, [*runner, name, "--live", "--customer-demo"], ROOT)
            for name in ("api", "worker", "beat")
        ]
        commands.append(
            ("frontend", ["node", "--input-type=module", "-e", vite], ROOT / "frontend")
        )
        from start_prometheus import executable
        if executable():
            commands.append(("prometheus", [sys.executable, "scripts/start_prometheus.py", "live"], ROOT))
        else:
            print("系统运行总览缺少采集服务，请安装 Prometheus。", flush=True)
        children = []

        def stop(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        try:
            for name, command, cwd in commands:
                with (work / (name + ".log")).open("w") as log:
                    children.append(
                        (name, subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log))
                    )
            started = time.monotonic()
            announced = False
            while True:
                for name, child in list(children):
                    if name == "prometheus" and child.poll() is not None:
                        print("Prometheus 已退出，系统运行总览的采集已停止。详见 prometheus.log", flush=True)
                        children.remove((name, child))
                        continue
                    if child.poll() is not None:
                        raise RuntimeError(f"{name} 已退出。详见 {work / (name + '.log')}")
                if not announced:
                    if ready():
                        print(f"实时售后已启动：{URL} 。保持窗口开启，按 Ctrl+C 停止。", flush=True)
                        announced = True
                    elif time.monotonic() - started > 90:
                        raise RuntimeError(f"页面未能就绪。服务日志位于 {work}")
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            for _, child in reversed(children):
                if child.poll() is None:
                    child.terminate()
            for _, child in children:
                child.wait()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
