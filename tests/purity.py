"""Shared architectural purity guards.

Every ``fintin/core/*`` engine is meant to be pure: no network, no database, no
filesystem — I/O arrives through injected ports. These helpers assert that by
statically inspecting a module's imports, so coupling core to an adapter fails
the suite instead of landing quietly.

Two things make this stronger than a hand-maintained denylist of known-impure
package names:

* **Allowlist, not denylist.** ``assert_module_is_pure`` requires a module's
  top-level imports to be a *subset* of :data:`PURE_IMPORTS`, so an impure
  dependency nobody thought to ban (an aliased client, a new I/O library) fails
  the guard on arrival rather than slipping past three hardcoded names.
* **No concrete adapters.** It also rejects any ``fintin.adapters.*`` import:
  core depends on ports, never on the implementations behind them.

Paths resolve against the repo root, so the guards hold regardless of which
directory pytest was invoked from.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Standard-library modules that cannot themselves perform I/O, plus `fintin`
# (narrowed further by the adapter check). Deliberately excludes `os`, `pathlib`,
# `socket`, `subprocess` and friends — a pure engine needing the filesystem or
# the network takes a port instead.
PURE_IMPORTS = frozenset(
    {
        "__future__",
        "abc",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "functools",
        "itertools",
        "math",
        "re",
        "typing",
        "fintin",
    }
)


def module_imports(rel_path: str, *, dotted: bool = False) -> set[str]:
    """Every module imported anywhere in ``rel_path`` (relative to the repo root).

    ``ast.walk`` visits the whole tree, so an import deferred inside a function
    body counts too — not just the module-level import block. Returns top-level
    names (``fintin``) unless ``dotted``, which returns full paths
    (``fintin.core.ingest``).
    """
    tree = ast.parse((_REPO_ROOT / rel_path).read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names if dotted else {name.split(".")[0] for name in names}


def assert_no_adapter_imports(rel_path: str) -> None:
    """Assert the module reaches no concrete adapter (``fintin.adapters.*``)."""
    adapters = sorted(
        name
        for name in module_imports(rel_path, dotted=True)
        if name.startswith("fintin.adapters")
    )
    assert not adapters, (
        f"{rel_path} imports concrete adapter(s) {adapters}; depend on an injected "
        "port instead."
    )


def assert_module_is_pure(rel_path: str, *, allow: Iterable[str] = ()) -> None:
    """Assert the module imports nothing impure and no concrete adapter.

    ``allow`` widens the allowlist for a module with a justified extra dependency.
    """
    permitted = PURE_IMPORTS | set(allow)
    impure = sorted(module_imports(rel_path) - permitted)
    assert not impure, (
        f"{rel_path} imports non-pure module(s) {impure}. A pure module may import "
        f"only {sorted(permitted)} — move the I/O behind an injected port, or widen "
        "this call via allow=(...) if the dependency really is pure."
    )
    assert_no_adapter_imports(rel_path)


def assert_no_edgar_import(rel_path: str) -> None:
    """Narrow zero-network guard: the module never imports ``edgar``.

    Weaker than :func:`assert_module_is_pure` on purpose — for modules allowed a
    real infrastructure dependency (a store adapter) that must still stay off the
    wire (NFR-7).
    """
    assert "edgar" not in module_imports(rel_path), (
        f"{rel_path} imports `edgar`; this module must not reach live EDGAR (NFR-7)."
    )
