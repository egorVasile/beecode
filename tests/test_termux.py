"""BeeCode on a phone.

Termux has no C compiler and no Rust. That rules out less than it looks: `g4f` is
pure Python and so are the tools that matter here — what a phone cannot build are
two packages `g4f` merely declares (`pycryptodome`, `brotli`), neither of which
BeeCode imports, and `tiktoken`, which is Rust. So `pip install beecode` on
Android leaves both out, and that is a deal, not a loss: the keyless answers and
the exact token count go away, and everything the agent actually *does* — read,
write, edit, grep, run a command, talk to a model through a pool or a key — has to
keep working. The keyless half is still reachable, with the recipe
`bin/beecode.js` and the README both give: g4f `--no-deps`, plus aiohttp from the
sdist under `AIOHTTP_NO_EXTENSIONS=1`, which compiles nothing.

The loop tests below run with those two modules missing, because that is what a
phone looks like to the import machinery when the recipe has not been run. The
rest read the shipped install instructions, so the advice cannot drift back to
telling a person with a phone to install a compiler.
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.utils.tokens import count_tokens

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "bin" / "beecode.js"
DOCTOR = ROOT / "beeagent" / "plugins" / "templates" / "plugins" / "doctor" / "plugin.py"
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"


@pytest.fixture()
def phone(monkeypatch, tmp_path):
    """An environment where the extension-built packages are not installed.

    `None` in sys.modules makes `import g4f` raise ImportError, which is the
    same answer pip gives when it never installed the package.
    """
    for name in ("g4f", "g4f.Provider", "tiktoken"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class Scripted:
    """A native endpoint that replays a prepared conversation."""

    name = "scripted"
    supports_tools = True

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen = []

    async def complete(self, messages, model="", tools=None):
        self.seen.append({"messages": messages, "tools": tools})
        return dict(self.steps.pop(0))


def _agent(phone, steps, provider="scripted"):
    # provider names the scripted endpoint: with g4f missing, the agent would
    # otherwise hand the turn to the pool, which is a different test.
    config = BeeConfig(model="m", provider=provider)
    agent = Agent(config=config, workdir=str(phone))
    endpoint = Scripted(steps)
    agent.providers.register(endpoint)
    agent.providers.select = lambda name: endpoint
    agent.permissions.mode = "auto"
    return agent, endpoint


def test_the_agent_starts_without_the_packages_android_cannot_build(phone):
    agent = Agent(config=BeeConfig(), workdir=str(phone))

    assert agent.tools.list_names(), "the tools are the product"
    for name in ("read", "write", "edit", "bash", "grep", "glob", "list_directory", "git"):
        assert agent.tools.get(name) is not None, name
    assert "g4f" in agent.providers.list_names(), \
        "the slot stays advertised; it says what is wrong when it is used"


def test_a_turn_that_writes_a_file_and_reports_it_needs_neither_g4f_nor_tiktoken(phone):
    agent, endpoint = _agent(phone, [
        {"text": "", "tool_calls": [{"tool": "write",
                                     "args": {"path": "hello.py",
                                              "content": "print('пчела')\n"}}]},
        {"text": "файл записан", "tool_calls": []},
    ])

    answer = asyncio.run(agent.run("создай hello.py", session=Session()))

    assert answer == "файл записан"
    assert (phone / "hello.py").read_text(encoding="utf-8") == "print('пчела')\n"
    assert endpoint.steps == [], "both turns came from the scripted endpoint"


def test_the_shell_tool_runs_a_real_command(phone):
    agent, _ = _agent(phone, [
        {"text": "", "tool_calls": [{"tool": "bash", "args": {"command": "echo пчела"}}]},
        {"text": "готово", "tool_calls": []},
    ])
    events = []

    answer = asyncio.run(agent.run("скажи пчела", session=Session(),
                                   callback=lambda e, d: events.append((e, d))))

    assert answer == "готово"
    ended = [d for e, d in events if e == "tool_end"]
    assert ended and not ended[0]["error"], ended
    assert "пчела" in str(ended[0]["output"])


def test_token_budgeting_survives_without_tiktoken(phone):
    """Counting goes approximate, which is the safe direction: an over-count
    trims the history early instead of sending a request the model will drop."""
    assert count_tokens("привет, пчела") > 0
    assert count_tokens("") == 0


def test_a_phone_without_g4f_is_answered_by_the_pool(tmp_path, monkeypatch):
    """The default provider is a compiled package Android cannot install. Left
    alone, a phone's first question would read as a broken agent; the pool is the
    other half of the same promise, so the turn goes there — and says so."""
    from beeagent.providers.pool import PoolProvider

    agent = Agent(config=BeeConfig(provider="g4f"), workdir=str(tmp_path))
    pool = agent.providers.get("pool")
    pool.url, pool.token = "https://pool.invalid", "a-seat"
    monkeypatch.setattr(Agent, "_g4f_installed", staticmethod(lambda: False))
    events = []

    provider = agent._provider_or_pool(lambda e, d: events.append((e, d)))

    assert provider.name == "pool"
    assert agent.config.provider == "pool", "the switch is real, not cosmetic"
    assert ("provider_fallback", {"from": "g4f", "to": "pool", "seat": True}) in events


def test_a_desktop_with_g4f_installed_is_never_moved_off_it(monkeypatch):
    """The fallback is for the machine that cannot have g4f, not for a provider
    the user chose away from."""
    agent = Agent(config=BeeConfig(provider="g4f"), workdir=".")
    monkeypatch.setattr(Agent, "_g4f_installed", staticmethod(lambda: True))
    events = []

    provider = agent._provider_or_pool(lambda e, d: events.append(e))

    assert provider.name == "g4f"
    assert events == []


# ---------------------------------------------------------------- the install

STEP = re.compile(r"\[([^\]]*)\]")
QUOTED = re.compile(r'"([^"]*)"')


def block(text, name):
    """The body of a `const NAME = [...]` literal from the bootstrap."""
    match = re.search(rf"const {name} = \[(.*?)\n\];", text, re.S)
    assert match, f"bin/beecode.js has no const {name}"
    return match.group(1)


def keyless_recipe():
    """The pip steps the bootstrap runs on Termux, in the order it runs them."""
    steps = [[m for m in QUOTED.findall(arg) if m][1:]
             for arg in STEP.findall(block(BOOTSTRAP.read_text(encoding="utf-8"), "KEYLESS_PIP"))]
    assert all(steps), "a step with no argument is not a step"
    return steps


BOUNDARY = re.compile(r"(?:\n[ \t]*\n)|(?<=[.!?])\s+")


def sentences(text):
    """Rough sentences — a full stop or a blank line ends one. Enough to ask
    whether a shipped page still claims the thing the measurement disproved."""
    return BOUNDARY.split(re.sub(r"[`*_]+", "", text))


def termux_section(text):
    """The README's phone chapter — everything from `### On a phone` to the next
    heading, so the desktop install above it cannot stand in for it."""
    tail = text.split("### On a phone", 1)[1]
    return tail.split("\n## ", 1)[0]


def test_the_bootstrap_adds_the_keyless_provider_with_two_flags_that_do_the_work():
    """The measurement that made this recipe: g4f imports with every compiled
    module refused, so the only things to keep out are the two declarations that
    cannot build — `--no-deps` does that — and the one wheel that arrives with a
    C parser inside, which `--no-binary` plus the env var puts back on the pure
    Python path. Both belong to the aiohttp step: the variable is read only when
    pip builds from the sdist."""
    recipe = keyless_recipe()

    assert any("--no-deps" in step and "g4f" in step for step in recipe), recipe
    assert any("--no-binary=aiohttp" in step and "aiohttp" in step for step in recipe), recipe
    assert any("requests" in step and "nest-asyncio2" in step for step in recipe), recipe
    assert not any(any(pkg in arg for pkg in ("pycryptodome", "brotli", "pydantic"))
                   for step in recipe for arg in step), \
        "the two that need a compiler stay out, and g4f does not declare pydantic"

    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "AIOHTTP_NO_EXTENSIONS" in text, "without it the sdist build compiles, too"
    for step in recipe:
        assert "pip" not in step and "--upgrade" not in step, "one arg list per pip call"


def test_the_bootstrap_runs_the_recipe_instead_of_only_printing_it():
    """The npm launcher already runs every other install, so a phone user should
    not have to type the recipe. Offering it from `install()` is what makes the
    first run on Termux the same shape as on a desktop: one command, then it
    starts."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    body = re.search(r"function addKeylessProvider\([^{]*\{(.*?)\n\}", text, re.S)
    assert body, "no function runs the recipe"
    source = " ".join(body.group(1).split())
    assert "KEYLESS_PIP" in source and '"-m", "pip"' in source, \
        "addKeylessProvider must hand KEYLESS_PIP to pip"
    assert "KEYLESS_ENV" in source, "the recipe needs the env var to mean anything"
    installer = re.search(r"function install\([^)]*\) \{(.*?)\n\}", text, re.S)
    assert "offerKeylessProvider" in installer.group(1), "install has to offer it"
    assert "--keyless" in text, "and it is there to re-run on purpose"


def test_the_recipe_reaches_the_user_even_when_pip_gives_up():
    """A phone that cannot fetch the sdist still has to be told what to type. The
    lines printed are the recipe itself, so they cannot drift from it."""
    text = BOOTSTRAP.read_text(encoding="utf-8")

    assert "export AIOHTTP_NO_EXTENSIONS=1" in text
    printed = [line for line in text.splitlines() if "console.error" in line]
    assert any("AIOHTTP_NO_EXTENSIONS" in line for line in printed), \
        "the env var belongs in what the user sees, not only in pip's environment"


def test_the_desktop_path_does_not_learn_the_phone_recipe():
    """Every flag in the recipe exists because of a missing Android wheel. On a
    machine that can take g4f from the manifest they are wrong: `--no-deps` would
    leave out requests and aiohttp, which is not a compiler problem but a broken
    provider."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    offers = [line.strip() for line in text.splitlines() if "offerKeylessProvider" in line
              and not line.strip().startswith("//")]

    assert len(offers) == 3, offers       # defined, called from install, called from --keyless
    for line in offers:
        if line.startswith("function"):
            continue
        assert "TERMUX" in line or line.startswith("offerKeylessProvider"), \
            f"{line} reaches a phone recipe on any platform"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_keyless_is_the_launchers_flag_and_a_desktop_is_not_sent_the_recipe(tmp_path):
    """`--keyless` is bin/beecode.js's own flag, not BeeCode's: it has to be
    swallowed, and a machine that gets g4f from the manifest must be told so
    instead of being walked through a phone recipe that would leave its own
    dependencies out. Both paths have to hold without touching the network."""
    node = shutil.which("node")
    checked = subprocess.run([node, "--check", str(BOOTSTRAP)],
                             capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr

    home = tmp_path / "home"
    env = dict(os.environ, BEECODE_HOME=str(home))
    said = subprocess.run([node, str(BOOTSTRAP), "--keyless"], capture_output=True,
                          text=True, env=env, timeout=180)
    assert said.returncode == 0, said.stderr
    assert "with the install itself" in said.stdout, said.stdout
    assert not home.exists(), "--keyless must not build a venv on a desktop"

    blind = subprocess.run([node, str(BOOTSTRAP), "--keyless"], capture_output=True,
                           text=True, env=dict(env, PATH=""), timeout=180)
    assert blind.returncode == 1, blind.stdout + blind.stderr
    assert "Python 3.10" in blind.stderr, \
        "a machine with no python has to hear it as a python problem, not a crash"


# ------------------------------------------------------------- stale advice

def test_no_install_instruction_asks_for_a_compiler():
    """No shipped instruction may tell a phone user to install a compiler for g4f.
    The advice was wrong, and a page that still gives it sends a person with a
    phone to a failed build of a package BeeCode never imports."""
    for path in (BOOTSTRAP, DOCTOR, README, PYPROJECT):
        lowered = path.read_text(encoding="utf-8").lower()
        assert not re.search(r"(pkg|apt|apt-get|brew)\s+install\s+(python3-)?clang", lowered), path
        assert "install clang" not in lowered and "clang++" not in lowered, path


def test_the_keyless_recipe_is_the_same_one_in_the_bootstrap_and_the_readme():
    """The README is what a person reads; the bootstrap is what runs. If the two
    stop agreeing, the printed advice is the stale half and nobody notices."""
    readme = README.read_text(encoding="utf-8")
    section = termux_section(readme)

    assert "--no-deps g4f" in section, "the README must keep telling pip to skip g4f's own deps"
    assert "--no-binary=aiohttp" in section and "AIOHTTP_NO_EXTENSIONS" in section, section
    assert "requests" in section and "nest-asyncio2" in section, section

    recipe = keyless_recipe()
    for arg in ("--no-deps", "--no-binary=aiohttp", "requests", "nest-asyncio2"):
        assert any(arg in step for step in recipe), f"{arg} is advice only"


def test_g4f_is_never_called_a_compiled_or_uninstallable_package():
    """What a phone cannot build is what g4f *declares*, not g4f: it ships a
    `py3-none-any` wheel, and pydantic is not among its dependencies at all. A
    sentence saying g4f needs a compiler is the mistake these tests exist to
    catch, so every one of them has to say plainly that it does not."""
    files = [BOOTSTRAP, DOCTOR, README, PYPROJECT, Path(__file__)]
    for path in files:
        for sentence in sentences(path.read_text(encoding="utf-8")):
            flat = " ".join(sentence.split()).lower()
            about_g4f = "g4f" in flat or "the keyless provider" in flat
            talks_about_building = any(word in flat for word in
                                       ("compil", "clang", "rustc", "wheel", "build"))
            if about_g4f and talks_about_building:
                assert re.search(r"\bno\b|\bnot\b|\bnone\b|\bnothing\b|\bnever\b|neither"
                                 r"|without|cannot|narrower|exception|refus|only|least|less",
                                 flat), f"{path}: {flat}"


def test_no_shipped_instruction_says_g4f_needs_pydantic():
    """g4f does not declare pydantic at all: its stubs guard it with a
    pure-Python BaseModel. So a page that pairs the two names is telling a phone
    user to chase a Rust extension for nothing, and BeeCode's own config never
    imported it either."""
    for path in (BOOTSTRAP, DOCTOR, README, PYPROJECT):
        for sentence in sentences(path.read_text(encoding="utf-8")):
            flat = " ".join(sentence.split()).lower()
            assert not ("g4f" in flat and "pydantic" in flat), f"{path}: {flat}"
