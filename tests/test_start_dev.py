from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


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
