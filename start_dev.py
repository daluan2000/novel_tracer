"""Start the FastAPI backend and Vite frontend as one development process."""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
DEV_PORTS = (8000, 5173)


@dataclass(frozen=True)
class PortListener:
    port: int
    pid: int | None
    process_name: str


def _port_is_available(port: int) -> bool:
    """Check the exact loopback bind used by the development servers."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _process_name(pid: int) -> str:
    if os.name == "nt":
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).ProcessName",
        ]
    else:
        command = ["ps", "-p", str(pid), "-o", "comm="]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "未知进程"
    return result.stdout.strip() or "未知进程"


def _find_windows_listeners(ports: set[int]) -> list[PortListener]:
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    found: set[tuple[int, int]] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP" or fields[-2].upper() != "LISTENING":
            continue
        match = re.search(r":(\d+)$", fields[1])
        if not match:
            continue
        port = int(match.group(1))
        if port not in ports or not fields[-1].isdigit():
            continue
        found.add((port, int(fields[-1])))
    return [
        PortListener(port=port, pid=pid, process_name=_process_name(pid))
        for port, pid in sorted(found)
    ]


def _find_posix_listeners(ports: set[int]) -> list[PortListener]:
    lsof = shutil.which("lsof")
    if not lsof:
        return []
    found: set[tuple[int, int]] = set()
    for port in ports:
        try:
            result = subprocess.run(
                [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        for value in result.stdout.split():
            if value.isdigit():
                found.add((port, int(value)))
    return [
        PortListener(port=port, pid=pid, process_name=_process_name(pid))
        for port, pid in sorted(found)
    ]


def _find_listeners(ports: set[int]) -> list[PortListener]:
    return _find_windows_listeners(ports) if os.name == "nt" else _find_posix_listeners(ports)


def _terminate_pid(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return True


def _ensure_ports_available(
    ports: tuple[int, ...] = DEV_PORTS,
    *,
    input_func: Callable[[str], str] = input,
) -> bool:
    occupied = {port for port in ports if not _port_is_available(port)}
    if not occupied:
        return True

    listeners = _find_listeners(occupied)
    by_port = {listener.port for listener in listeners}
    listeners.extend(
        PortListener(port=port, pid=None, process_name="无法识别")
        for port in sorted(occupied - by_port)
    )
    print("[warning] 启动所需端口已被占用：", file=sys.stderr)
    for listener in listeners:
        pid = str(listener.pid) if listener.pid is not None else "未知"
        print(
            f"  - 端口 {listener.port}: PID {pid} ({listener.process_name})",
            file=sys.stderr,
        )

    if any(listener.pid is None for listener in listeners):
        print(
            "[error] 无法识别全部占用进程，未执行清理。请手动释放端口后重试。",
            file=sys.stderr,
        )
        return False
    try:
        answer = input_func("是否终止以上占用进程并继续启动？[y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n[info] 已取消启动。", file=sys.stderr)
        return False
    if answer not in {"y", "yes"}:
        print("[info] 已保留占用进程并取消启动。", file=sys.stderr)
        return False

    failed: list[int] = []
    for pid in sorted({listener.pid for listener in listeners if listener.pid is not None}):
        print(f"[info] 正在终止 PID {pid}……", flush=True)
        if not _terminate_pid(pid):
            failed.append(pid)
    time.sleep(0.3)
    still_occupied = [port for port in ports if not _port_is_available(port)]
    if failed or still_occupied:
        if failed:
            print(f"[error] 无法终止 PID：{', '.join(map(str, failed))}", file=sys.stderr)
        if still_occupied:
            print(
                f"[error] 端口仍被占用：{', '.join(map(str, still_occupied))}",
                file=sys.stderr,
            )
        return False
    print("[ok] 已释放启动所需端口。", flush=True)
    return True


def _npm_command() -> str:
    candidates = ("npm.cmd", "npm") if os.name == "nt" else ("npm",)
    for candidate in candidates:
        command = shutil.which(candidate)
        if command:
            return command
    raise RuntimeError("未找到 npm，请先安装 Node.js 及 npm。")


def _validate_environment() -> str:
    npm = _npm_command()
    if not (FRONTEND_ROOT / "node_modules").is_dir():
        raise RuntimeError("缺少 frontend/node_modules，请先在 frontend 目录运行 npm install。")
    if not (PROJECT_ROOT / ".env").is_file():
        print(
            "[warning] 未找到 .env；调用模型前请复制 .env.example 并配置 API Key。",
            file=sys.stderr,
            flush=True,
        )
    return npm


def _process_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _backend_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_root = str(PROJECT_ROOT / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root + os.pathsep + existing if existing else source_root
    )
    return environment


def _usage() -> None:
    print("usage: start_dev.py [--check]")


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--help"]:
        _usage()
        return 0
    if arguments not in ([], ["--check"]):
        _usage()
        return 2

    try:
        npm = _validate_environment()
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    print(f"[info] project:  {PROJECT_ROOT}")
    print(f"[info] backend: http://127.0.0.1:8000")
    print(f"[info] frontend: http://127.0.0.1:5173")
    if arguments == ["--check"]:
        print(f"[ok] python: {sys.executable}")
        print(f"[ok] npm:    {npm}")
        return 0

    if not _ensure_ports_available():
        return 1

    print("[info] 按 Ctrl+C 同时停止前后端。", flush=True)
    options = _process_options()
    processes: list[subprocess.Popen[bytes]] = []
    interrupted = False

    try:
        processes.append(
            subprocess.Popen(
                [sys.executable, "-m", "novel_agent"],
                cwd=PROJECT_ROOT,
                env=_backend_environment(),
                **options,
            )
        )
        processes.append(
            subprocess.Popen(
                [npm, "run", "dev"],
                cwd=FRONTEND_ROOT,
                **options,
            )
        )

        while all(process.poll() is None for process in processes):
            time.sleep(0.25)

        exited = next(process for process in processes if process.poll() is not None)
        return int(exited.returncode or 0)
    except KeyboardInterrupt:
        interrupted = True
        print("\n[info] 正在停止前后端……", flush=True)
        return 130
    finally:
        for process in reversed(processes):
            _stop_process_tree(process)
        if not interrupted and processes:
            print("[info] 前后端进程已停止。", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
