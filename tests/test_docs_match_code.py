"""The documentation is a claim about the software, so it gets tested like one.

Every test here compares a fact README.md (or docs/) states against the code that
has to back it: the command registry, the tool registry, the config schema, the
three files that carry the version, the image paths, the ignore rules the tool
itself depends on. A test fails when the prose and the program part company,
which is how a public README ends up promising a command that does not exist —
or shipping a key shape in its own text.

Nothing here reaches the network or a provider.
"""
import io
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
COMMANDS_BEGIN = "<!-- COMMANDS:BEGIN -->"
COMMANDS_END = "<!-- COMMANDS:END -->"

# Commands that belong to a catalog plugin rather than to the built-in registry.
# README has to say which one it is talking about; anything outside this list
# that README writes as `/name` must exist in beeagent/ui/commands.py.
PLUGIN_OWNED_COMMANDS = {"doctor"}


@pytest.fixture(scope="module")
def readme() -> str:
    return io.open(README, encoding="utf-8").read()


@pytest.fixture(scope="module")
def command_block(readme) -> str:
    assert COMMANDS_BEGIN in readme and COMMANDS_END in readme, (
        "README lost the markers scripts/sync_readme.py writes its command table "
        "between — regenerate it instead of hand-editing the block")
    return readme.split(COMMANDS_BEGIN, 1)[1].split(COMMANDS_END, 1)[0]


@pytest.fixture()
def live_agent(tmp_path, monkeypatch):
    """An Agent built in a folder with no extensions installed.

    `Agent.__init__` scans `.beeagent/` in the current directory, so without the
    chdir this suite would read whichever MCP servers and plugin packs happen to
    sit in the developer's own tree and fail differently on every machine.
    """
    monkeypatch.chdir(tmp_path)
    from beeagent.config.schema import BeeConfig
    from beeagent.core.agent import Agent

    return Agent(config=BeeConfig())


# ---------------------------------------------------------------- commands ---

def _table_rows(block: str):
    """`(/name, bare name, what it does, usage)` for each documented command."""
    out = []
    for line in block.splitlines():
        m = re.match(r"^\| `(/([a-z_]+))` \| (.*?) \| (.*?) \|$", line)
        if m:
            out.append((m.group(1), m.group(2), m.group(3), m.group(4).strip("`")))
    return out


def test_every_readme_command_exists_in_the_registry(command_block):
    from beeagent.ui.commands import COMMANDS

    registry = {c.name for c in COMMANDS}
    documented = {name for _, name, _, _ in _table_rows(command_block)}
    assert documented, "no command rows parsed — the table shape changed"
    missing = sorted(documented - registry)
    assert not missing, (
        f"README documents commands the registry does not have: {missing}. "
        "Either the command was removed or renamed; run scripts/sync_readme.py.")


def test_the_table_holds_every_registered_command(command_block):
    from beeagent.ui.commands import COMMANDS

    documented = {name for _, name, _, _ in _table_rows(command_block)}
    undocumented = sorted({c.name for c in COMMANDS} - documented)
    assert not undocumented, (
        f"commands the machine has and the page does not: {undocumented}. "
        "Run scripts/sync_readme.py.")


def test_documented_descriptions_and_usage_are_what_the_registry_says(command_block):
    from beeagent.ui.commands import COMMANDS

    by_name = {c.name: c for c in COMMANDS}
    wrong = []
    for _, name, description, usage in _table_rows(command_block):
        command = by_name.get(name)
        if command is None:
            continue                        # reported by the previous test
        expected_usage = command.usage or f"/{command.name}"
        if description != command.description:
            wrong.append(f"/{name}: README says {description!r}, "
                         f"the registry says {command.description!r}")
        if usage != expected_usage:
            wrong.append(f"/{name}: README shows usage {usage!r}, "
                         f"the registry declares {expected_usage!r}")
    assert not wrong, "the command table drifted from the registry:\n" + "\n".join(wrong)


def test_readme_never_prompts_a_command_that_is_not_registered(readme, command_block):
    """A `/something` in prose is a promise the reader will type."""
    from beeagent.ui.commands import COMMANDS

    registry = {c.name for c in COMMANDS}
    mentioned = set(re.findall(r"`(/[A-Za-z][A-Za-z0-9_-]*)`", readme))
    unknown = sorted({m.lstrip("/") for m in mentioned
                      if m.lstrip("/") not in registry
                      and m.lstrip("/") not in PLUGIN_OWNED_COMMANDS})
    assert not unknown, (
        f"README writes {unknown} as a slash command; none is in the registry and "
        "none is listed as plugin-owned. Add it to PLUGIN_OWNED_COMMANDS only if a "
        "catalog plugin really registers it.")


# ------------------------------------------------------------------- tools ---

def _tools_table(readme) -> set:
    section = readme.split("## Tools", 1)[1]
    section = section.split("\n## ", 1)[0]
    return {m.group(1) for m in re.finditer(r"^\| `([a-z_]+)` \|", section, re.M)}


def test_every_tool_in_the_readme_table_is_registered(readme, live_agent):
    registered = set(live_agent.tools.list_names())
    claimed = _tools_table(readme)
    assert claimed, "no tool rows parsed — the Tools table shape changed"
    ghost = sorted(claimed - registered)
    assert not ghost, f"README advertises tools the Agent never registers: {ghost}"


def test_every_core_tool_a_user_can_hit_is_in_the_readme_table(readme, live_agent):
    """The reverse direction, extension tools excepted.

    README's own words: `/tools` shows the live list "including tools added by
    plugins and MCP servers" — those are the user's, not the documentation's, so
    only the built-in set has to appear in the table.
    """
    core = [t.name for t in live_agent.tools.list_tools()
            if not getattr(t, "from_extension", False)]
    undocumented = sorted(set(core) - _tools_table(readme))
    assert not undocumented, (
        f"built-in tools with no README row: {undocumented}")


def test_the_invented_tool_names_readme_lists_are_real_aliases(readme, live_agent):
    """"Mistakes are recovered" promises specific aliases; the registry has them.

    Aliases are the one part of the tool surface README spells out by name, and a
    rename in `list_dir.py` would turn that paragraph into a story about a tool
    that no longer forgives anything.
    """
    claim = re.search(r"\* (`[^*]+`) — invented names for", readme)
    assert claim, "README no longer lists its invented tool names where this test looks"
    documented = {n.strip().strip("`") for n in claim.group(1).split(",")}
    accepted = set(live_agent.tools._aliases)
    stale = sorted(documented - accepted)
    assert not stale, (
        f"README teaches the model {stale}, which the registry does not accept; "
        f"the live aliases are {sorted(accepted)}")
    silent = sorted(accepted - documented)
    assert not silent, (
        f"the registry accepts {silent} and README never says so — a user reading "
        "the page cannot know the recovery exists")


# ----------------------------------------------------------------- version ---

def _read(path: Path) -> str:
    return io.open(path, encoding="utf-8").read()


def _permission_rows(readme):
    section = readme.split("## Permissions", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        m = re.match(r"^\| `(ask|auto|readonly)`[^|]*\| (.*?) \|", line)
        if m:
            rows[m.group(1)] = m.group(2)
    return rows


def test_the_ask_mode_row_is_the_set_that_actually_runs_without_asking(readme, live_agent):
    """README once listed `web_search` here; `is_safe()` has always refused it.

    The grant gate is the one table on the page where a wrong name costs the
    user a file or a query leaving the machine, so it is compared tool by tool
    against `Permissions.allows()` rather than trusted to a copywriter.
    """
    from beeagent.core.permissions import Permissions

    rows = _permission_rows(readme)
    assert "ask" in rows, "README's Permissions table no longer has an `ask` row"
    documented = {n for n in re.findall(r"`([a-z_]+)`", rows["ask"])}
    permissions = Permissions(mode="ask", allowed=[])
    allowed = {t.name for t in live_agent.tools.list_tools()
               if not getattr(t, "from_extension", False) and permissions.allows(t)}
    assert documented == allowed, (
        f"README says these run without asking: {sorted(documented)}; "
        f"the gate answers: {sorted(allowed)}. "
        f"claimed and not allowed: {sorted(documented - allowed)}, "
        f"allowed and unlisted: {sorted(allowed - documented)}")


def test_readonly_still_refuses_the_tools_that_write_their_own_file(readme, live_agent):
    """`todo` and `diagram` are safe to look with and still change the disk."""
    from beeagent.core.permissions import Permissions

    readonly = Permissions(mode="readonly", allowed=[])
    granted_first = Permissions(mode="readonly", allowed=["todo", "diagram", "write"])
    writers = [t for t in live_agent.tools.list_tools()
               if not getattr(t, "from_extension", False)
               and getattr(t, "writes_files", False)]
    assert writers, "no tool claims to write files any more; delete this test on purpose"
    for tool in writers:
        assert not readonly.allows(tool), f"{tool.name} runs in readonly"
        assert not granted_first.allows(tool), (
            f"{tool.name} runs in readonly after a grant — readonly is meant to be a "
            "ceiling, and README tells the user so")
    row = _permission_rows(readme).get("readonly", "")
    for tool in writers:
        assert f"`{tool.name}`" in row, (
            f"{tool.name} writes and readonly refuses it; README's readonly row says nothing")
    """`beeagent/__init__.py`, `pyproject.toml`, `package.json` — one number."""
    python = re.search(r'__version__\s*=\s*"([^"]+)"', _read(ROOT / "beeagent" / "__init__.py"))
    pyproject = re.search(r'^version\s*=\s*"([^"]+)"', _read(ROOT / "pyproject.toml"), re.M)
    package = re.search(r'"version"\s*:\s*"([^"]+)"', _read(ROOT / "package.json"))
    assert python and pyproject and package, (
        "a version file stopped declaring its version in the shape this test reads")
    found = {"beeagent/__init__.py": python.group(1),
             "pyproject.toml": pyproject.group(1),
             "package.json": package.group(1)}
    assert len(set(found.values())) == 1, f"the version files disagree: {found}"


def test_readme_carries_no_version_of_its_own(readme):
    """A version typed into the prose is a fourth file to keep in sync."""
    hits = re.findall(r"BeeCode\s+v?\d+\.\d+(?:\.\d+)?", readme)
    assert not hits, f"README hard-codes a release number: {hits}"


# ----------------------------------------------------------------- secrets ---

# A prefix printed on its own — `sk-or-v1-…` in prose about how a provider names
# its keys — is documentation. A prefix with a key's worth of characters behind
# it is a leak, so the length is the test.
SECRET_PATTERNS = (
    (r"crk_live_[A-Za-z0-9_\-]{16,}", "a crax key"),
    (r"\bgsk_[A-Za-z0-9_\-]{16,}", "a Groq key"),
    (r"\bsk-[A-Za-z0-9_\-]{16,}", "an OpenAI-shaped key"),
    (r"\bbearer\s+[A-Za-z0-9._\-]{16,}", "a Bearer token"),
    (r"\b[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b", "a JWT"),
)


@pytest.mark.parametrize("relative", ["README.md", "docs/MODELS.md", "beeagent.example.json"])
def test_no_shipped_document_holds_a_token(relative):
    """The way a public repository leaks is a real key pasted into its prose.

    Placeholder forms (`<key>`, `<token>`) pass; anything shaped like a issued
    secret does not, and this has to stay that way even for an example.
    """
    path = ROOT / relative
    assert path.exists(), f"{relative} vanished; the docs test has to be told about it"
    text = _read(path)
    offenders = []
    for pattern, label in SECRET_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE if "bearer" in pattern else 0):
            line = text[:match.start()].count("\n") + 1
            offenders.append(f"{relative}:{line} looks like {label}: {match.group(0)[:24]!r}")
    assert not offenders, "a secret-shaped string is in a published file:\n" + "\n".join(offenders)


# ------------------------------------------------------------------- paths ---

def test_every_local_path_readme_points_at_exists(readme):
    """Screenshots and links: a broken image is a blank hole in the README."""
    local = set(re.findall(r'src="([^"]+)"', readme)) | set(re.findall(r"\]\(([^)]+)\)", readme))
    broken = []
    for target in sorted(local):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        if not (ROOT / target).exists():
            broken.append(target)
    assert not broken, f"README references files that are not in the repository: {broken}"


def test_readme_anchors_resolve_to_a_heading(readme):
    headings = set()
    for line in readme.splitlines():
        if line.startswith("#"):
            slug = re.sub(r"[^a-z0-9 -]", "", line.lstrip("#").strip().lower())
            headings.add(slug.replace(" ", "-"))
    dangling = [t for t in re.findall(r"\]\(#([^)]+)\)", readme) if t not in headings]
    assert not dangling, f"README links to sections that have no heading: {dangling}"


# ------------------------------------------------------- config and hygiene ---

def test_every_config_field_has_a_readme_row(readme):
    """`beeagent.json` is what a user reads the README for, key by key."""
    from beeagent.config.schema import BeeConfig

    section = readme.split("## Configuration", 1)[1].split("\n## ", 1)[0]
    missing = [name for name in BeeConfig.model_fields if f"`{name}" not in section]
    assert not missing, (
        f"BeeConfig has fields README does not document: {missing}. "
        "A field that writes itself into a user's beeagent.json is documented or "
        "it is a secret nobody agreed to put in a repository.")


def test_gitignore_hides_everything_a_run_leaves_behind():
    """BeeCode writes into the folder it runs from; the ignore list is the proof.

    Checked as text rather than by asking git, so the failure names the missing
    line instead of an exit code.
    """
    lines = {line.strip() for line in _read(ROOT / ".gitignore").splitlines()
             if line.strip() and not line.startswith("#")}
    needed = {
        "beeagent.json": "the seat token and every key /key stored go here",
        ".beeagent/": "sessions, todos, cached answers, measured windows",
        "nul": "`> nul` from a shell redirect writes a file named after a device",
        "*.beecode-tmp": "the side file every guarded write passes through",
        "*.svg": "the `diagram` tool draws into the working directory",
    }
    absent = [f"{pattern} ({why})" for pattern, why in needed.items() if pattern not in lines]
    assert not absent, ".gitignore no longer hides: " + "; ".join(absent)


def test_example_config_holds_no_live_values():
    """`beeagent.example.json` is the file a user copies; it must stay inert."""
    data = json.loads(_read(ROOT / "beeagent.example.json"))
    for key, value in _flatten(data):
        text = str(value)
        if not text:
            continue
        if key.endswith("key") or key.endswith("token"):
            assert not re.search(r"[A-Za-z0-9_\-]{12,}", text), (
                f"{key} in the example config holds {text[:12]!r}… — an example that "
                "looks real is copied as-is")


def _flatten(node, prefix=""):
    """Every leaf of a JSON object as (dotted key, value)."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _flatten(v, f"{prefix}{k}.")
    elif isinstance(node, list):
        for v in node:
            yield from _flatten(v, prefix)
    else:
        yield prefix.rstrip("."), node
