"""Check a forged extension before it is installed — without running it.

Validation never executes the code: `plugin.py` is read with `ast`, never
imported, so a broken or hostile file costs a repair turn, not a process.
Every error names the file, the line and the fix in words the model can act
on directly; warnings name real risks (network, subprocess, dynamic imports)
without refusing legitimate packs — refusing is the loader's and the trust
gate's job, not the spellchecker's.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from beeagent.plugins.catalog import install_name_ok

#: Names a pack may not take: the loader would wire them, and the registry
#: refuses collisions at load — failing here, with the reason, beats failing
#: there with "already registered".
RESERVED = {"skill", "plugin", "mcp", "system", "beeagent", "config"}

MAX_PLUGIN_BYTES = 200 * 1024
MAX_SKILL_BYTES = 100 * 1024

#: Imports that reach outside the process. Warned, never refused: the browser
#: pack spawns processes for a living, and a validator that knows better than
#: the user is how legitimate packs learn to dodge validators.
RISKY_MODULES = {"socket", "subprocess", "os", "sys", "shutil", "pathlib",
                 "requests", "urllib", "http", "ctypes", "multiprocessing",
                 "threading", "pty", "signal", "importlib"}
RISKY_CALLS = {"eval", "exec", "__import__", "compile", "open"}


def _walk_files(root: Path) -> list[Path]:
    try:
        return sorted(p for p in root.rglob("*") if p.is_file()
                      and ".beeagent" not in p.parts)
    except OSError:
        return []


def validate_staged(path: str | Path) -> tuple[list[str], list[str]]:
    """Errors and warnings for the extension taking shape at `path`.

    Returns `(errors, warnings)`: empty errors means installable; warnings
    travel with the install report so the user sees what the model waved
    through. Directories nest one level (`<name>/SKILL.md`), like installs.
    """
    errors: list[str] = []
    warnings: list[str] = []
    root = Path(path)
    name = root.name
    if not install_name_ok(name):
        errors.append(
            f"{name}: bad pack name — it becomes a folder, so no separators, "
            f"dot-dots, colons, device names (nul, con, …) or edge whitespace")
        return errors, warnings
    if name in RESERVED:
        errors.append(f"{name}: reserved — pick another name, this one is infrastructure")
        return errors, warnings
    if not root.is_dir():
        errors.append(f"{name}: nothing staged — write the files first "
                      f"(SKILL.md and/or plugin.py under this folder)")
        return errors, warnings

    skill = root / "SKILL.md"
    entry = root / "plugin.py"
    manifest = root / "plugin.json"
    if not skill.is_file() and not entry.is_file():
        errors.append(
            f"{name}: needs SKILL.md (a skill), plugin.py (a tool pack), or both — "
            f"an empty folder installs nothing and teaches nothing")
        return errors, warnings

    if skill.is_file():
        errors.extend(_check_skill(name, skill))
    if entry.is_file():
        entry_errors, entry_warnings = _check_plugin(name, entry)
        errors.extend(entry_errors)
        warnings.extend(entry_warnings)
    if manifest.is_file():
        errors.extend(_check_manifest(name, manifest, has_plugin=entry.is_file()))
    for extra in _walk_files(root):
        if extra.name in ("SKILL.md", "plugin.py", "plugin.json"):
            continue
        if extra.suffix.lower() not in (".md", ".json", ".txt"):
            warnings.append(
                f"{extra.name}: ships inside the pack but only SKILL.md / "
                f"plugin.py / plugin.json load — data files ride along unread")
    return errors, warnings


def _check_skill(name: str, path: Path) -> list[str]:
    errors = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as e:
        return [f"{name}/SKILL.md: unreadable ({e}) — write UTF-8 text"]
    if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
        errors.append(f"{name}/SKILL.md: {len(text)} chars — skills are read "
                      f"whole into the prompt; split it or it eats the window")
    if not text.strip():
        errors.append(f"{name}/SKILL.md: empty — say when to use it and what to do")
    # No description requirement: the loader falls back to a generic line.
    return errors


def _check_plugin(name: str, path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        raw = path.read_bytes()
    except OSError as e:
        return [f"{name}/plugin.py: unreadable ({e})"], warnings
    if len(raw) > MAX_PLUGIN_BYTES:
        errors.append(f"{name}/plugin.py: {len(raw)} bytes — split helpers out "
                      f"or nobody will ever review it")
        return errors, warnings
    try:
        text = raw.decode("utf-8")
    except ValueError:
        return [f"{name}/plugin.py: not UTF-8 — save it as UTF-8"], warnings
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [f"{name}/plugin.py:{e.lineno}: syntax error — {e.msg}; "
                f"fix it and validate again"], warnings
    top = {node.targets[0].id for node in tree.body
           if isinstance(node, ast.Assign) and len(node.targets) == 1
           and isinstance(node.targets[0], ast.Name)}
    funcs = {node.name for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if "TOOLS" not in top and "setup" not in funcs:
        errors.append(f"{name}/plugin.py: needs TOOLS = [...] (tool classes) "
                      f"and/or def setup(api) (commands, settings, listeners) — "
                      f"without either it installs silently and does nothing")
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            target = node.func
            called.add(target.id if isinstance(target, ast.Name)
                       else getattr(target, "attr", ""))
    risky = sorted(imported & RISKY_MODULES)
    if risky:
        warnings.append(f"{name}/plugin.py imports {', '.join(risky)} — "
                        f"fine for honest packs, and exactly what a dishonest "
                        f"one needs; the user sees this line")
    risky_calls = sorted(called & RISKY_CALLS)
    if risky_calls:
        warnings.append(f"{name}/plugin.py calls {', '.join(risky_calls)} — "
                        f"same deal: legitimate uses exist, review the lines")
    return errors, warnings


def _check_manifest(name: str, path: Path, has_plugin: bool) -> list[str]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"{name}/plugin.json: not JSON ({e}) — fix or delete it"]
    if not isinstance(body, dict):
        return [f"{name}/plugin.json: must be an object with name/version/type"]
    errors = []
    if body.get("name", name) != name:
        errors.append(f"{name}/plugin.json: name is {body.get('name')!r} but the "
                      f"folder is {name!r} — they must match")
    if body.get("type", "plugin") not in ("plugin", "skill"):
        errors.append(f"{name}/plugin.json: type must be 'plugin' or 'skill'")
    if not has_plugin and body.get("type") == "plugin":
        errors.append(f"{name}/plugin.json says plugin but there is no plugin.py")
    return errors
