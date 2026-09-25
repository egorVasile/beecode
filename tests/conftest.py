"""Shared test hygiene: no network, no litter, no state that survives a test.

Three incidents shaped this file.

1. A test made a live request because its fake provider was registered under a
   name the config did not select.  ``_no_outbound_network`` below makes that
   class of mistake impossible rather than unlikely: every route out of the
   process is checked, and only loopback is allowed through.  Loopback is what
   the honest fakes use — ``test_crax.py`` and ``test_pool_server.py`` both bind
   a real ``ThreadingHTTPServer`` on ``127.0.0.1`` and drive it with a real
   ``httpx`` client — so the guard cannot be satisfied by monkeypatching itself.
2. ``big.svg``, ``diagram.svg``, ``out.svg`` and ``nul`` have all appeared in the
   repository root during a full-suite run.  The trap is that the tools resolve
   against the *process* cwd, not the ``workdir`` handed around, so a test with
   no ``monkeypatch.chdir`` writes into whoever cloned the repo.
   ``_no_litter`` fails such a test by name and removes what it wrote, so one
   litterer cannot cascade into the next test's snapshot.
3. A green working tree over a broken committed tree shipped a public repo once.
   These fixtures are deliberately loud: a hidden global now shows up as a
   failure at the test that set it, not as a mystery three files later.

Everything here is stdlib only — this project does not take pytest plugins.
"""
import asyncio
import os
import re
import shutil
import socket
import sys
from pathlib import Path

import pytest

# The directory pytest was started in, which is also the repository root.  Pinned
# at import because a test is allowed to change the cwd underneath us.
_INVOCATION_DIR_NOTE = "pinned at import: a test is allowed to change the cwd"
INVOCATION_DIR = Path.cwd().resolve()
REPO_ROOT = Path(__file__).resolve().parent.parent
_CWD_LEAKS: list[tuple[str, str]] = []

# Directories whose contents churn for reasons no test is responsible for.
_NOISY_DIRS = {"__pycache__", ".git", ".pytest_cache", "build", "dist",
               "node_modules", ".venv", "venv"}
_NOISE_RE = re.compile(r"(^|/)(__pycache__|\.git|\.pytest_cache|build|dist|"
                       r"node_modules|\.venv|venv|-p)(/|$)")
# A file the suite is allowed to rewrite: nothing.  The set exists to make the
# omission obvious — every path under the repo is the developer's, so writing to
# any of one is a test bug.
_MAX_ENTRIES = 20000


# --------------------------------------------------------------------------
# 1. the network guard
# --------------------------------------------------------------------------

class NetworkBlocked(RuntimeError):
    """A test tried to leave the machine."""


def _is_loopback(host) -> bool:
    """True for the addresses the honest fakes legitimately bind and dial."""
    if isinstance(host, (bytes, bytearray)):
        host = host.decode("utf-8", "replace")
    name = str(host or "").strip().strip("[]")
    if not name or name in ("localhost", "::1", "::"):
        return True
    if name.startswith("127.") or name == "127":
        return True
    if name.startswith("::ffff:"):                     # IPv4-mapped IPv6
        return _is_loopback(name[len("::ffff:"):])
    return False


def _check_socket_address(family, address, via):
    if family in (getattr(socket, "AF_INET", None), getattr(socket, "AF_INET6", None)):
        if isinstance(address, tuple) and address:
            if not _is_loopback(address[0]):
                port = address[1] if len(address) > 1 else "?"
                raise NetworkBlocked(
                    f"BLOCKED: a test reached the network through {via} -> "
                    f"{address[0]!r}:{port}. Nothing in this suite may leave "
                    "the machine; point the provider at a 127.0.0.1 fake or "
                    "replace the client.")
    # AF_UNIX / AF_PIPE / AF_PACKET carry no host to leak through.


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch):
    """Raise on any connect, resolve or datagram send to a non-loopback address.

    Five layers, because no single one is enough and each was proven necessary
    on this box:

    * ``socket.socket.connect`` / ``connect_ex`` — the plain path, and the one
      asyncio's *selector* loop uses through ``loop.sock_connect``.
    * ``socket.getaddrinfo`` — catches a hostname before it becomes an address,
      which is what ``httpx`` (sync and async) and ``urllib`` trip over.
    * ``socket.create_connection`` — ``socket.create_connection((ip, port))``
      takes the address straight to ``connect``; keep it named in the error.
    * ``socket.socket.sendto`` — UDP, i.e. DNS, which bypasses ``connect``.
    * ``asyncio.BaseEventLoop.create_connection`` / ``create_datagram_endpoint``
      — the hole that matters on Windows.  ``ProactorEventLoop`` does not call
      ``socket.connect``: ``asyncio/windows_events.py:596`` runs
      ``ov.ConnectEx(conn.fileno(), address)``, a C call on the raw descriptor
      that neither the Python socket method nor ``getaddrinfo`` is consulted
      for, because ``create_connection`` skips resolution for a numeric host.
      A guard without this layer happily lets ``asyncio.open_connection
      ("93.184.216.34", 80)`` complete.  Note ``open_connection`` passes host
      and port *positionally*, so the wrapper reads both forms.
    """
    _real_connect = socket.socket.connect
    _real_connect_ex = socket.socket.connect_ex
    _real_sendto = socket.socket.sendto
    _real_getaddrinfo = socket.getaddrinfo
    _real_create_connection = socket.create_connection

    def connect(self, address, *a, **k):
        _check_socket_address(self.family, address, "socket.connect")
        return _real_connect(self, address, *a, **k)

    def connect_ex(self, address, *a, **k):
        _check_socket_address(self.family, address, "socket.connect_ex")
        return _real_connect_ex(self, address, *a, **k)

    def sendto(self, data, *rest, **k):
        address = rest[1] if len(rest) > 1 else k.get("address")
        if address is None and rest and isinstance(rest[0], tuple):
            address = rest[0]
        if address is not None:
            _check_socket_address(self.family, address, "socket.sendto")
        return _real_sendto(self, data, *rest, **k)

    def getaddrinfo(host, *a, **k):
        if host is not None and not _is_loopback(host):
            raise NetworkBlocked(
                f"BLOCKED: a test resolved {host!r} through socket.getaddrinfo. "
                "Nothing in this suite may reach a real name server; use a "
                "127.0.0.1 fake or monkeypatch the client.")
        return _real_getaddrinfo(host, *a, **k)

    def create_connection(address, *a, **k):
        _check_socket_address(socket.AF_INET, address, "socket.create_connection")
        return _real_create_connection(address, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex, raising=False)
    monkeypatch.setattr(socket.socket, "sendto", sendto, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo, raising=False)
    monkeypatch.setattr(socket, "create_connection", create_connection, raising=False)

    _real_loop_connect = asyncio.BaseEventLoop.create_connection
    _real_loop_dgram = asyncio.BaseEventLoop.create_datagram_endpoint

    async def loop_create_connection(self, *args, **kwargs):
        host = kwargs.get("host")
        if host is None and len(args) > 1:
            host = args[1]                      # open_connection passes it positionally
        port = kwargs.get("port")
        if port is None and len(args) > 2:
            port = args[2]
        sock = kwargs.get("sock")
        if sock is None and host is not None and not _is_loopback(host):
            raise NetworkBlocked(
                f"BLOCKED: a test opened an asyncio transport to {host!r}:{port}. "
                "On Windows this bypasses socket.connect entirely "
                "(asyncio/windows_events.py:596 ov.ConnectEx), so it is the one "
                "path a socket-method-only guard misses.")
        if sock is not None:
            try:
                peer = sock.getpeername()
            except OSError:
                peer = None
            if peer:
                _check_socket_address(sock.family, peer, "asyncio sock=")
        return await _real_loop_connect(self, *args, **kwargs)

    async def loop_create_datagram(self, *args, **kwargs):
        remote = kwargs.get("remote_addr")
        if remote and not _is_loopback(remote[0]):
            raise NetworkBlocked(
                f"BLOCKED: a test sent a datagram to {remote!r} — that is a DNS "
                "query off the machine.")
        return await _real_loop_dgram(self, *args, **kwargs)

    monkeypatch.setattr(asyncio.BaseEventLoop, "create_connection",
                        loop_create_connection, raising=False)
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_datagram_endpoint",
                        loop_create_datagram, raising=False)


# --------------------------------------------------------------------------
# 2. litter: nothing may be written outside tmp_path
# --------------------------------------------------------------------------

def _snapshot(root: Path) -> dict:
    """relpath -> (size, mtime_ns) for the watched working-tree paths.

    Deliberately not the whole tree.  ``beeagent/`` and ``server/`` hold source,
    source is edited by other processes while this suite runs, and a watch that
    fires on somebody else's commit is a watch that gets deleted.  What is
    watched is where the real incidents landed: the top level of the process cwd,
    where a tool with no ``chdir`` writes ``big.svg`` and ``nul``, plus the
    ``.beeagent/`` state directory, where the plugin ledger, the response cache
    and the measured-model caches live.
    """
    seen: dict[str, tuple[int, int]] = {}

    def record(rel: str) -> None:
        rel = rel.replace("\\", "/")
        if _NOISE_RE.match(rel):
            return
        try:
            st = os.stat(os.path.join(str(root), rel))
        except OSError:
            return
        seen[rel] = (st.st_size, st.st_mtime_ns)

    try:
        names = sorted(os.listdir(str(root)))
    except OSError:
        names = []
    for name in names:
        if name in _NOISY_DIRS:
            continue
        # Files only: a directory's own mtime moves whenever anyone touches a
        # file inside it, which would blame every source edit on the test that
        # happened to be running.  Watched directories are walked below.
        if os.path.isdir(os.path.join(str(root), name)):
            continue
        record(name)
    for state in (".beeagent",):
        base = Path(str(root)) / state
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(str(base)):
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            dirnames[:] = [d for d in dirnames if d not in _NOISY_DIRS]
            for name in filenames:
                record(f"{rel_dir}/{name}")
                if len(seen) > _MAX_ENTRIES:
                    return seen
    return seen


@pytest.fixture(autouse=True)
def _no_litter(request):
    """Fail a test that creates or rewrites a file in the working tree.

    Detection, not a blanket ``chdir``, on purpose: a default ``chdir`` would
    hide the bug instead of naming it, and half the tests here already pin their
    own ``monkeypatch.chdir`` because the tools use the process cwd.  Anything
    new is deleted after being reported so one litterer does not poison the next
    test's baseline.
    """
    before = _snapshot(INVOCATION_DIR)
    yield
    after = _snapshot(INVOCATION_DIR)
    added = sorted(set(after) - set(before))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    for rel in added:
        victim = INVOCATION_DIR / rel
        try:
            if victim.is_dir():
                shutil.rmtree(str(victim), ignore_errors=True)
            else:
                victim.unlink()
        except OSError:
            pass                                  # reported either way
    if added or changed:
        pytest.fail(
            f"{request.node.name} wrote to the working tree instead of tmp_path.\n"
            + "".join(f"  created  {INVOCATION_DIR / p}\n" for p in added)
            + "".join(f"  modified {INVOCATION_DIR / p}\n" for p in changed)
            + "The tools resolve against the process cwd, not the workdir passed "
              "around, so add monkeypatch.chdir(tmp_path) (or point the sink at "
              "tmp_path) to this test.",
            pytrace=False,
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Fail the test that left the process cwd somewhere else, and undo the damage.

    Not a fixture: `monkeypatch.chdir` is undone by `monkeypatch`'s own teardown,
    so a fixture either runs too early to see a real leak (it saw one, and
    blamed the wrong test) or too late to attribute it.  A wrapper hook fires
    after every finalizer, which is the only point where "the cwd is wrong" can
    only mean "a test broke it and nothing put it back".  It matters because the
    tools resolve against the process cwd: one leaked `os.chdir` re-homes every
    relative default for the rest of the session, which is an order dependence
    that points at nothing when it finally fails.
    """
    result = yield
    cwd = Path(os.getcwd())
    if cwd != INVOCATION_DIR:
        os.chdir(str(INVOCATION_DIR))
        _CWD_LEAKS.append((item.nodeid, str(cwd)))
        _report_teardown_failure(
            item,
            f"left the process cwd at {cwd} instead of {INVOCATION_DIR}. "
            "Use monkeypatch.chdir, which puts it back.")
    return result


def _report_teardown_failure(item, message: str) -> None:
    """Emit a real failing teardown report for *item* from inside a hook."""
    from _pytest.reports import TestReport

    report = TestReport(nodeid=item.nodeid, location=getattr(item, "location", None),
                        keywords=getattr(item, "keywords", {}), when="teardown",
                        outcome="failed", longrepr=message, duration=0.0)
    item.ihook.pytest_runtest_logreport(report=report)


# --------------------------------------------------------------------------
# 3. globals that outlive the test that set them
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_window_cache(tmp_path, monkeypatch):
    """Measured context windows live in a project-local cache.  Left alone, a value
    written by one test (or sitting in the working tree from a real measurement)
    would silently change what other tests believe a model's window is.
    """
    from beeagent.core import windows

    monkeypatch.setattr(windows, "CACHE", tmp_path / ".beeagent" / "windows.json")


@pytest.fixture(autouse=True)
def _isolated_language(monkeypatch):
    """``beeagent.i18n._lang`` is process-global and there is no setter-side undo.

    ``test_context.py`` and ``test_pool_client.py`` switch to Russian to assert a
    Russian string.  The language decides which half of every ``L(en, ru)`` call
    comes back, so a leaked "ru" silently changes the wording of assertions in
    whichever file happens to run next — order dependence with no order in it.
    """
    from beeagent import i18n

    monkeypatch.setattr(i18n, "_lang", "en", raising=False)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Redirect the home directory so no test can read or write a real profile.

    ``providers/pool.install_key`` (``beeagent/providers/pool.py:98``) falls back
    to ``~/.beecode/pool-key.json``, and that is the file the seat is signed
    with: ``test_pool_server.py`` calls ``enroll()`` without overriding it, so
    the suite minted and read the *developer's* signing key.  Only
    ``test_pool_client.py`` had the env guard.
    """
    home = tmp_path / "fake-home"
    (home / ".beecode").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("BEECODE_POOL_KEY_FILE", str(home / ".beecode" / "pool-key.json"))
    monkeypatch.setenv("BEECODE_TRUST_FILE", str(home / ".beecode" / "trusted.json"))
    # `trust.py` keeps three caches of its own.  `_STORE` is a *file handle plus a
    # parsed dict* made the first time anybody asks, so a redirect of HOME alone
    # leaves it pointing at the previous test's (already deleted) tmp directory:
    # one folder's "yes, trust it" quietly becomes the next folder's answer.
    # `_GATES` caches one ProjectTrust per folder key and `_SHIPPED` the digest
    # set.  All three have to come back to a cold start every test.
    try:
        from beeagent.core import trust as _trust
    except Exception:
        _trust = None
    if _trust is not None:
        for attribute in ("_STORE", "_SHIPPED"):
            if hasattr(_trust, attribute):
                monkeypatch.setattr(_trust, attribute, None, raising=False)
        if hasattr(_trust, "_GATES"):
            monkeypatch.setattr(_trust, "_GATES", {}, raising=False)


@pytest.fixture(autouse=True)
def _no_trust_gate_answer(monkeypatch):
    """Never let a test be the one that answers "trust this folder?".

    `ProjectTrust.announce()` reads stdin when the terminal can take an answer.
    Under pytest stdin is usually not a tty, so it stays quiet -- but a test that
    patches `isatty` for its own reasons can put a blocking `input()` in the
    middle of the suite, and the hang it produces names neither of them.
    """
    monkeypatch.setenv("BEECODE_TRUST_PROMPT", "0")


@pytest.fixture(autouse=True)
def _isolated_plugin_state(tmp_path, monkeypatch):
    """``manager.STATE_PATH`` is the relative ``.beeagent/plugins.json``.

    Relative means "the process cwd", i.e. the repository root, so a plugin
    enable/disable test rewrote the developer's installed-plugin ledger.
    """
    from beeagent.plugins import manager

    monkeypatch.setattr(manager, "STATE_PATH",
                        tmp_path / ".beeagent" / "plugins.json", raising=False)


@pytest.fixture(autouse=True)
def _isolated_economy_cache(tmp_path, monkeypatch):
    """``EconomyManager``/``ResponseCache`` default to the relative ``.beeagent/cache``.

    Only the bare default is redirected.  A test that names its own cache
    directory is exercising directory behaviour -- swallowing that argument
    turned "the folder is created when missing" into a test of the fixture.
    """
    from beeagent.core import economy
    from beeagent.utils import cache as cache_mod

    dest = str(tmp_path / ".beeagent" / "cache")
    monkeypatch.setattr(economy.EconomyManager, "__init__",
                        _default_to(economy.EconomyManager.__init__, 2, "cache_dir",
                                    ".beeagent/cache", dest), raising=False)
    monkeypatch.setattr(cache_mod.ResponseCache, "__init__",
                        _default_to(cache_mod.ResponseCache.__init__, 1, "cache_dir",
                                    ".beeagent/cache", dest), raising=False)


def _default_to(func, position, name, default_value, dest):
    """Substitute *dest* for *default_value* in one parameter, positionally or not."""
    def wrapper(*args, **kwargs):
        if name in kwargs:
            if kwargs[name] == default_value:
                kwargs[name] = dest
        elif len(args) > position and args[position] == default_value:
            args = args[:position] + (dest,) + args[position + 1:]
        elif len(args) <= position:
            kwargs[name] = dest
        return func(*args, **kwargs)
    wrapper.__doc__ = func.__doc__
    wrapper.__name__ = getattr(func, "__name__", "wrapper")
    return wrapper


@pytest.fixture(autouse=True)
def _no_orphaned_repl_task(request):
    """``repl._ACTIVE_TASK`` is a module global holding a live asyncio task.

    A task left behind outlives its event loop: the next test that runs
    ``asyncio.run`` inherits a reference it can only cancel into the wrong loop,
    and ``/stop`` in a later test would interrupt an earlier test's work.
    """
    yield
    try:
        from beeagent.ui import repl
    except Exception:
        return
    task = getattr(repl, "_ACTIVE_TASK", None)
    if task is not None:
        repl._ACTIVE_TASK = None
        if not task.done():
            task.cancel()
        pytest.fail(f"{request.node.name} left repl._ACTIVE_TASK populated: {task!r}",
                    pytrace=False)


# --------------------------------------------------------------------------
# 4. collection honesty
# --------------------------------------------------------------------------

def pytest_collection_modifyitems(session, config, items):
    """Warn if a test file exists that pytest never imported.

    There is no ``[tool.pytest.ini_options]`` anywhere in this repository, so
    collection runs on the built-in ``test_*.py`` pattern.  A file named
    ``checks_foo.py`` would be silently skipped forever; the precedent in this
    repo is an import sweep, so this is the collection-side twin of it.
    """
    collected = {Path(str(item.fspath)).name for item in items}
    stray = [p.name for p in sorted((REPO_ROOT / "tests").glob("*.py"))
             if p.name not in collected and p.name != "conftest.py"
             and not p.name.startswith("test_")]
    if stray:
        import warnings

        warnings.warn(f"tests/ files that match no collection pattern and were "
                      f"never imported: {stray}", RuntimeWarning, stacklevel=1)


# --------------------------------------------------------------------------
# helpers the tests import rather than re-implement
# --------------------------------------------------------------------------

@pytest.fixture
def forbid_sockets():
    """Hand a test the guard class so it can assert its own fake never dials."""
    return NetworkBlocked


@pytest.fixture
def tree_snapshot():
    """The working-tree inventory, for a test that wants to prove it changed nothing."""
    return lambda: _snapshot(INVOCATION_DIR)


if sys.platform == "win32":
    # Some tools print Russian and the console here is cp1251; a test that
    # captures child output must not die on an undecodable byte first.
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
