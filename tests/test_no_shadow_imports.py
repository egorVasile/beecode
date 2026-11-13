"""No function may rebind a name its module already imports.

`/history` once killed the whole REPL with UnboundLocalError: `run_repl`
carried a late `from beeagent.i18n import L` on a branch the suite never
takes, and any binding in a function body — however unreachable — makes the
name local to the whole function. Module-level duplicates of the same import
are the same landmine with a longer fuse: the next edit that uses the name
above the late import ships the crash.

The rule this pins, for every function in `beeagent/` (nested scopes keep
their own locals and are not inspected):
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "beeagent"


def _module_top_names(tree):
    top = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                top[alias.asname or alias.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top[(alias.asname or alias.name).split(".")[0]] = node.lineno
    return top


def _direct_binds(func):
    bound = {}
    stack = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.setdefault(node.id, node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.setdefault(alias.asname or alias.name.split(".")[0],
                                 node.lineno)
    for group in (func.args.posonlyargs, func.args.args, func.args.kwonlyargs):
        for arg in group:
            bound.setdefault(arg.arg, func.lineno)
    for extra in (func.args.vararg, func.args.kwarg):
        if extra is not None:
            bound.setdefault(extra.arg, func.lineno)
    return bound


def _scan():
    hits = []
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        top = _module_top_names(tree)
        if not top:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for name, line in _direct_binds(node).items():
                if name in top:
                    hits.append(f"{path.name}:{node.name}:{line} rebinds {name!r}")
    return hits


def test_no_function_rebinds_a_module_level_import():
    hits = _scan()
    assert hits == [], \
        "a late rebinding makes the name function-local and kills every use " \
        "above it with UnboundLocalError:\n" + "\n".join(hits)
