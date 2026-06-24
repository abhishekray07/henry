from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

import henry.integrations.builtins as builtins_pkg
from henry.integrations.registry import discover, get_integrations


def _write_integration(path: Path, module: str, name: str) -> None:
    path.joinpath(f"{module}.py").write_text(
        "\n".join(
            [
                "from dataclasses import dataclass",
                "@dataclass",
                "class Integration:",
                f"    name: str = {name!r}",
                "    auth_type: str = 'none'",
                "    allowed_domains: tuple[str, ...] = ()",
                "    def tools(self): return []",
                "    def prompt_fragment(self): return ''",
                "def get_integration(): return Integration()",
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture
def temp_builtin_package(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(builtins_pkg, "__path__", [str(tmp_path)])
    importlib.invalidate_caches()
    yield tmp_path
    for name in list(sys.modules):
        if name.startswith("henry.integrations.builtins.temp_"):
            del sys.modules[name]
    importlib.invalidate_caches()


def test_discover_scans_builtin_package(temp_builtin_package) -> None:
    _write_integration(temp_builtin_package, "temp_alpha", "alpha")

    registry = discover()

    assert registry["alpha"].name == "alpha"


def test_discover_rejects_duplicate_names(temp_builtin_package) -> None:
    _write_integration(temp_builtin_package, "temp_alpha", "same")
    _write_integration(temp_builtin_package, "temp_beta", "same")

    with pytest.raises(ValueError, match="duplicate integration same"):
        discover()


def test_get_integrations_filters_missing_names() -> None:
    registry = {"alpha": object(), "beta": object()}

    assert get_integrations(["beta", "missing"], registry) == [registry["beta"]]
