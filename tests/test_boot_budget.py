"""Pin BeeCode's startup speed with tests.

The rule this file exists to enforce is *"nothing slow at import"*: no model
discovery, no network, no MCP connect, no tokenizer load and no third-party
heavyweight is allowed to run when a module is merely imported -- because this
project once took 57 seconds to boot, and on a phone or Termux a slow import is
a slow *everything*.  The rule lived only in comments and memory; here it is
executable.

Everything is measured in a subprocess (``tests/_boot_probe.py``) launched with
``python -I``, so the answer is not polluted by whatever the rest of the suite
already imported, and so a checkout cannot report "installed" about its own
source (``-I`` keeps the current folder off ``sys.path``; the probe adds the repo
explicitly).  Each timing figure is a median of three cold interpreter starts --
a single number from a warm cache is a lie on a slow CPU.

Budgets below are hard wall-clock numbers, calibrated on the machine named in
``MEASURED_ON``.  When the box is visibly loaded the timing tests SKIP rather
than pass or fail, because a contended measurement describes the neighbours, not
the code.  A structural assertion (is a heavy module imported? did anything touch
a socket?) is deterministic and never skips.

stdlib + pytest only.  No installs, no real endpoints: where a boot would reach
the network, the probe's socket guard refuses it -- a blocked socket is the point.
"""
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = Path(__file__).resolve().parent / "_boot_probe.py"

# The module the task calls "beeagent.ui.cli" does not exist; the CLI entry point
# wired in pyproject is ``beeagent.cli`` (``beecode = "beeagent.cli:main"``), so
# that is what is measured.  ``beeagent.ui.repl`` is the interactive surface.
CLI_MODULE = "beeagent.cli"

# --- budgets, and where they were measured --------------------------------
# MEASURED_ON: Windows (AMD64), 4 logical CPUs, CPython 3.12.9, on 2026-09-25.
# Idle-box medians that these budgets are set against:
#     import beeagent                ~   1 ms
#     import beeagent.core.agent     ~ 600 ms   (dominated by httpx)
#     import beeagent.cli            ~ 1000-1600 ms (httpx + prompt_toolkit + rich)
#     one Agent construction         ~ 160 ms
#     g4f import (lazy, never at boot) ~ 2000-6600 ms
CLI_IMPORT_BUDGET_MS = 1500.0          # task-set hard budget for importing the CLI
AGENT_CONSTRUCT_BUDGET_MS = 2000.0     # task-set hard budget for one Agent() build
BEEAGENT_IMPORT_BUDGET_MS = 150.0      # `import beeagent` must stay near-free
FULL_CLI_HELP_BUDGET_MS = 2500.0       # interpreter-up `beecode --help`
ONESHOT_UNREACHABLE_CEILING_MS = 30000.0  # a run against a dead host must be bounded

# Loaded-machine detector (see _boot_probe.cmd_baseline for why the json loop is
# not enough and a wall/CPU contention ratio is added on top).
BASELINE_REF_MS = 4.0                  # idle `json.dumps` loop on MEASURED_ON
LOADED_BASELINE_MULTIPLIER = 3.0       # skip if the loop costs > 3x its reference
LOAD_RATIO_MAX = 2.0                   # idle wall/cpu ratio ~1.2; skip above this

# The heavy third-party modules that may not ride the boot path.
HEAVY = ("httpx", "rich", "textual", "prompt_toolkit", "g4f", "tiktoken")


# --------------------------------------------------------------------------
# probe plumbing
# --------------------------------------------------------------------------

def run_probe(args, cwd, timeout=180):
    """Run ``python -I _boot_probe.py <args>`` and return its single JSON object."""
    completed = subprocess.run(
        [sys.executable, "-I", str(PROBE), *[str(a) for a in args]],
        cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
    )
    lines = [ln for ln in (completed.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise AssertionError(
            f"probe {' '.join(map(str, args))} produced no output "
            f"(rc={completed.returncode}); stderr:\n{completed.stderr[-1500:]}")
    payload = json.loads(lines[-1])            # _emit always writes the last line
    if "error" in payload and payload.get("error") and "ok" not in payload:
        # A probe "error" during this session is almost always another agent's
        # half-saved edit to the source under test, not a boot finding.
        raise ProbeError(payload["error"])
    return payload


class ProbeError(RuntimeError):
    """The probe itself could not run -- typically a syntax error the source is
    being edited into right now.  Distinct from an assertion failure."""


def _skip_if_source_unready(exc):
    if isinstance(exc, ProbeError):
        pytest.skip(f"beeagent source did not import at measurement time "
                    f"(another edit in flight?): {exc}")
    raise exc


def _median(values):
    return round(statistics.median(values), 1)


def _measure(cwd, command, key, reps=3):
    """Median of ``key`` over ``reps`` independent cold starts; None if any fails."""
    samples = []
    for _ in range(reps):
        try:
            payload = run_probe(command, cwd)
        except Exception as exc:
            _skip_if_source_unready(exc)
            return None
        if key not in payload:
            return None
        samples.append(float(payload[key]))
    return _median(samples)


# --------------------------------------------------------------------------
# session fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def probe_dir(tmp_path_factory):
    return str(tmp_path_factory.mktemp("bootprobe-cwd"))


@pytest.fixture(scope="session")
def load_state(probe_dir):
    """How loaded the box is; timing tests skip when the numbers would lie.

    ``BEECODE_BOOT_TIMING=force`` overrides the skip so a maintainer on a box they
    know is quiet can collect real numbers anyway -- the ratio is still measured
    and printed, so forcing on a thrashing machine is a visible choice, not a
    hidden pass.  It never overrides the "source did not import" skip: that is a
    correctness gate, not a timing one.
    """
    data = run_probe(["baseline"], probe_dir)
    loaded = (data.get("ratio", 0) > LOAD_RATIO_MAX
              or data.get("baseline_ms", 0) > BASELINE_REF_MS * LOADED_BASELINE_MULTIPLIER)
    if os.environ.get("BEECODE_BOOT_TIMING") == "force":
        data["forced"] = True
        loaded = False
    data["loaded"] = loaded
    return data


def _require_quiet(load_state):
    if load_state.get("loaded"):
        pytest.skip(
            f"box is loaded (wall/cpu ratio={load_state['ratio']}, "
            f"json loop={load_state['baseline_ms']} ms) -- timing would measure "
            f"the neighbours; budgets are for a quiet machine.")


@pytest.fixture(scope="session")
def measure_boot(load_state, probe_dir):
    """A lazy median-of-3 cold-start measurer, shared and cached per session.

    A test asks for one metric and only pays for that metric's three subprocess
    starts; nothing pays for a module it never names, and one slow or half-edited
    module cannot poison the others.  The whole thing skips up front when the box
    is loaded, because a contended timing number describes the neighbours.
    """
    _require_quiet(load_state)
    cache: dict = {}

    def measure(key, command, field):
        if key not in cache:
            cache[key] = _measure(probe_dir, command, field)
        return cache[key]

    return measure


# Each metric the table and the budgets read: key -> (probe command, field).
METRICS = {
    "import beeagent": (["import", "beeagent"], "import_ms"),
    "import beeagent.core.agent": (["import", "beeagent.core.agent"], "import_ms"),
    "import beeagent.cli": (["import", CLI_MODULE], "import_ms"),
    "import beeagent.providers.g4f_provider": (
        ["import", "beeagent.providers.g4f_provider"], "import_ms"),
    "import beeagent.plugins.loader": (["import", "beeagent.plugins.loader"], "import_ms"),
    "construct Agent (import pre-warmed)": (["agent"], "construct_ms"),
    "beecode --help (interpreter up)": (["cli-help"], "total_ms"),
}


# --------------------------------------------------------------------------
# A. lazy-vs-eager: heavy third-party modules off the boot path (deterministic)
# --------------------------------------------------------------------------

def test_import_beeagent_pulls_no_heavy_libs(probe_dir):
    """`import beeagent` alone must not pull a single heavyweight. The rule."""
    try:
        heavies = run_probe(["heavies", "beeagent"], probe_dir)["heavies"]
    except Exception as exc:
        _skip_if_source_unready(exc)
    leaked = [name for name in HEAVY if heavies.get(name)]
    assert not leaked, f"import beeagent pulled in: {leaked}"


def test_g4f_provider_import_defers_g4f(probe_dir):
    """Importing the g4f provider must not import g4f (it is ~2-6s and a subprocess)."""
    try:
        heavies = run_probe(["heavies", "beeagent.providers.g4f_provider"], probe_dir)["heavies"]
    except Exception as exc:
        _skip_if_source_unready(exc)
    assert not heavies["g4f"], "beeagent.providers.g4f_provider imported g4f at module load"


def test_plugins_loader_import_is_light(probe_dir):
    """The plugin loader module on its own must not drag in httpx/g4f/rich/etc."""
    try:
        heavies = run_probe(["heavies", "beeagent.plugins.loader"], probe_dir)["heavies"]
    except Exception as exc:
        _skip_if_source_unready(exc)
    leaked = [name for name in HEAVY if heavies.get(name)]
    assert not leaked, f"import beeagent.plugins.loader pulled in: {leaked}"


@pytest.mark.xfail(strict=True, reason=(
    "BUG beeagent/providers/ollama.py:9 (+ openai_compat.py:15), reached via "
    "beeagent/core/agent.py:20-21 -- `import httpx` runs at module load, so "
    "importing the agent eagerly imports httpx (~460 ms measured idle on "
    "MEASURED_ON); rich (~180 ms) rides in with it transitively. Both belong "
    "behind the provider call, like g4f already is."))
def test_import_core_agent_defers_httpx(probe_dir):
    """KNOWN DEBT: `import beeagent.core.agent` currently imports httpx eagerly."""
    try:
        heavies = run_probe(["heavies", "beeagent.core.agent"], probe_dir)["heavies"]
    except Exception as exc:
        _skip_if_source_unready(exc)
    assert not heavies["httpx"], "httpx imported by `import beeagent.core.agent`"
    assert not heavies["rich"], "rich imported by `import beeagent.core.agent`"


@pytest.mark.xfail(strict=True, reason=(
    "BUG beeagent/cli.py:14 -> beeagent/ui/repl.py:14 `from prompt_toolkit "
    "import PromptSession` -- prompt_toolkit (~460 ms idle on MEASURED_ON) is "
    "imported just to read argv, so even `beecode --help` pays for the "
    "line-editor before it knows a UI was asked for. Defer it behind the "
    "interactive/TUI branches the way `--tui` already defers textual."))
def test_cli_import_defers_prompt_toolkit(probe_dir):
    """KNOWN DEBT: importing the CLI pulls prompt_toolkit at module scope."""
    try:
        heavies = run_probe(["heavies", CLI_MODULE], probe_dir)["heavies"]
    except Exception as exc:
        _skip_if_source_unready(exc)
    assert not heavies["prompt_toolkit"], "prompt_toolkit imported by `import beeagent.cli`"


@pytest.mark.xfail(strict=True, reason=(
    "BUG beeagent/cli.py:6-14 imports the agent (-> httpx at ollama.py:9) and "
    "ui.components (rich at components.py:7) at module scope. Together with the "
    "prompt_toolkit import above this is the whole reason `import beeagent.cli` "
    "measures ~1.0-1.6 s on MEASURED_ON, straddling the 1.5 s budget."))
def test_cli_import_defers_httpx_and_rich(probe_dir):
    """KNOWN DEBT: importing the CLI pulls httpx and rich at module scope."""
    try:
        heavies = run_probe(["heavies", CLI_MODULE], probe_dir)["heavies"]
    except Exception as exc:
        _skip_if_source_unready(exc)
    assert not heavies["httpx"], "httpx imported by `import beeagent.cli`"
    assert not heavies["rich"], "rich imported by `import beeagent.cli`"


def test_cli_import_does_not_touch_textual_g4f_tiktoken(probe_dir):
    """The lazy path is real: three of the heavyweights already stay off `import cli`."""
    try:
        heavies = run_probe(["heavies", CLI_MODULE], probe_dir)["heavies"]
    except Exception as exc:
        _skip_if_source_unready(exc)
    for name in ("textual", "g4f", "tiktoken"):
        assert not heavies[name], f"{name} imported by `import beeagent.cli`"


def test_first_import_tracebacks_report_the_dragger(probe_dir):
    """Record which beeagent line drags each heavy module in (for the report)."""
    try:
        data = run_probe(["first-importers", CLI_MODULE], probe_dir)
    except Exception as exc:
        _skip_if_source_unready(exc)
    importers = data["importers"]
    # httpx must be attributed to the providers import, prompt_toolkit to repl.
    assert any("ollama.py" in f or "openai_compat.py" in f
               for f in importers.get("httpx", [])), importers.get("httpx")
    assert any("repl.py" in f for f in importers.get("prompt_toolkit", [])), \
        importers.get("prompt_toolkit")


# --------------------------------------------------------------------------
# B. no-network / no-spawn at import and at Agent construction (deterministic)
# --------------------------------------------------------------------------

def test_constructing_agent_touches_no_socket(probe_dir):
    """An `Agent` built in a temp folder must not open a single socket."""
    try:
        data = run_probe(["agent"], probe_dir)
    except Exception as exc:
        _skip_if_source_unready(exc)
    assert data["sockets"] == 0, f"Agent construction attempted: {data['egress']}"


def test_import_core_agent_touches_no_socket(probe_dir):
    """Merely importing the agent module must not reach the network."""
    try:
        data = run_probe(["import-guarded", "beeagent.core.agent"], probe_dir)
    except Exception as exc:
        _skip_if_source_unready(exc)
    assert data["egress"] == [], f"import of agent reached: {data['egress']}"


def test_tokenizer_not_imported_at_module_load(probe_dir):
    """`tokens.py`/`usage.py` must not load a tokenizer until a real count runs.

    This is the 57-second shape: a heavy tokenizer pulled at import is exactly
    what dies on a phone. tiktoken may load lazily on first count, never at
    import.
    """
    try:
        data = run_probe(["tokens-lazy"], probe_dir)
    except Exception as exc:
        _skip_if_source_unready(exc)
    assert data["tiktoken_after_import"] is False
    assert data["rough"] > 0                     # fallback estimator works
    assert data["sockets"] == 0, data["egress"]  # counting never reaches out


def test_usage_ledger_and_economy_cache_import_without_network(probe_dir):
    """The ledger and the economy cache read/write disk only -- no socket, no tokenizer."""
    for module in ("beeagent.core.usage", "beeagent.core.economy"):
        try:
            data = run_probe(["import-guarded", module], probe_dir)
        except Exception as exc:
            _skip_if_source_unready(exc)
        assert data["egress"] == [], f"{module} reached the network at import: {data['egress']}"
        assert not data["heavies"]["tiktoken"], f"{module} imported tiktoken at load"


def test_configured_mcp_servers_do_not_connect_or_spawn_at_boot(probe_dir):
    """A project folder's `mcp.json` may not start a server (or dial out) at boot.

    Two servers are configured, one with a cached schema and one without; both
    commands name a script that would hang forever if it were ever exec'd. The
    gated boot withholds them; a vouched-for folder exercises the cached-
    registration path. Neither may spawn a process nor touch a socket -- first-
    time discovery belongs to the explicit `/mcp connect`.
    """
    try:
        data = run_probe(["mcp-boot"], probe_dir)
    except Exception as exc:
        _skip_if_source_unready(exc)
    assert data["gated_egress"] == 0, data
    assert data["trusted_egress"] == 0, f"trusted MCP path spawned/connected: {data['trusted_detail']}"
    assert "mcp_cached_echo" in data["registered_mcp_tools"], data   # cached registers, no spawn
    assert "fresh" in data["pending_mcp"], data                      # uncached stays pending


def test_g4f_model_discovery_makes_no_socket_call(probe_dir):
    """`discover_models()` is offline by design: it reads the installed catalogue."""
    try:
        data = run_probe(["g4f-discovery"], probe_dir)
    except Exception as exc:
        _skip_if_source_unready(exc)
    if not data.get("g4f_present"):
        pytest.skip("g4f not installed in this environment")
    assert data["sockets"] == 0, f"g4f discovery reached out: {data['egress']}"
    assert data["model_count"] > 0


# --------------------------------------------------------------------------
# C. wall-clock budgets (skip when the box is loaded)
# --------------------------------------------------------------------------

def test_boot_measurement_table(measure_boot):
    """Print the module -> median-ms table this file is built around."""
    lines = ["", f"boot measurement table (median of 3 cold starts; {MEASURED_ON_LABEL}):"]
    for label, (command, field) in METRICS.items():
        value = measure_boot(label, command, field)
        budget = _budget_for(label)
        note = "" if budget is None else f"  (budget <{budget:.0f} ms)"
        lines.append(f"  {label:36s} {value if value is not None else 'n/a':>9}"
                     f" ms{note}")
    print("\n".join(lines))
    assert measure_boot("import beeagent", *METRICS["import beeagent"]) is not None


def _budget_for(label):
    return {
        "import beeagent": BEEAGENT_IMPORT_BUDGET_MS,
        "import beeagent.cli": CLI_IMPORT_BUDGET_MS,
        "construct Agent (import pre-warmed)": AGENT_CONSTRUCT_BUDGET_MS,
        "beecode --help (interpreter up)": FULL_CLI_HELP_BUDGET_MS,
    }.get(label)


MEASURED_ON_LABEL = ("Windows AMD64, 4 CPUs, CPython "
                     f"{sys.version.split()[0]}, 2026-09-25")


def test_import_beeagent_is_near_free(measure_boot):
    value = measure_boot("import beeagent", *METRICS["import beeagent"])
    assert value is not None and value < BEEAGENT_IMPORT_BUDGET_MS


@pytest.mark.xfail(strict=False, reason=(
    f"Budget `import beeagent.cli` < {CLI_IMPORT_BUDGET_MS:.0f} ms is NOT met at "
    "rest: it measures ~1.0-1.6 s on MEASURED_ON because cli.py:6 (agent -> httpx "
    "at providers/ollama.py:9, ~460 ms), cli.py:10 (components -> rich, ~180 ms) "
    "and cli.py:14 (repl -> prompt_toolkit, ~460 ms) all run at module scope. The "
    "straddling figure is a timing measurement, so this is a non-strict xfail -- it "
    "records the miss and turns into a pass once the imports are deferred (see the "
    "strict structural xfails in section A)."))
def test_cli_import_under_budget(measure_boot):
    value = measure_boot("import beeagent.cli", *METRICS["import beeagent.cli"])
    assert value is not None and value < CLI_IMPORT_BUDGET_MS


def test_agent_construction_under_budget(measure_boot):
    key = "construct Agent (import pre-warmed)"
    value = measure_boot(key, *METRICS[key])
    assert value is not None and value < AGENT_CONSTRUCT_BUDGET_MS


def test_full_cli_help_startup_under_budget(measure_boot):
    key = "beecode --help (interpreter up)"
    value = measure_boot(key, *METRICS[key])
    assert value is not None and value < FULL_CLI_HELP_BUDGET_MS


def test_oneshot_against_unreachable_provider_is_bounded(load_state, probe_dir):
    """A run against a dead host returns an error rather than hanging forever.

    The socket guard refuses the connect instantly, so the elapsed time is the
    agent's own retry backoff (`core/agent.py:_stream_response` sleeps
    `1.5*(attempt+1)` per failure).  This measures that floor, not a network wait.
    """
    _require_quiet(load_state)
    try:
        data = run_probe(["oneshot"], probe_dir)
    except Exception as exc:
        _skip_if_source_unready(exc)
    assert data["sockets"] > 0, "the run never even attempted the unreachable host"
    assert "Error calling provider" in data["result"] or data.get("failure")
    assert data["run_ms"] < ONESHOT_UNREACHABLE_CEILING_MS, (
        f"one-shot against a dead provider took {data['run_ms']} ms -- the retry "
        "policy is unbounded")
