from __future__ import annotations

import importlib.util
import sys
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "start_dev_under_test",
    Path(__file__).parents[1] / "start_dev.py",
)
assert _SPEC is not None and _SPEC.loader is not None
start_dev = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = start_dev
_SPEC.loader.exec_module(start_dev)


def test_port_check_continues_when_ports_are_free(monkeypatch) -> None:
    monkeypatch.setattr(start_dev, "_port_is_available", lambda _port: True)

    assert start_dev._ensure_ports_available(input_func=lambda _prompt: "unused") is True


def test_port_check_keeps_process_when_user_declines(monkeypatch) -> None:
    monkeypatch.setattr(start_dev, "_port_is_available", lambda port: port != 8000)
    monkeypatch.setattr(
        start_dev,
        "_find_listeners",
        lambda _ports: [start_dev.PortListener(8000, 24544, "python")],
    )
    terminated: list[int] = []
    monkeypatch.setattr(start_dev, "_terminate_pid", lambda pid: terminated.append(pid) or True)

    assert start_dev._ensure_ports_available(input_func=lambda _prompt: "n") is False
    assert terminated == []


def test_port_check_terminates_each_confirmed_pid_once(monkeypatch) -> None:
    terminated: list[int] = []

    def available(_port: int) -> bool:
        return bool(terminated)

    monkeypatch.setattr(start_dev, "_port_is_available", available)
    monkeypatch.setattr(
        start_dev,
        "_find_listeners",
        lambda _ports: [
            start_dev.PortListener(8000, 24544, "python"),
            start_dev.PortListener(5173, 24544, "python"),
        ],
    )
    monkeypatch.setattr(start_dev.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(start_dev, "_terminate_pid", lambda pid: terminated.append(pid) or True)

    assert start_dev._ensure_ports_available(input_func=lambda _prompt: "yes") is True
    assert terminated == [24544]


def test_port_check_refuses_unsafe_cleanup_when_pid_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr(start_dev, "_port_is_available", lambda _port: False)
    monkeypatch.setattr(start_dev, "_find_listeners", lambda _ports: [])

    assert start_dev._ensure_ports_available(input_func=lambda _prompt: "yes") is False


@pytest.mark.parametrize("body,status,expected", [
    ({"ready": True}, 200, True),
    ({"ready": False, "error": "模型未配置"}, 200, True),
    ({"ready": True}, 503, False),
    ({"unrelated_service": True}, 200, False),
    ([], 200, False),
    ("invalid JSON", 200, False),
])
def test_backend_readiness_requires_http_but_not_model_configuration(monkeypatch, body, status, expected):
    response = io.BytesIO(body.encode() if isinstance(body, str) else json.dumps(body).encode())
    response.status = status
    def open_url(url, timeout):
        assert url == "http://127.0.0.1:8000/api/config"
        assert timeout == 0.5
        return response
    monkeypatch.setattr(start_dev, "build_opener", lambda *_: SimpleNamespace(open=open_url))
    assert start_dev._backend_is_ready(0.5) is expected


def test_backend_readiness_tolerates_connection_refused(monkeypatch):
    def open_url(*args, **kwargs):
        raise URLError("connection refused")
    monkeypatch.setattr(start_dev, "build_opener", lambda *_: SimpleNamespace(open=open_url))
    assert start_dev._backend_is_ready(0.5) is False


def test_wait_for_backend_retries_until_http_ready(monkeypatch):
    probes = iter([False, False, True])
    sleeps = []
    monkeypatch.setattr(start_dev, "_backend_is_ready", lambda **_: next(probes))
    monkeypatch.setattr(start_dev.time, "sleep", sleeps.append)
    start_dev._wait_for_backend(SimpleNamespace(poll=lambda: None))
    assert len(sleeps) == 2


def test_wait_for_backend_detects_exit_before_probing(monkeypatch):
    monkeypatch.setattr(start_dev, "_backend_is_ready", lambda **_: pytest.fail("Exited backend must not be probed"))
    with pytest.raises(RuntimeError, match="退出码 7"):
        start_dev._wait_for_backend(SimpleNamespace(poll=lambda: 7))


def test_wait_for_backend_has_a_bounded_deadline(monkeypatch):
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(start_dev.time, "monotonic", lambda: clock.value)
    monkeypatch.setattr(start_dev.time, "sleep", lambda delay: setattr(clock, "value", clock.value + delay))
    monkeypatch.setattr(start_dev, "_backend_is_ready", lambda **_: False)
    with pytest.raises(RuntimeError, match="等待后端就绪超时"):
        start_dev._wait_for_backend(SimpleNamespace(poll=lambda: None), timeout=1)
    assert clock.value == 1


def setup_launch(monkeypatch):
    events, processes, stopped = [], [], []
    monkeypatch.setattr(start_dev.sys, "argv", ["start_dev.py"])
    monkeypatch.setattr(start_dev, "_validate_environment", lambda: "npm")
    monkeypatch.setattr(start_dev, "_ensure_ports_available", lambda: True)
    monkeypatch.setattr(start_dev, "_process_options", lambda: {})
    monkeypatch.setattr(start_dev, "_stop_process_tree", stopped.append)
    def spawn(command, **kwargs):
        name = "backend" if command[0] == sys.executable else "frontend"
        events.append(name)
        code = None if name == "backend" else 0
        process = SimpleNamespace(name=name, returncode=code, poll=lambda: code)
        processes.append(process)
        return process
    monkeypatch.setattr(start_dev.subprocess, "Popen", spawn)
    return events, processes, stopped


def test_main_waits_for_backend_before_launching_frontend(monkeypatch):
    events, processes, stopped = setup_launch(monkeypatch)
    def ready(process):
        assert process is processes[0] and len(processes) == 1
        events.append("backend_ready")
    monkeypatch.setattr(start_dev, "_wait_for_backend", ready)
    assert start_dev.main() == 0
    assert events == ["backend", "backend_ready", "frontend"]
    assert [p.name for p in stopped] == ["frontend", "backend"]


@pytest.mark.parametrize("failure,expected", [(RuntimeError("后端等待失败"), 1), (KeyboardInterrupt(), 130)])
def test_main_does_not_launch_frontend_when_backend_wait_fails(monkeypatch, failure, expected):
    events, processes, stopped = setup_launch(monkeypatch)
    def fail(_process):
        raise failure
    monkeypatch.setattr(start_dev, "_wait_for_backend", fail)
    assert start_dev.main() == expected
    assert events == ["backend"] and stopped == processes


def test_main_cleans_up_backend_when_frontend_spawn_fails(monkeypatch):
    events, processes, stopped = setup_launch(monkeypatch)
    original = start_dev.subprocess.Popen
    def spawn(command, **kwargs):
        if command[0] == "npm":
            raise OSError("frontend failed to start")
        return original(command, **kwargs)
    monkeypatch.setattr(start_dev, "_wait_for_backend", lambda process: None)
    monkeypatch.setattr(start_dev.subprocess, "Popen", spawn)
    assert start_dev.main() == 1
    assert events == ["backend"] and stopped == processes
