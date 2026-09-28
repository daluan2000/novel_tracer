from __future__ import annotations

import runpy
import sys
from types import SimpleNamespace
from typing import Any

import novel_agent.web.app as web_app


def test_web_main_starts_uvicorn(monkeypatch) -> None:
    invocation: dict[str, Any] = {}

    def run(application: Any, **kwargs: Any) -> None:
        invocation["application"] = application
        invocation["kwargs"] = kwargs

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run))

    web_app.main()

    assert invocation == {
        "application": web_app.app,
        "kwargs": {"host": "127.0.0.1", "port": 8000, "reload": False},
    }


def test_package_main_delegates_to_web(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(web_app, "main", lambda: calls.append("started"))

    runpy.run_module("novel_agent", run_name="__main__")

    assert calls == ["started"]
