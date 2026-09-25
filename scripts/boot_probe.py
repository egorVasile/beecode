"""A single-boot job, run in a fresh interpreter, that prints one JSON line.

``tests/test_boot_budget.py`` spawns this as ``python -I tests/_boot_probe.py
<command>`` from a neutral working directory.  Three reasons it is a subprocess
and not a helper imported by the test:

* Import state has to be clean.  ``import beeagent`` the moment pytest collects
  another test file and every "is X imported at boot?" answer in this repo is a
  lie.  A fresh ``-I`` interpreter is the only honest measurement.
* ``-I`` matters.  Without it the current folder lands on ``sys.path`` and a
  checkout reports "installed" about its own source -- the mistake that broke
  ``beeagent/cli.update_self`` once.  Isolated mode drops the cwd *and*
  ``PYTHONPATH``, so the repo is reachable only because this file adds its own
  parent explicitly below, and the third-party deps come from real site-packages
  exactly as they do for a pip-installed user.
* One process per number, so the caller can take a median of independent cold
  starts.  A single number from a warm cache is a lie on a phone.

Stdlib only.  Every byte of the program under test that wants to talk is caught
by the guard installed by the ``network``/``agent``/``mcp``/``g4f`` commands --
a blocked socket is the point of those runs, and the recorder also logs subprocess
spawns so an MCP server that starts itself at boot cannot hide.
"""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# The real stdout, captured before any redirect.  ``_emit`` always writes here so
# the single JSON line is never interleaved with a print from the program under
# test (the trust announcement, a provider banner, g4f's own chatter).
_REAL_OUT = sys.__stdout__

# Heavy third-party modules the suite asserts stay off the boot path.
HEAVY = ("httpx", "rich", "textual", "prompt_toolkit", "g4f", "tiktoken")

# A non-routable address (TEST-NET-3) for the "unreachable provider" run.  With
# the guard installed it is refused at the socket layer in microseconds instead
# of hanging on a real TCP timeout, so the elapsed number is the retry policy's
# doing and not the network's.
UNREACHABLE_URL = "http://203.0.113.9:8000/v1"


def _emit(payload) -> None:
    """One JSON line on the real stdout; the program's own prints never touch it."""
    _REAL_OUT.write(json.dumps(payload) + "\n")
    _REAL_OUT.flush()


@contextlib.contextmanager
def quiet():
    """Swallow stdout written by the program under test, so _emit stays parseable."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


# --------------------------------------------------------------------------
# the guard: no route out, and no child process, leaves the probe unnoticed
# --------------------------------------------------------------------------

class Guarded(RuntimeError):
    """Raised instead of letting the boot reach a network or spawn a process.

    A ``RuntimeError``, not a bare ``BaseException``, on purpose: conftest's
    ``NetworkBlocked`` is one, and only by being an ``Exception`` does the guard
    travel the app's own ``except Exception`` retry path -- so the "unreachable
    provider" run measures BeeCode's backoff, not a crash the program never sees.
    """


def install_guard(log: list) -> None:
    """Record -- and refuse -- every egress a boot could attempt.

    Mirrors ``tests/conftest.py``'s five layers, plus subprocess spawns.  It is
    duplicated rather than imported because conftest's guard is a pytest fixture
    that monkeypatches and un-monkeypatches per test; a child interpreter has no
    fixture machinery, and the import-time boot this measures happens long before
    a fixture could be installed even if it could.
    """

    def is_loopback(host) -> bool:
        name = str(host or "").strip().strip("[]")
        if not name or name in ("localhost", "::1", "::", "127"):
            return True
        return name.startswith("127.") or name.startswith("::ffff:127.")

    def check(host, via, arg=None):
        if not is_loopback(host):
            log.append({"via": via, "target": arg if arg is not None else host})
            raise Guarded(f"{via}:{host}")

    real = {}

    def patch(obj, name, wrapper):
        real[name] = getattr(obj, name)
        setattr(obj, name, wrapper)

    def connect(self, address, *a, **k):
        check(address[0] if isinstance(address, tuple) else address,
              "socket.connect", address)
        return real["connect"](self, address, *a, **k)

    def getaddrinfo(host, *a, **k):
        check(host, "getaddrinfo")
        return real["getaddrinfo"](host, *a, **k)

    def create_connection(address, *a, **k):
        check(address[0] if isinstance(address, tuple) else address,
              "socket.create_connection", address)
        return real["create_connection"](address, *a, **k)

    def sendto(self, data, *rest, **k):
        address = rest[1] if len(rest) > 1 else k.get("address")
        if address is None and rest and isinstance(rest[0], tuple):
            address = rest[0]
        if address is not None:
            check(address[0] if isinstance(address, tuple) else address,
                  "socket.sendto", address)
        return real["sendto"](self, data, *rest, **k)

    patch(socket.socket, "connect", connect)
    patch(socket.socket, "connect_ex", connect)
    patch(socket, "getaddrinfo", getaddrinfo)
    patch(socket, "create_connection", create_connection)
    patch(socket.socket, "sendto", sendto)

    # The asyncio hole that matters on Windows (ProactorEventLoop calls ConnectEx
    # on the raw descriptor, bypassing socket.connect entirely) and the subprocess
    # hole that MCP servers would use to come alive at boot.
    import asyncio
    import subprocess

    async def loop_connect(self, *args, **kwargs):
        host = kwargs.get("host")
        if host is None and len(args) > 1:
            host = args[1]
        check(host, "asyncio.create_connection", host)
        return await real["loop_connect"](self, *args, **kwargs)

    def popen(self, args, *a, **k):
        log.append({"via": "subprocess.Popen", "target": str(args)[:120]})
        raise Guarded("subprocess.Popen")

    async def aio_exec(*args, **kwargs):
        log.append({"via": "asyncio.create_subprocess_exec",
                    "target": str(args)[:120]})
        raise Guarded("asyncio.create_subprocess_exec")

    real["loop_connect"] = asyncio.BaseEventLoop.create_connection
    asyncio.BaseEventLoop.create_connection = loop_connect
    real["popen"] = subprocess.Popen
    subprocess.Popen = popen
    real["aio_exec"] = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = aio_exec


def heavy_state() -> dict:
    """Which heavy modules are live in this interpreter, right now."""
    return {name: (name in sys.modules) for name in HEAVY}


# --------------------------------------------------------------------------
# isolated working directory: a temp folder the program believes is "the project"
# --------------------------------------------------------------------------

@contextlib.contextmanager
def sandbox(seeds: dict = None):
    """A temp cwd + a temp HOME, so boot cannot read or write a real profile.

    The Agent resolves plugin/skill/MCP/ledger state against the cwd and the
    home directory; both are redirected here so "constructing an Agent in a temp
    folder touches no socket" is tested against an empty folder, not against
    whatever the developer's ``.beeagent`` happens to hold.
    """
    work = tempfile.mkdtemp(prefix="bootwork-")
    home = tempfile.mkdtemp(prefix="boothome-")
    saved = {k: os.environ.get(k) for k in
             ("HOME", "USERPROFILE", "BEECODE_TRUST_PROMPT", "BEECODE_POOL_KEY_FILE",
              "BEECODE_TRUST_FILE", "PYTHONPATH")}
    os.environ["HOME"] = home
    os.environ["USERPROFILE"] = home
    os.environ["BEECODE_TRUST_PROMPT"] = "0"          # never block boot on a prompt
    os.environ["BEECODE_POOL_KEY_FILE"] = str(Path(home) / "pool-key.json")
    os.environ["BEECODE_TRUST_FILE"] = str(Path(home) / "trusted.json")
    cwd = os.getcwd()
    try:
        for name, text in (seeds or {}).items():
            path = Path(work) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        os.chdir(work)
        yield work
    finally:
        os.chdir(cwd)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def _isolated_economy_cache(work: str) -> None:
    """Point the economy cache and the measured-window cache at the temp folder.

    ``EconomyManager`` and ``ResponseCache`` default to a *relative* ``.beeagent/
    cache`` -- fine, because the cwd is the sandbox -- but ``core.windows`` keeps
    its own module-level ``CACHE`` path, and the usage ledger resolves ``PATH``
    against the cwd too.  Nothing here may write to the checkout.
    """
    from beeagent.core import windows
    windows.CACHE = Path(work) / ".beeagent" / "windows.json"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_baseline() -> None:
    """Reference cost of a trivial CPU loop, to detect a loaded machine.

    Two independent signals, because one is not enough and the failure mode of
    the obvious one is the whole reason this file runs in a subprocess:

    * ``baseline_ms`` -- the wall time of a small in-memory ``json.dumps`` loop.
      This is the ``conftest``-style check the task named, but it is nearly blind
      to the contention that actually wrecks a boot number here: a checkout being
      actively rewritten by other processes invalidates ``__pycache__`` and starves
      disk I/O, and a fixed loop of pure integers measures neither.
    * ``ratio`` -- wall-time divided by process-CPU-time over the same loop.  When
      the box is quiet this is ~1.0; when it is thrashing -- the four CPUs of a
      phone, or this developer machine with a dozen interpreters alive -- the
      process is descheduled and the ratio climbs.  It is the honest loaded switch.
    """
    import json as _json
    import time
    best_wall = None
    for _ in range(3):
        start = time.perf_counter()
        for _ in range(500):
            _json.dumps({"a": 1, "b": "x"})
        elapsed = (time.perf_counter() - start) * 1000
        best_wall = elapsed if best_wall is None else min(best_wall, elapsed)
    # A loop long enough (a few tenths of a second) that preemption is visible:
    # if the scheduler takes the process off a core, wall stretches while cpu does
    # not, and the ratio says so.  The json loop above is far too short for that.
    iterations = 1_500_000
    best_ratio = None
    wall = cpu = 0.0
    for _ in range(2):
        w0, c0 = time.perf_counter(), time.process_time()
        total = 0
        for i in range(iterations):
            total += i
        wall = (time.perf_counter() - w0) * 1000
        cpu = (time.process_time() - c0) * 1000
        ratio = wall / cpu if cpu else 0.0
        best_ratio = ratio if best_ratio is None else min(best_ratio, ratio)
    _emit({"baseline_ms": round(best_wall, 3),
           "wall_ms": round(wall, 1), "cpu_ms": round(cpu, 1),
           "ratio": round(best_ratio or 0.0, 2)})


def cmd_import(module: str) -> None:
    """Median-able single measurement: time importing one module from cold."""
    import importlib
    import time
    start = time.perf_counter()
    importlib.import_module(module)
    _emit({"module": module, "import_ms": round((time.perf_counter() - start) * 1000, 1)})


def cmd_heavies(module: str) -> None:
    """After importing *module*, which heavy third-party libs are live."""
    import importlib
    importlib.import_module(module)
    _emit({"module": module, "heavies": heavy_state()})


def cmd_first_importers(module: str) -> None:
    """For each heavy lib, the beeagent frame that first pulled it in."""
    import importlib
    import importlib.abc
    import traceback

    seen: dict[str, list] = {}

    class Hook(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            root = fullname.split(".")[0]
            if root in HEAVY and root not in seen:
                frames = [f for f in traceback.extract_stack()
                          if os.sep + "beeagent" + os.sep in f.filename
                          and (os.sep + "templates" + os.sep) not in f.filename]
                seen[root] = [f"{Path(f.filename).name}:{f.lineno}" for f in frames[-3:]]
            return None

    sys.meta_path.insert(0, Hook())
    importlib.import_module(module)
    _emit({"module": module, "importers": seen, "heavies": heavy_state()})


def cmd_agent() -> None:
    """Construct an Agent in a temp folder under the guard: time it, count egress."""
    log: list = []
    install_guard(log)
    import time
    with sandbox() as work, quiet():
        from beeagent.core.agent import Agent
        from beeagent.config.schema import BeeConfig
        _isolated_economy_cache(work)
        # Importing the classes is setup, not boot; only construction is timed.
        config = BeeConfig()
        start = time.perf_counter()
        agent = Agent(config=config, workdir=work)
        elapsed = (time.perf_counter() - start) * 1000
        data = {"construct_ms": round(elapsed, 1),
                "sockets": len(log), "egress": log,
                "pending_mcp": list(agent.plugins.pending_mcp),
                "load_errors": list(agent.plugins.load_errors),
                "heavies": heavy_state()}
    _emit(data)


def cmd_import_guarded(module: str) -> None:
    """Import *module* under the guard and prove nothing tried to leave."""
    log: list = []
    install_guard(log)
    import importlib
    with quiet():
        importlib.import_module(module)
        data = {"module": module, "egress": log, "heavies": heavy_state()}
    _emit(data)


def cmd_agent_network() -> None:
    """Alias kept for the no-network proof on the construction path."""
    cmd_agent()


def cmd_mcp_boot() -> None:
    """A project folder that configures an MCP server must not start it at boot.

    Two servers: one with a cached schema, one without.  The commands name a
    script that would hang forever if it were ever exec'd, so a spawn is both
    logged by the guard and would leave this run unfinished.  The Agent boots the
    gated way (folder untrusted -> servers withheld); a hand-built loader with the
    unenforced gate then stands in for a folder the user *did* trust, so the
    cached-registration path is exercised too.  Neither may spawn or connect.
    """
    hang = "import time\nwhile True: time.sleep(60)\n"
    mcp_json = json.dumps({"servers": {
        "cached": {"command": sys.executable, "args": ["-c", hang], "enabled": True},
        "fresh": {"command": sys.executable, "args": ["-c", hang], "enabled": True},
    }})
    cache_json = json.dumps({"cached": {"tools": [
        {"name": "echo", "description": "d", "inputSchema": {"type": "object"}}]}})
    seeds = {".beeagent/mcp.json": mcp_json, ".beeagent/mcp-cache.json": cache_json,
             "hang.py": hang}
    log: list = []
    install_guard(log)
    import time
    with sandbox(seeds) as work, quiet():
        from beeagent.core.agent import Agent
        from beeagent.config.schema import BeeConfig
        _isolated_economy_cache(work)
        config = BeeConfig()
        start = time.perf_counter()
        agent = Agent(config=config, workdir=work)
        gated_ms = (time.perf_counter() - start) * 1000
        gated_egress = len(log)

        # Same folder, but vouched for: the loader now takes the register-from-
        # cache path instead of withholding, and still must not spawn a process.
        from beeagent.plugins.loader import PluginLoader
        log.clear()                             # count only what the trusted path does
        loader = PluginLoader(agent, gate_project=False)
        loader.load_all()
        registered = [n for n in agent.tools.list_names() if n.startswith("mcp_")]
        data = {"gated_construct_ms": round(gated_ms, 1),
                "gated_egress": gated_egress,
                "trusted_egress": len(log), "trusted_detail": list(log),
                "registered_mcp_tools": registered,
                "pending_mcp": list(loader.pending_mcp)}
        loader.shutdown()
    _emit(data)


def cmd_g4f_discovery() -> None:
    """``discover_models`` is offline by design: it reads the installed package.

    g4f itself is imported *before* the guard goes on: its import is a known
    several-second subprocess, and that cost belongs to the lazy g4f provider, not
    to the discovery call being tested here.  What this measures is whether
    BeeCode's own discovery reaches a socket once g4f is present.
    """
    with quiet():
        try:
            import g4f.Provider  # noqa: F401
            g4f_present = True
        except Exception:
            g4f_present = False
    log: list = []
    install_guard(log)
    with quiet():
        from beeagent.providers.g4f_provider import G4fProvider
        try:
            models = G4fProvider.discover_models()
            failure = ""
        except BaseException as exc:
            models = []
            failure = f"{type(exc).__name__}: {exc}"
        data = {"g4f_present": g4f_present, "g4f_imported": "g4f" in sys.modules,
                "model_count": len(models), "sockets": len(log), "egress": log,
                "failure": failure}
    _emit(data)


def cmd_tokens_lazy() -> None:
    """The token helper must not load a tokenizer until it actually counts."""
    log: list = []
    install_guard(log)
    import importlib
    with quiet():
        importlib.import_module("beeagent.utils.tokens")
        importlib.import_module("beeagent.core.usage")
        after_import = "tiktoken" in sys.modules
        from beeagent.utils.tokens import count_tokens, rough_count
        sample = "hello world, counting some tokens"
        n = count_tokens(sample, "gpt-4")
        after_call = "tiktoken" in sys.modules
        data = {"tiktoken_after_import": after_import, "tiktoken_after_count": after_call,
                "count": n, "rough": rough_count(sample),
                "sockets": len(log), "egress": log}
    _emit(data)


def cmd_cli_help() -> None:
    """The full ``beecode --help`` path: interpreter already up, so time from the
    import of the entry module through argparse printing help and exiting."""
    import time
    with quiet() as buffer:
        start = time.perf_counter()
        from beeagent.cli import main
        import_ms = (time.perf_counter() - start) * 1000
        sys.argv = ["beecode", "--help"]
        code = 0
        try:
            main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
        total_ms = (time.perf_counter() - start) * 1000
    _emit({"import_ms": round(import_ms, 1), "total_ms": round(total_ms, 1),
           "exit_code": code, "help_bytes": len(buffer.getvalue())})


def cmd_oneshot() -> None:
    """A one-shot run against an unreachable provider: bounded, never hanging.

    The provider points at a non-routable address; the guard refuses the connect
    instantly, so whatever elapsed beyond a few hundred ms is the agent's own
    retry backoff (`core/agent.py`, `_stream_response`), not the network.
    """
    log: list = []
    install_guard(log)
    import asyncio
    config_text = json.dumps({
        # The class name it registers under, not the custom entry's label:
        # Agent() builds OpenAICompatProvider(base_url=...) and that class keeps
        # ``name = "openai_compat"``, so ``select()`` must ask for that.
        "provider": "openai_compat",
        "custom_providers": [
            {"name": "openai_compat", "type": "openai_compat", "url": UNREACHABLE_URL,
             "model": "x", "key": "k"}],
        "stream_idle_timeout": 3,
    })
    with sandbox({"beeagent.json": config_text}) as work, quiet():
        from beeagent.core.agent import Agent
        from beeagent.config.loader import load_config
        _isolated_economy_cache(work)
        config = load_config(work)
        agent = Agent(config=config, workdir=work)
        import time
        start = time.perf_counter()
        try:
            result = asyncio.run(agent.run("hi"))
            failure = ""
        except BaseException as exc:            # a failure here is the point
            result = ""
            failure = f"{type(exc).__name__}: {exc}"
        elapsed = (time.perf_counter() - start) * 1000
        data = {"run_ms": round(elapsed, 1), "result": (result or "")[:200],
                "failure": failure[:200], "sockets": len(log), "egress": log[:8]}
    _emit(data)


COMMANDS = {
    "baseline": lambda: cmd_baseline(),
    "import": cmd_import,
    "heavies": cmd_heavies,
    "first-importers": cmd_first_importers,
    "agent": lambda: cmd_agent(),
    "import-guarded": cmd_import_guarded,
    "mcp-boot": lambda: cmd_mcp_boot(),
    "g4f-discovery": lambda: cmd_g4f_discovery(),
    "tokens-lazy": lambda: cmd_tokens_lazy(),
    "cli-help": lambda: cmd_cli_help(),
    "oneshot": lambda: cmd_oneshot(),
}


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(REPO_ROOT))
    if not argv or argv[0] not in COMMANDS:
        _emit({"error": f"unknown command {argv[:1]!r}; want one of "
                        f"{sorted(COMMANDS)}"})
        return 2
    try:
        COMMANDS[argv[0]](*argv[1:])
    except BaseException as exc:                    # report, never leak a traceback
        _emit({"error": f"{type(exc).__name__}: {exc}", "command": argv[0]})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
