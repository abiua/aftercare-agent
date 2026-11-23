"""Manage existing local aftercare containers without rebuilding or deleting data."""

import argparse
import fcntl
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "colima"
DEMO = {
    "refund": ("offline-browser-demo-live-success-ordinary-r10.json", 5215),
    "complex": ("offline-browser-demo-live-success-complex-r2.json", 5213),
}
ROLES = ("pg", "redis", "objects", "api", "worker", "frontend")


class ControlError(Exception):
    pass


def run(args, *, timeout=120, check=True):
    try:
        result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ControlError(f"{args[0]} 未能完成：{type(error).__name__}") from None
    if check and result.returncode:
        raise ControlError(f"{args[0]} 操作失败（退出码 {result.returncode}）；未删除任何数据。")
    return result


def docker(*args, **kwargs):
    return run(["docker", "--context", CONTEXT, *args], **kwargs)


def local_context():
    result = run(["docker", "context", "inspect", CONTEXT], check=False)
    if result.returncode:
        return False
    endpoint = json.loads(result.stdout)[0]["Endpoints"]["docker"]["Host"]
    expected = "unix://" + str(Path.home() / ".colima/default/docker.sock")
    if endpoint != expected:
        raise ControlError("colima context 并非本机默认虚拟机，已停止操作。")
    return True


def available():
    return (
        local_context()
        and docker("info", "--format", "{{.ID}}", check=False, timeout=15).returncode == 0
    )


def start_vm():
    if available():
        return
    print("启动 Colima 默认虚拟机（沿用已有 CPU、内存和磁盘配置）…", flush=True)
    run(["colima", "start", "--profile", "default"], timeout=300)
    if not available():
        raise ControlError("虚拟机启动后 Docker 仍不可访问。")


def inventory():
    ids = docker("ps", "-aq").stdout.split()
    return json.loads(docker("inspect", *ids).stdout) if ids else []


def owned(row):
    name = row["Name"].lstrip("/")
    labels = row["Config"].get("Labels") or {}
    match = re.fullmatch(r"aftercare-fresh-([a-f0-9]{32})-.+", name)
    if match:
        return labels.get("com.aftercare.fresh-bootstrap") == match[1]
    return (
        name.startswith("aftercare-v2-")
        and labels.get("com.docker.compose.project") == "aftercare-v2"
        and Path(labels.get("com.docker.compose.project.working_dir", "/")).resolve()
        == ROOT / "infra"
    )


def selection(rows, target, *, starting=False):
    if target == "all":
        if starting:
            raise ControlError("all 只用于关闭或查看；启动请选 demo、refund、complex 或 app。")
        return [row for row in rows if owned(row)], []
    if target == "app":
        names = [f"aftercare-v2-{role}-app-1" for role in (*ROLES, "beat")]
        monitor = "aftercare-v2-prometheus-app-1"
        if any(row["Name"].lstrip("/") == monitor for row in rows):
            names.append(monitor)
        urls = ["http://127.0.0.1:5186"]
    else:
        names, urls = [], []
        for item in ("refund", "complex") if target == "demo" else (target,):
            filename, port = DEMO[item]
            path = ROOT / ".runtime" / filename
            if not path.is_file():
                raise ControlError(f"找不到演示登记文件：{filename}。此脚本不创建或覆盖环境。")
            state = json.loads(path.read_text())
            ident = state["run_id"]
            if (
                not re.fullmatch("[a-f0-9]{32}", ident)
                or state["url"] != f"http://127.0.0.1:{port}"
            ):
                raise ControlError("演示登记信息不匹配。")
            names.extend(f"aftercare-fresh-{ident}-{role}" for role in ROLES)
            if not starting:
                names.append(f"aftercare-fresh-{ident}-beat")
            urls.append(state["url"])
    indexed = {row["Name"].lstrip("/"): row for row in rows}
    missing = [name for name in names if name not in indexed]
    if missing and starting:
        raise ControlError("缺少已有容器：" + ", ".join(missing) + "；不会自动初始化数据。")
    selected = [indexed[name] for name in names if name in indexed]
    if not all(owned(row) for row in selected):
        raise ControlError("容器归属校验失败。")
    return selected, urls


def role(row):
    name = row["Name"].lstrip("/")
    return next(
        (item for item in (*ROLES, "beat", "prometheus") if re.search(rf"-{item}(?:-app-1)?$", name)), "other"
    )


def start(target):
    start_vm()
    rows, urls = selection(inventory(), target, starting=True)
    for group in (("pg", "redis", "objects", "prometheus"), ("api", "worker"), ("frontend",), ("beat",)):
        chosen = [row for row in rows if role(row) in group]
        stopped = [row["Id"] for row in chosen if not row["State"]["Running"]]
        paused = [row["Id"] for row in chosen if row["State"]["Paused"]]
        if stopped:
            print("启动：" + "、".join(group), flush=True)
            docker("start", *stopped)
        if paused:
            docker("unpause", *paused)
        if group == ("pg", "redis", "objects", "prometheus"):
            for row in chosen:
                if role(row) == "pg":
                    wait_command(row["Id"], ["pg_isready", "-h", "127.0.0.1"])
                elif role(row) == "redis":
                    wait_command(row["Id"], ["redis-cli", "ping"])
    for url in urls:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                with urlopen(url + "/api/readyz", timeout=4) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(2)
        else:
            raise ControlError(f"{url} 未在 120 秒内就绪；容器保留，请用 status 查看。")
        print("页面已就绪：" + url, flush=True)
    for row in rows:
        if role(row) == "worker":
            wait_command(
                row["Id"],
                ["celery", "-A", "aftercare.worker", "inspect", "--timeout", "3", "ping"],
                seconds=90,
                timeout=25,
            )
    print("后台任务进程已响应。演示环境不启动定时调度器，也不会重跑历史模型验收。")


def wait_command(ident, command, *, seconds=60, timeout=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if docker("exec", ident, *command, check=False, timeout=timeout).returncode == 0:
            return
        time.sleep(2)
    raise ControlError("依赖或任务进程未能就绪；容器保留，请查看状态。")


def stop(target):
    if not available():
        print("虚拟机或 Docker 已关闭，无需停止容器。")
        return
    selected, _ = selection(inventory(), target)
    for group in (("beat",), ("frontend", "api"), ("worker",), ("redis", "objects", "pg", "prometheus", "other")):
        ids = [row["Id"] for row in selected if role(row) in group and row["State"]["Running"]]
        if ids:
            print("停止：" + "、".join(group), flush=True)
            docker("stop", "--timeout", "75", *ids, timeout=110)
    ids = {row["Id"] for row in selected}
    if any(row["State"]["Running"] for row in inventory() if row["Id"] in ids):
        raise ControlError("仍有选中服务运行，请检查状态。")
    print("售后服务已停止，容器、数据库、照片和凭据均保留。")


def stop_vm():
    if available():
        running = [row for row in inventory() if row["State"]["Running"]]
        if running:
            names = ", ".join(row["Name"].lstrip("/") for row in running)
            raise ControlError("虚拟机仍被运行中的容器使用，已保留：" + names)
    result = run(["colima", "status", "--profile", "default"], check=False)
    if result.returncode == 0:
        run(["colima", "stop", "--profile", "default"], timeout=180)
    print("Colima 虚拟机已关闭。")


def status(target):
    if not available():
        print("Colima / Docker：未运行。start 会自动启动虚拟机。")
        return
    print("Colima / Docker：运行中")
    rows, urls = selection(inventory(), target)
    for row in rows:
        state = row["State"]
        label = "暂停" if state["Paused"] else ("运行" if state["Running"] else "已停止")
        health = state.get("Health", {}).get("Status")
        print(f"{label:4} {row['Name'].lstrip('/')}" + (f" [{health}]" if health else ""))
    for url in urls:
        print("入口：" + url)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("start", "stop", "restart", "status", "vm-start", "vm-stop")
    )
    parser.add_argument("--target", choices=("demo", "refund", "complex", "app", "all"))
    parser.add_argument("--vm", action="store_true", help="stop 时同时尝试关闭空闲的虚拟机")
    args = parser.parse_args()
    if args.vm and args.action != "stop":
        parser.error("--vm 仅能用于 stop")
    target = args.target or ("all" if args.action == "stop" else "demo")
    if args.action in ("start", "restart") and target == "all":
        parser.error("不能批量启动全部历史测试环境")
    (ROOT / ".runtime").mkdir(exist_ok=True)
    with (ROOT / ".runtime/service-control.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ControlError("已有启动或关闭操作进行中，请稍后再试。") from None
        if args.action == "start":
            start(target)
        elif args.action == "stop":
            stop(target)
            if args.vm:
                stop_vm()
        elif args.action == "restart":
            stop(target)
            start(target)
        elif args.action == "status":
            status(target)
        elif args.action == "vm-start":
            start_vm()
        else:
            stop("all")
            stop_vm()


if __name__ == "__main__":
    try:
        main()
    except (ControlError, ValueError, KeyError) as error:
        print(f"未完成：{error}", file=sys.stderr)
        sys.exit(1)
