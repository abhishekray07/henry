from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable

from henry.interfaces import Integration


def discover() -> dict[str, Integration]:
    import henry.integrations.builtins as pkg

    found: dict[str, Integration] = {}
    for module_info in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"{pkg.__name__}.{module_info.name}")
        integration = module.get_integration()
        if integration.name in found:
            raise ValueError(f"duplicate integration {integration.name}")
        found[integration.name] = integration
    return found


def get_integrations(names: Iterable[str], registry: dict[str, Integration]) -> list[Integration]:
    return [registry[name] for name in names if name in registry]
