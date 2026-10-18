"""The portal IMAGE must be self-contained.

portal/Dockerfile installs ONLY portal/requirements.txt and copies the .py files named in
`_SHIP` (portal/portal_ops.py ships exactly those, plus the frontend source). So an import in
those files that is neither shipped nor declared is a runtime crash in the container, not at
test time — the offline suite runs against the developer venv, where the whole driver and
every dependency is importable.

caught live (crimson-relay): the portal container crash-looped with
    ModuleNotFoundError: No module named 'requests'
because console.py imported requests but requirements.txt never listed it. The same class of
defect — a driver import in an image that ships no driver (`from infra.console import
vncproxy_mint`, and `infra/__init__` pulls in infra.gates and the whole deploy driver) — came
from the later dedupe commit. Both are caught here.
"""

import ast
import re
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# Guaranteed by a declared direct dependency, so not listed in requirements.txt by design:
# starlette rides fastapi, urllib3 rides requests.
_TRANSITIVE = {"starlette", "urllib3"}

_FALLBACK_SHIP = ("Dockerfile", "compose.yaml", "requirements.txt", "auth.py", "console.py",
                  "app.py")


def _shipped_files():
    """The `_SHIP` tuple from portal/portal_ops.py (what actually lands in the image)."""
    try:
        tree = ast.parse((_REPO / "portal" / "portal_ops.py").read_text())
    except OSError:
        return _FALLBACK_SHIP
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_SHIP":
                    try:
                        return tuple(ast.literal_eval(node.value))
                    except ValueError:
                        pass
    return _FALLBACK_SHIP


def _declared_requirements():
    text = (_REPO / "portal" / "requirements.txt").read_text()
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(re.split(r"[=<>~!\[;]", line)[0].strip().lower().replace("-", "_"))
    return names


def _guarded_import_nodes(tree):
    """Nodes inside a `try: ... except ImportError:` — the optional/dev-mode arm.

    app.py imports `from portal.auth import ...` first and falls back to `from auth import ...`
    (the flat layout the container uses), so a guarded import is not evidence the module is
    required at runtime.
    """
    guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches = False
        for handler in node.handlers:
            t = handler.type
            if t is None:
                catches = True
            elif isinstance(t, ast.Name) and t.id in ("ImportError", "ModuleNotFoundError"):
                catches = True
            elif isinstance(t, ast.Tuple) and any(
                    isinstance(e, ast.Name) and e.id in ("ImportError", "ModuleNotFoundError")
                    for e in t.elts):
                catches = True
        if catches:
            for stmt in node.body:
                for inner in ast.walk(stmt):
                    guarded.add(id(inner))
    return guarded


def _top_level_imports(path):
    tree = ast.parse(path.read_text())
    guarded = _guarded_import_nodes(tree)
    mods = set()
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def _image_python_files():
    return [name for name in _shipped_files() if name.endswith(".py")]


def _driver_modules():
    """Repo-root modules/packages that only exist on the deploy side."""
    mods = {p.stem for p in _REPO.glob("*.py")}
    mods |= {p.name for p in _REPO.iterdir() if p.is_dir() and not p.name.startswith(".")}
    return mods


class PortalImageImports(unittest.TestCase):

    def test_no_driver_imports(self):
        local = {p.stem for p in (_REPO / "portal").glob("*.py")}
        driver = _driver_modules()
        for name in _image_python_files():
            for mod in _top_level_imports(_REPO / "portal" / name):
                if mod in sys.stdlib_module_names or mod in local:
                    continue
                if mod in driver:
                    self.fail(f"portal/{name} imports driver module '{mod}', which is NOT "
                              f"shipped to the engine (portal/portal_ops._SHIP)")

    def test_third_party_imports_are_declared(self):
        declared = _declared_requirements()
        local = {p.stem for p in (_REPO / "portal").glob("*.py")}
        for name in _image_python_files():
            for mod in _top_level_imports(_REPO / "portal" / name):
                if mod in sys.stdlib_module_names or mod in local or mod in _TRANSITIVE:
                    continue
                self.assertIn(
                    mod.lower().replace("-", "_"), declared,
                    f"portal/{name} imports '{mod}' but portal/requirements.txt does not "
                    f"declare it — the container would crash on start")

