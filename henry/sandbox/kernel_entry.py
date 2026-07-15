"""Container-side entrypoint for booting, probing, and driving IPython."""

from __future__ import annotations

import base64
import json
import os
import sys
import time

try:
    from . import kernel_protocol
except ImportError:  # pragma: no cover - the image copies these as standalone modules
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kernel_protocol  # type: ignore[no-redef]

CONNECTION_FILE = "/workspace/.henry/kernel.json"


def _connection_file() -> str:
    """Where the kernel's connection file lives.

    The host sets this from the sandbox policy's workdir; only that path is
    writable, so a hardcoded default cannot serve a non-default workdir.
    """
    return os.environ.get("HENRY_KERNEL_CONNECTION_FILE") or CONNECTION_FILE


def _blocking_client(connection_file: str):
    from jupyter_client import BlockingKernelClient

    return BlockingKernelClient(connection_file=connection_file)


def _boot() -> None:
    from jupyter_client import KernelManager

    connection_file = _connection_file()
    os.makedirs(os.path.dirname(connection_file), exist_ok=True)
    manager = KernelManager(kernel_name="python3")
    manager.connection_file = connection_file
    manager.start_kernel()
    manager.write_connection_file()

    startup = os.environ.get("HENRY_KERNEL_STARTUP", "")
    if startup:
        client = manager.client()
        client.start_channels()
        try:
            client.wait_for_ready(timeout=60)
            kernel_protocol.drain_execution(client, manager, startup, timeout=60.0)
        finally:
            client.stop_channels()

    while manager.is_alive():
        time.sleep(1.0)


def _exec(code: str) -> None:
    client = _blocking_client(_connection_file())
    client.load_connection_file()
    client.start_channels()
    try:
        client.wait_for_ready(timeout=30)
        timeout = float(os.environ.get("HENRY_CELL_TIMEOUT", "120"))
        result = kernel_protocol.drain_execution(client, None, code, timeout=timeout)
    finally:
        client.stop_channels()
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()


def _ping() -> int:
    try:
        client = _blocking_client(_connection_file())
        client.load_connection_file()
        client.start_channels()
        try:
            client.wait_for_ready(timeout=float(os.environ.get("HENRY_PING_TIMEOUT", "5")))
        finally:
            client.stop_channels()
        return 0
    except Exception as exc:
        sys.stderr.write(f"ping failed: {exc}\n")
        return 1


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "boot":
        _boot()
        return 0
    if mode == "exec":
        if len(argv) != 3:
            sys.stderr.write("exec requires one base64-encoded code payload\n")
            return 2
        code = base64.b64decode(argv[2], validate=True).decode("utf-8")
        _exec(code)
        return 0
    if mode == "ping":
        return _ping()
    sys.stderr.write(f"unknown kernel_entry mode: {mode!r}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
