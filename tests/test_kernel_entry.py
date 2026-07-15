from __future__ import annotations

import base64
import json

from henry.sandbox import kernel_entry


class _FakeKernelClient:
    def load_connection_file(self) -> None: ...

    def start_channels(self) -> None: ...

    def wait_for_ready(self, timeout) -> None: ...

    def stop_channels(self) -> None: ...


def test_connection_file_follows_the_host_supplied_path(monkeypatch) -> None:
    monkeypatch.setenv("HENRY_KERNEL_CONNECTION_FILE", "/work/.henry/kernel.json")
    assert kernel_entry._connection_file() == "/work/.henry/kernel.json"


def test_connection_file_defaults_when_host_sets_nothing(monkeypatch) -> None:
    monkeypatch.delenv("HENRY_KERNEL_CONNECTION_FILE", raising=False)
    assert kernel_entry._connection_file() == "/workspace/.henry/kernel.json"


def test_exec_decodes_and_prints_json(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(kernel_entry, "_blocking_client", lambda connection_file: _FakeKernelClient())
    monkeypatch.setattr(
        kernel_entry.kernel_protocol,
        "drain_execution",
        lambda kc, km, code, timeout: seen.update(code=code)
        or {
            "status": "ok",
            "outputs": [],
            "execution_count": 1,
            "timed_out": False,
            "truncated": False,
        },
    )
    encoded = base64.b64encode(b"1+2").decode()

    result = kernel_entry.main(["kernel_entry.py", "exec", encoded])

    assert result == 0
    assert seen["code"] == "1+2"
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_ping_ok(monkeypatch) -> None:
    monkeypatch.setattr(kernel_entry, "_blocking_client", lambda connection_file: _FakeKernelClient())
    assert kernel_entry.main(["kernel_entry.py", "ping"]) == 0


def test_ping_unready(monkeypatch) -> None:
    class _DeadKernelClient(_FakeKernelClient):
        def wait_for_ready(self, timeout):
            raise RuntimeError("not ready")

    monkeypatch.setattr(kernel_entry, "_blocking_client", lambda connection_file: _DeadKernelClient())
    assert kernel_entry.main(["kernel_entry.py", "ping"]) == 1


def test_unknown_mode() -> None:
    assert kernel_entry.main(["kernel_entry.py", "wat"]) == 2


def test_exec_requires_payload(capsys) -> None:
    assert kernel_entry.main(["kernel_entry.py", "exec"]) == 2
    assert "requires" in capsys.readouterr().err
