"""Start the FastAPI backend and Vite frontend as one development process."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = PROJECT_ROOT / "frontend"


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
