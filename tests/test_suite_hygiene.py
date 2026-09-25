"""Proofs, not promises: each test here tries to break one rule of `conftest.py`.

A hygiene fixture that nobody exercises rots into a comment.  Every guard in
`tests/conftest.py` gets attacked here by the route that actually matters for
this project -- `httpx` (sync and async), the asyncio transport that bypasses
`socket.connect` on Windows, a shell redirect into the working tree, and the two
per-machine files the suite used to read as if they were its own.

Loopback is checked too, and on purpose: a guard that blocks everything is
indistinguishable from a guard that is broken, and `test_crax.py` and
`test_pool_server.py` both drive a real HTTP server on 127.0.0.1.
"""
import asyncio
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from beeagent.config.loader import CONFIG_FILE
from conftest import NetworkBlocked, _snapshot

CONFTEST = Path(__file__).resolve().parent / "conftest.py"
# The nested runs copy conftest.py into a temp folder and pytest it from there;
# that conftest imports `beeagent`, and the child's cwd is not the checkout — so
# without this the mini run dies at collection and every guard it proves goes
# unproven.
REPO = CONFTEST.parent.parent

# Documentation addresses.  Both are reserved and neither is supposed to answer;
# what is being asserted is that the *attempt* is stopped, so a real host is
# never needed and no test here can pass by an outage.
REMOTE_IP = "203.0.113.7"          # RFC 5737 TEST-NET-3
REMOTE_HOST = "test.invalid"       # RFC 6761: never resolvable


# --- the network guard --------------------------------------------------------

def test_httpx_sync_client_cannot_leave_the_machine():
    with pytest.raises(NetworkBlocked):
        httpx.get(f"https://{REMOTE_HOST}/", timeout=2)


def test_httpx_async_client_cannot_leave_the_machine():
    async def attempt():
        async with httpx.AsyncClient() as client:
            await client.get(f"https://{REMOTE_HOST}/", timeout=2)

    with pytest.raises(NetworkBlocked):
        asyncio.run(attempt())


def test_an_asyncio_transport_cannot_reach_a_bare_ip():
    """The hole this file exists to close.

    A guard that only wraps `socket.socket.connect` and `socket.getaddrinfo`
    looks complete and is not.  On Windows `asyncio` runs on the proactor loop,
    which connects with `ov.ConnectEx(conn.fileno(), address)`
    (`asyncio/windows_events.py:596`) straight on the descriptor, and
    `create_connection` skips `getaddrinfo` entirely when the host is already a
    numeric address.  Measured on this box before the loop layer was added: this
    exact call completed a TCP handshake to a public IP *with the guard
    installed*.  `httpx.AsyncClient` was covered, `asyncio.open_connection` was
    not -- and the pool client is the async one.
    """
    async def attempt():
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(REMOTE_IP, 80), timeout=3)
        writer.close()

    with pytest.raises(NetworkBlocked):
        asyncio.run(attempt())


def test_a_plain_socket_cannot_connect_out():
    with pytest.raises(NetworkBlocked):
        socket.socket().connect((REMOTE_IP, 53))


def test_create_connection_cannot_be_used_to_dial_out():
    """`socket.create_connection((ip, port))` reaches connect without resolving."""
    with pytest.raises(NetworkBlocked):
        socket.create_connection((REMOTE_IP, 80), timeout=2)


def test_a_dns_query_is_blocked_as_firmly_as_a_tcp_connection():
    """UDP to :53 bypasses `connect` entirely, so the guard names `sendto` too."""
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo(REMOTE_HOST, 443)
    with pytest.raises(NetworkBlocked):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(b"\x00" * 12, ("8.8.8.8", 53))
        finally:
            sock.close()


def test_urllib_is_not_a_way_round_the_guard():
    import urllib.request

    with pytest.raises(NetworkBlocked):
        urllib.request.urlopen(f"http://{REMOTE_HOST}/", timeout=2)


# --- and loopback must still work ---------------------------------------------

def test_the_guard_still_lets_a_loopback_server_through():
    """The honest fakes bind 127.0.0.1 and dial it for real.

    If this fails, the guard has become a wall and every "network blocked"
    message in the suite is now a lie about the test that got there first.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"ok\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert httpx.get(f"http://127.0.0.1:{port}/", timeout=5).text == "ok\n"
        # `create_connection` has already connected, so a second connect on the
        # same socket answers WSAEISCONN on Windows — what proves the guard lets
        # loopback through is the peer it reached, not a fresh connect attempt.
        peer = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            assert peer.getpeername()[1] == port
        finally:
            peer.close()
        assert socket.getaddrinfo("localhost", 80)

        async def via_asyncio():
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET / HTTP/1.0\r\n\r\n")
            body = await reader.read(64)
            writer.close()
            return body

        assert b"200" in asyncio.run(via_asyncio())
    finally:
        thread.join(timeout=0)
        server.shutdown()
        server.server_close()


def test_the_guard_is_installed_for_every_test_not_just_this_file():
    """A fixture-scoped guard that silently stopped applying is the bug's twin."""
    with pytest.raises(NetworkBlocked):
        httpx.get(f"https://{REMOTE_HOST}/", timeout=2)


def test_a_child_process_is_not_covered_and_that_is_the_one_remaining_hole():
    """Recorded, not fixed: the guard patches *this* interpreter.

    `beeagent/tools/shell.py:121` Popen's a bash or cmd child with a copy of
    `os.environ` and no network namespace, so a model-issued `curl` still leaves
    the machine.  Asserted as a property of the code rather than by dialing out,
    because a test that proved the hole would have to make one.
    """
    from beeagent.tools import shell

    source = Path(shell.__file__).read_text(encoding="utf-8")
    # Asserted as the property, not one literal line: the git hardening added an
    # `env=` argument to the same call, and a string match would then "prove" the
    # hole had been closed when only the wording moved.
    assert "**os.environ" in source or "env = dict(os.environ)" in source, \
        "the child is no longer handed a copy of our environment — re-read this test"
    assert "namespace" not in source.lower(), "there is still no sandbox around it"
    # ... so the only thing standing between a `curl` and the internet is the
    # permission gate, which is what tests/test_permissions.py holds the line on.


# --- the litter guard ---------------------------------------------------------

def test_the_litter_guard_fails_a_test_that_writes_into_the_working_tree(tmp_path):
    """Run the guard against a test that is knowingly dirty.

    A nested pytest, because "the fixture would have caught it" is only worth
    anything as "it does catch it".  The mini repository is real: a conftest copy
    and one test that drops `big.svg` in the cwd it was started in -- exactly the
    shape of the diagram incident, where the tool used the *process* cwd and the
    test never said otherwise.
    """
    if not CONFTEST.exists():
        pytest.skip("no conftest to copy")
    work = tmp_path / "mini-repo"
    (work / "tests").mkdir(parents=True)
    shutil.copy(str(CONFTEST), str(work / "tests" / "conftest.py"))
    (work / "tests" / "test_dirty.py").write_text(
        "def test_drops_a_file_in_the_cwd():\n"
        "    open('big.svg', 'w', encoding='utf-8').write('<svg/>')\n"
        "    assert True\n",
        encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "tests"],
        cwd=str(work), capture_output=True, text=True, timeout=180,
        env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(REPO)))

    out = result.stdout + result.stderr
    assert result.returncode != 0, "a test that litters the tree must not pass\n" + out[-2000:]
    assert "wrote to the working tree" in out, out[-2000:]
    assert "big.svg" in out, "name the file that was littered"
    assert not (work / "big.svg").exists(), \
        "the guard reports what a test wrote and then removes it, so the next " \
        "test is not blamed for it"


def test_the_litter_guard_notices_a_rewritten_file_without_a_new_name(tmp_path):
    """`beeagent.json` already exists, so a new-name diff would miss it.

    This is the failure that actually shipped: a `dispatch(ctx, "/provider ...")`
    with no agent took `workdir="."` and saved the developer's config over with
    the test's, which is invisible to any check that only looks for new files.
    """
    (tmp_path / "beeagent.json").write_text('{"model": "mine"}', encoding="utf-8")
    before = _snapshot(tmp_path)
    assert list(before) == ["beeagent.json"]
    (tmp_path / "beeagent.json").write_text('{"model": "theirs"}', encoding="utf-8")
    after = _snapshot(tmp_path)
    assert before != after, "size or mtime has to move, or the guard is blind"


def test_the_real_config_in_the_working_tree_survives_the_suite():
    """Prove the fix, not the intention.

    `beeagent.json` at the repository root held `"pool_url": "https://pool.example"`
    and `"pool_token": "t"` -- values invented by `test_commands.py`.  A test
    reached `save_config(cfg, ".")` because `ctx.agent` was None and `"."` is the
    fallback, and it rewrote the developer's provider settings, keys included,
    with a fake pool address.  The litter guard now makes that a failure at the
    test that does it.  Here we only assert the file on disk is not the test's.
    """
    from conftest import INVOCATION_DIR

    path = INVOCATION_DIR / CONFIG_FILE
    if not path.exists():
        return                                  # nothing to protect; also fine
    text = path.read_text(encoding="utf-8")
    assert "pool.example" not in text or "pool_token" not in text, (
        "the repository's own beeagent.json has been overwritten by test data; "
        "a test is calling save_config with the default workdir again")


# --- per-machine state --------------------------------------------------------

def test_the_pool_signing_key_is_never_the_developer_s(tmp_path):
    """`providers/pool.install_key` falls back to `~/.beecode/pool-key.json`.

    `test_pool_server.py` calls `enroll()`, which calls it.  Only
    `test_pool_client.py` had the environment guard, so the suite signed with --
    and would happily have minted over -- the key that identifies this machine to
    the pool.
    """
    from beeagent.providers.pool import install_key

    seed, public, device = install_key()
    written = Path(os.environ["BEECODE_POOL_KEY_FILE"])
    assert written.exists(), "install_key wrote somewhere other than the redirected home"
    assert tmp_path in written.parents
    assert seed and public and device


def test_the_home_directory_a_test_sees_is_not_the_one_on_disk():
    from pathlib import Path as P

    home = P.home()
    assert "fake-home" in str(home), (
        f"the suite is looking at a real profile: {home}. Anything that reads "
        "~/.beecode or ~/.becode is reading somebody's machine")


def test_the_trust_store_is_cold_for_every_test(tmp_path):
    """`trust._STORE`, `_GATES` and `_SHIPPED` are module-level caches.

    `_STORE` is built once from `store_path()`; redirecting HOME without also
    resetting it leaves a file handle into the *previous* test's temporary
    directory, so one folder's "yes, I trust it" answers for the next one.
    """
    pytest.importorskip("beeagent.core.trust", reason="the gate is newer than this checkout")
    from beeagent.core import trust

    assert trust._STORE is None, "a cached TrustStore survives into this test"
    assert trust._GATES == {}, "a cached ProjectTrust survives into this test"
    assert "fake-home" in str(trust.store_path())


def test_the_language_does_not_leak_from_one_file_to_the_next():
    """`test_context.py` and `test_pool_client.py` switch to Russian to quote it.

    `i18n._lang` is process-global with no undo, and `L(english, russian)` picks
    its half from that global -- so whichever file happened to run after one of
    them was asserting against a different language than the one it was written
    in.
    """
    from beeagent.i18n import L, get_lang

    assert get_lang() == "en"
    assert L("hello", "привет") == "hello"


def test_a_test_that_leaves_the_repl_task_global_is_failed_by_name(tmp_path):
    """Proved with a nested run, because the fixture is a teardown.

    `repl._ACTIVE_TASK` is a module global holding a live asyncio task.  A task
    that outlives its loop is cancelled into whoever's loop runs next, which is
    how `/stop` in one test ends up interrupting another test's answer.
    """
    work = tmp_path / "mini-repl"
    (work / "tests").mkdir(parents=True)
    shutil.copy(str(CONFTEST), str(work / "tests" / "conftest.py"))
    (work / "tests" / "test_leaves_a_task.py").write_text(
        "def test_leaves_the_active_task_behind():\n"
        "    from beeagent.ui import repl\n"
        "    import asyncio\n\n"
        "    async def make():\n"
        "        return asyncio.create_task(asyncio.sleep(30))\n\n"
        "    repl._ACTIVE_TASK = asyncio.run(make())\n"
        "    assert repl._ACTIVE_TASK is not None\n",
        encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "tests"],
        cwd=str(work), capture_output=True, text=True, timeout=180,
        env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(REPO)))
    out = result.stdout + result.stderr
    assert result.returncode != 0, "a leaked _ACTIVE_TASK must not pass\n" + out[-2000:]
    assert "_ACTIVE_TASK" in out, out[-2000:]


# --- collection honesty -------------------------------------------------------

def test_no_test_is_defined_twice_in_the_same_file():
    """A second `def test_x` rebinds the name, so the first body never runs again.

    pytest collected one, the file read as two, and what actually executed was
    whichever copy happened to sit last.  Found while strengthening
    `test_commands_tools.py`, which carried two `test_unknown_tool_command_is_safe`
    definitions -- and the one running was the weaker, because it came second.
    """
    offenders = _duplicate_names_in_files()
    assert not offenders, "defined twice, collected once:\n" + "\n".join(offenders)


def _duplicate_names_in_files():
    import ast

    offenders = []
    for path in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue                      # an in-flight file is not this check's job
        names = [node.name for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name.startswith("test_")]
        seen, dupes = set(), set()
        for name in names:
            if name in seen:
                dupes.add(name)
            seen.add(name)
        if dupes:
            offenders.append(f"{path.name}: {sorted(dupes)}")
    return offenders


def test_the_collection_pattern_covers_every_file_in_tests():
    """There is no `[tool.pytest.ini_options]` anywhere in this repository.

    Collection therefore runs on the built-in `test_*.py`, and a file named
    anything else -- `checks_tools.py`, `tools_spec.py` -- is never imported,
    never run and never reported as missing.  This is the collection-side twin of
    the import sweep the repo already runs over source.
    """
    tests_dir = Path(__file__).resolve().parent
    orphans = [p.name for p in sorted(tests_dir.glob("*.py"))
               if p.name != "conftest.py" and not p.name.startswith("test_")]
    assert not orphans, f"tests/ files no pattern will ever collect: {orphans}"


def test_no_source_module_is_outside_every_test_import_graph():
    """An ast sweep: which `beeagent/` module can no test reach, even transitively?

    `beeagent/core/agent.py` imports most of the package, so reachability is a
    weak promise -- but *unreachable from every test file* is strong enough to be
    worth a failure, and it is how `core/trust.py` would have stayed at zero.
    """
    import ast

    root = Path(__file__).resolve().parent.parent

    def module_name(path: Path) -> str:
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts and parts[0] == "server":
            # `test_pool_server.py` does `sys.path.insert(0, server/)` and then
            # `import pool_server`, so inside that checkout the operator's files
            # really are top-level modules.  Naming them `server.pool_server`
            # would make every one of them look unreachable to a sweep that is
            # otherwise honest.
            return parts[-1]
        return ".".join(parts)

    def package_of(path: Path) -> tuple:
        """The directory chain a module's relative imports resolve against.

        `beeagent/tools/bash.py` -> ("beeagent", "tools"), and so does
        `beeagent/tools/__init__.py` -> ("beeagent", "tools"): both are the
        package `beeagent.tools`, which is what `from .x import` counts levels on.
        """
        return path.relative_to(root).parts[:-1]

    def resolves_to(path: Path, node) -> str:
        """Absolute dotted target of one ImportFrom, `.` levels included.

        `from .base import BaseTool` in `beeagent/tools/bash.py` is
        `beeagent.tools.base`, and `from ..i18n import L` in git.py is
        `beeagent.i18n`.  A sweep that reads only absolute imports finds the
        whole tool layer unreachable, because that is the only way the tools
        refer to one another.
        """
        parts = list(package_of(path))
        if node.level:
            parts = parts[:len(parts) - (node.level - 1)]
        else:
            parts = []
        if node.module:
            parts = [*parts, node.module]
        return ".".join(parts)

    def names_imported(path: Path):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            return set()
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                target = resolves_to(path, node)
                if not target:
                    continue
                found.add(target)
                for alias in node.names:
                    found.add(f"{target}.{alias.name}" if target else alias.name)
        return {name for name in found if name}

    package = {}
    for base in ("beeagent", "server"):
        for path in (root / base).rglob("*.py"):
            if "templates" in path.parts or "__pycache__" in path.parts:
                continue
            package[module_name(path)] = path

    def answers_for(name: str):
        """The package module a dotted import target really loads."""
        if name in package:
            return name
        parts = name.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            candidate = ".".join(parts[:cut])
            if candidate in package:
                return candidate
        for module in package:
            if name.startswith(module + "."):
                return module
        return None

    def walk(path: Path):
        return {m for m in (answers_for(n) for n in names_imported(path)) if m}

    seen = set()
    frontier = []
    for test in sorted((root / "tests").glob("test_*.py")):
        frontier.extend(walk(test))

    while frontier:
        current = frontier.pop()
        if current in seen or current not in package:
            continue
        seen.add(current)
        # Loading `beeagent.tools.base` necessarily runs `beeagent` and
        # `beeagent.tools` first: an ancestor package is never "untested" merely
        # because the file itself is an empty `__init__`.
        parts = current.split(".")
        for cut in range(1, len(parts)):
            ancestor = ".".join(parts[:cut])
            if ancestor in package:
                seen.add(ancestor)
        frontier.extend(walk(package[current]))

    unreachable = sorted(set(package) - seen)
    assert not unreachable, (
        "no test reaches these modules, directly or through anything it imports:\n"
        + "\n".join(f"  {m}  ({package[m]})" for m in unreachable))
