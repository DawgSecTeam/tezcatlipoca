"""The portal image must be self-contained: unshipped or undeclared imports crash the container."""

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
    """try/except ImportError arms are optional fallbacks, not runtime requirements."""
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

