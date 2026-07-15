"""Ordered, byte-bounded IPython execution protocol. Runs inside the sandbox.

This module intentionally uses only the standard library and the client object
passed by ``kernel_entry``. The container image does not install Henry itself.
"""

from __future__ import annotations

import json
import queue
import re
import time
from collections.abc import Callable
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_MAX_TOTAL_BYTES = 256 * 1024
_MAX_ITEM_BYTES = 256 * 1024
_MAX_ITEMS = 256
_MAX_FIELD_BYTES = 16 * 1024
_MAX_CHANNEL_FAILURES = 5
_CHANNEL_RETRY_SLEEP_S = 0.05


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _cap_utf8(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def drain_execution(
    kc: Any,
    km: Any,
    code: str,
    timeout: float,
    max_total_bytes: int = _MAX_TOTAL_BYTES,
    max_item_bytes: int = _MAX_ITEM_BYTES,
    max_items: int = _MAX_ITEMS,
) -> dict[str, Any]:
    """Execute one cell and return ordered nbformat-shaped output dictionaries."""

    if max_total_bytes < 2 or max_item_bytes < 2 or max_items < 0:
        raise ValueError("output budgets must allow a JSON list and non-negative item count")

    msg_id = kc.execute(code, silent=False, store_history=True, allow_stdin=False)
    outputs: list[dict[str, Any]] = []
    status = "ok"
    execution_count = 0
    truncated = False

    def _fits(output: dict[str, Any]) -> bool:
        try:
            return _json_bytes(output) <= max_item_bytes and _json_bytes([*outputs, output]) <= max_total_bytes
        except (TypeError, ValueError):
            return False

    def _append(output: dict[str, Any]) -> bool:
        nonlocal truncated
        if len(outputs) >= max_items or not _fits(output):
            truncated = True
            return False
        outputs.append(output)
        return True

    def _fit_text(
        text: str,
        make_output: Callable[[str], dict[str, Any]],
        *,
        field_limit: int | None = None,
    ) -> tuple[str, bool]:
        candidate = text
        cut = False
        if field_limit is not None:
            candidate, cut = _cap_utf8(candidate, field_limit)
        if _fits(make_output(candidate)):
            return candidate, cut

        low = 0
        high = len(candidate)
        while low < high:
            middle = (low + high + 1) // 2
            if _fits(make_output(candidate[:middle])):
                low = middle
            else:
                high = middle - 1
        return candidate[:low], True

    def _placeholder(reason: str) -> None:
        nonlocal truncated
        truncated = True
        prefix = "<dropped: "
        suffix = ">"

        def _make(value: str) -> dict[str, Any]:
            return {
                "output_type": "error",
                "ename": "OutputDropped",
                "evalue": f"{prefix}{value}{suffix}",
                "traceback": [],
            }

        fitted, _ = _fit_text(reason, _make, field_limit=_MAX_FIELD_BYTES - _utf8_bytes(prefix + suffix))
        _append(_make(fitted))

    def _append_stream(content: dict[str, Any]) -> None:
        nonlocal truncated
        text = str(content.get("text", ""))
        name = str(content.get("name", "stdout"))

        def _make(value: str) -> dict[str, Any]:
            return {"output_type": "stream", "name": name, "text": value}

        fitted, cut = _fit_text(text, _make)
        truncated = truncated or cut
        if fitted or not text:
            _append(_make(fitted))

    def _append_bundle(output_type: str, content: dict[str, Any]) -> None:
        output = {
            "output_type": output_type,
            "data": content.get("data") or {},
            "metadata": content.get("metadata") or {},
            "execution_count": content.get("execution_count"),
        }
        if not _append(output):
            _placeholder(f"{output_type} exceeds output limit")

    def _append_error(content: dict[str, Any]) -> None:
        nonlocal truncated
        output: dict[str, Any] = {
            "output_type": "error",
            "ename": "",
            "evalue": "",
            "traceback": [],
        }

        def _fit_error_field(key: str, value: str) -> None:
            nonlocal truncated

            def _make(candidate: str) -> dict[str, Any]:
                return {**output, key: candidate}

            fitted, cut = _fit_text(value, _make, field_limit=_MAX_FIELD_BYTES)
            output[key] = fitted
            truncated = truncated or cut

        _fit_error_field("ename", str(content.get("ename", "")))
        _fit_error_field("evalue", str(content.get("evalue", "")))

        for raw_frame in content.get("traceback") or []:
            frame = _strip_ansi(str(raw_frame))

            def _make(candidate: str) -> dict[str, Any]:
                return {**output, "traceback": [*output["traceback"], candidate]}

            fitted, cut = _fit_text(frame, _make, field_limit=_MAX_FIELD_BYTES)
            if not fitted and frame:
                truncated = True
                break
            output["traceback"].append(fitted)
            truncated = truncated or cut
            if cut:
                break

        _append(output)

    deadline = time.monotonic() + timeout
    channel_failures = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if km is not None:
                try:
                    km.interrupt_kernel()
                except Exception:
                    pass
            return {
                "status": "error",
                "outputs": outputs,
                "execution_count": execution_count,
                "timed_out": True,
                "truncated": truncated,
            }
        try:
            message = kc.get_iopub_msg(timeout=min(remaining, 0.5))
        except queue.Empty:
            continue
        except Exception:
            # An empty queue is normal and already waited out its timeout. Any
            # other failure returns instantly, so retrying it at full speed
            # spins the CPU until the deadline. Back off, and give up once the
            # channel looks genuinely broken.
            channel_failures += 1
            if channel_failures >= _MAX_CHANNEL_FAILURES:
                status = "error"
                break
            time.sleep(_CHANNEL_RETRY_SLEEP_S)
            continue
        channel_failures = 0
        if message.get("parent_header", {}).get("msg_id") != msg_id:
            continue

        message_type = message.get("msg_type", "")
        content = message.get("content", {})
        if message_type == "stream":
            _append_stream(content)
        elif message_type == "execute_input":
            execution_count = content.get("execution_count", execution_count)
        elif message_type == "execute_result":
            execution_count = content.get("execution_count", execution_count)
            _append_bundle("execute_result", content)
        elif message_type in ("display_data", "update_display_data"):
            _append_bundle("display_data", content)
        elif message_type == "error":
            status = "error"
            _append_error(content)
        elif message_type == "status" and content.get("execution_state") == "idle":
            break

    shell_deadline = time.monotonic() + 1.0
    matching_reply: dict[str, Any] | None = None
    while matching_reply is None:
        remaining = shell_deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            reply = kc.get_shell_msg(timeout=remaining)
        except queue.Empty:
            break
        except Exception:
            break
        if reply.get("parent_header", {}).get("msg_id") == msg_id:
            matching_reply = reply

    if matching_reply is None:
        status = "error"
    else:
        reply_content = matching_reply.get("content", {})
        execution_count = reply_content.get("execution_count", execution_count)
        if reply_content.get("status") != "ok":
            status = "error"

    return {
        "status": status,
        "outputs": outputs,
        "execution_count": execution_count,
        "timed_out": False,
        "truncated": truncated,
    }
