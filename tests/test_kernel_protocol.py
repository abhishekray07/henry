from __future__ import annotations

import json
import queue

from henry.sandbox.kernel_protocol import drain_execution


class _FakeClient:
    def __init__(self, iopub: list[dict], shell: dict | list[dict] | None = None) -> None:
        self._iopub = iopub
        self._shell = shell
        self.execute_kwargs: dict = {}

    def execute(self, code, silent=False, store_history=True, allow_stdin=True) -> str:
        self.execute_kwargs = {
            "silent": silent,
            "store_history": store_history,
            "allow_stdin": allow_stdin,
        }
        return "msg-1"

    def get_iopub_msg(self, timeout):
        if not self._iopub:
            raise queue.Empty()
        return self._iopub.pop(0)

    def get_shell_msg(self, timeout):
        if self._shell is None:
            raise queue.Empty()
        if isinstance(self._shell, list):
            if not self._shell:
                raise queue.Empty()
            return self._shell.pop(0)
        return self._shell


def _io(message_type, content, parent="msg-1"):
    return {"msg_type": message_type, "content": content, "parent_header": {"msg_id": parent}}


def _shell(status="ok", count=3):
    return {
        "content": {"status": status, "execution_count": count},
        "parent_header": {"msg_id": "msg-1"},
    }


def _output_bytes(output: dict) -> int:
    return len(json.dumps(output, ensure_ascii=False).encode("utf-8"))


def test_forbids_stdin() -> None:
    client = _FakeClient([_io("status", {"execution_state": "idle"})], _shell())
    drain_execution(client, None, "pass", timeout=5.0)
    assert client.execute_kwargs["allow_stdin"] is False


def test_preserves_output_order() -> None:
    client = _FakeClient(
        [
            _io("stream", {"name": "stdout", "text": "before\n"}),
            _io("display_data", {"data": {"image/png": "b64", "text/plain": "<img>"}}),
            _io("stream", {"name": "stdout", "text": "after\n"}),
            _io("execute_result", {"execution_count": 3, "data": {"text/plain": "42"}}),
            _io("status", {"execution_state": "idle"}),
        ],
        _shell(count=3),
    )
    result = drain_execution(client, None, "code", timeout=5.0)
    assert [output["output_type"] for output in result["outputs"]] == [
        "stream",
        "display_data",
        "stream",
        "execute_result",
    ]
    assert result["outputs"][1]["data"]["image/png"] == "b64"
    assert result["execution_count"] == 3


def test_error_output_is_captured_and_ansi_stripped() -> None:
    client = _FakeClient(
        [
            _io(
                "error",
                {
                    "ename": "ValueError",
                    "evalue": "boom",
                    "traceback": ["\x1b[31mTraceback\x1b[0m", "ValueError: boom"],
                },
            ),
            _io("status", {"execution_state": "idle"}),
        ],
        _shell(status="error"),
    )
    result = drain_execution(client, None, "raise", timeout=5.0)
    assert result["status"] == "error"
    error = result["outputs"][0]
    assert error["output_type"] == "error"
    assert error["traceback"] == ["Traceback", "ValueError: boom"]


def test_ignores_foreign_parent() -> None:
    client = _FakeClient(
        [
            _io("stream", {"name": "stdout", "text": "OTHER"}, parent="other"),
            _io("stream", {"name": "stdout", "text": "mine"}),
            _io("status", {"execution_state": "idle"}),
        ],
        _shell(),
    )
    result = drain_execution(client, None, "code", timeout=5.0)
    assert [output["text"] for output in result["outputs"]] == ["mine"]


def test_broken_iopub_channel_gives_up_instead_of_spinning() -> None:
    class _BrokenChannel:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, code, silent=False, store_history=True, allow_stdin=True) -> str:
            return "msg-1"

        def get_iopub_msg(self, timeout):
            # A dead socket fails instantly rather than waiting out the timeout,
            # so retrying it unthrottled burns CPU until the deadline.
            self.calls += 1
            raise OSError("socket is closed")

        def get_shell_msg(self, timeout):
            raise queue.Empty()

    client = _BrokenChannel()
    result = drain_execution(client, None, "code", timeout=30.0)

    assert result["status"] == "error"
    assert result["timed_out"] is False
    assert client.calls <= 10, f"busy-spun {client.calls} times on a dead channel"


def test_timeout_flags_and_returns_partial() -> None:
    client = _FakeClient([_io("stream", {"name": "stdout", "text": "partial"})])
    result = drain_execution(client, None, "loop", timeout=0.01)
    assert result["timed_out"] is True
    assert result["status"] == "error"
    assert result["outputs"][0]["text"] == "partial"


def test_budget_measures_utf8_bytes_not_codepoints() -> None:
    client = _FakeClient(
        [
            _io("stream", {"name": "stdout", "text": "é" * 400}),
            _io("status", {"execution_state": "idle"}),
        ],
        _shell(),
    )
    result = drain_execution(client, None, "code", timeout=5.0, max_total_bytes=500)
    assert result["truncated"] is True
    assert len(json.dumps(result["outputs"], ensure_ascii=False).encode("utf-8")) <= 500


def test_oversize_rich_item_replaced_with_placeholder() -> None:
    client = _FakeClient(
        [
            _io("display_data", {"data": {"image/png": "A" * (2 * 1024 * 1024)}}),
            _io("status", {"execution_state": "idle"}),
        ],
        _shell(),
    )
    result = drain_execution(client, None, "img", timeout=5.0, max_item_bytes=1024)
    item = result["outputs"][0]
    assert item["output_type"] == "error"
    assert "dropped" in (item["evalue"] or "")
    assert result["truncated"] is True


def test_stream_respects_per_item_budget() -> None:
    client = _FakeClient(
        [
            _io("stream", {"name": "stdout", "text": "x" * 1000}),
            _io("status", {"execution_state": "idle"}),
        ],
        _shell(),
    )
    result = drain_execution(client, None, "stream", timeout=5.0, max_item_bytes=200)
    assert result["truncated"] is True
    assert _output_bytes(result["outputs"][0]) <= 200


def test_item_count_cap() -> None:
    messages = [_io("stream", {"name": "stdout", "text": "x"}) for _ in range(50)]
    messages.append(_io("status", {"execution_state": "idle"}))
    result = drain_execution(_FakeClient(messages, _shell()), None, "loop", timeout=5.0, max_items=10)
    assert len(result["outputs"]) <= 10
    assert result["truncated"] is True


def test_huge_traceback_is_bounded() -> None:
    client = _FakeClient(
        [
            _io("error", {"ename": "E", "evalue": "v", "traceback": ["line " * 100_000]}),
            _io("status", {"execution_state": "idle"}),
        ],
        _shell(status="error"),
    )
    result = drain_execution(client, None, "raise", timeout=5.0, max_total_bytes=2048)
    assert len(json.dumps(result["outputs"], ensure_ascii=False).encode("utf-8")) <= 2048
    assert result["truncated"] is True


def test_combined_error_fields_respect_total_budget() -> None:
    client = _FakeClient(
        [
            _io(
                "error",
                {"ename": "E" * 1000, "evalue": "v" * 1000, "traceback": ["frame" * 1000]},
            ),
            _io("status", {"execution_state": "idle"}),
        ],
        _shell(status="error"),
    )
    result = drain_execution(client, None, "raise", timeout=5.0, max_total_bytes=300)
    assert result["truncated"] is True
    assert len(json.dumps(result["outputs"], ensure_ascii=False).encode("utf-8")) <= 300


def test_non_string_mime_data_and_metadata_are_budgeted() -> None:
    client = _FakeClient(
        [
            _io("display_data", {"data": {"application/json": {"values": ["x" * 500]}}, "metadata": {"large": "y" * 500}}),
            _io("status", {"execution_state": "idle"}),
        ],
        _shell(),
    )
    result = drain_execution(client, None, "rich", timeout=5.0, max_total_bytes=300)
    assert result["truncated"] is True
    assert len(json.dumps(result["outputs"], ensure_ascii=False).encode("utf-8")) <= 300


def test_shell_reply_is_filtered_and_authoritative() -> None:
    foreign = _shell(status="ok", count=99)
    foreign["parent_header"]["msg_id"] = "other"
    client = _FakeClient(
        [_io("status", {"execution_state": "idle"})],
        [foreign, _shell(status="aborted", count=4)],
    )
    result = drain_execution(client, None, "code", timeout=5.0)
    assert result["status"] == "error"
    assert result["execution_count"] == 4


def test_missing_shell_reply_is_an_error() -> None:
    client = _FakeClient([_io("status", {"execution_state": "idle"})])
    result = drain_execution(client, None, "code", timeout=5.0)
    assert result["status"] == "error"
