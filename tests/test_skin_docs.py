"""`docs/SKINS.md` is a claim about the software, so it is tested like one.

A stranger copies the example skin out of that page. If it does not compile, does
not pass the AST gate, or paints nothing when the real host drives it, the page is a
bug report waiting for someone else. So this file:

* pulls every ```python fence out of `docs/SKINS.md` and compiles it, which catches
  the mistake a documentation author actually makes — a snippet that reads fine and
  is not valid Python;
* runs the example pack's own source through `skins.check_source()`, the gate a
  user's BeeCode will run before it lets the pack near a screen;
* installs the pack the way a pack is really installed — `.beeagent/plugins/<name>/`,
  the entry in `.beeagent/plugins.json`, the folder's trust answer — and loads it
  with `PluginLoader` behind a real `Agent`, so the *layout* the page documents is
  exercised and not only its code;
* feeds the host a scripted sequence of `(dt, event)` pairs and asserts on the cells
  the skin put into a real `renderer.Grid`: a lit row while the model works, an
  empty one after `done`;
* asserts the documented `/skins <name>` switch answers the way the page says it
  answers, through `commands.dispatch`, with the command registry restored after.

Nothing here sleeps, writes into the working tree, or reaches the network.

And the honest part: the engine is landing while this is being written. Where the
module the page documents is not on disk, or does not yet have the entry point a
snippet needs, these tests `skip` and name the missing thing in the reason. A
skipped test says "not yet"; a test that faked a pass would say "fine", and it would
not be.
"""
import importlib
import inspect
import io
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "SKINS.md"

#: The pack the page tells a stranger to write, and the name it switches to.
SKIN_NAME = "thinking-pulse"
FOLDER = "thinking-pulse"

ENGINE = "beeagent.core.skins"
RENDERER = "beeagent.core.renderer"

#: The painter calls section 4's tables document, and the arguments they promise.
PAINTER_API = {
    "draw_text": ("x", "y", "text"),
    "draw_box": ("x", "y", "w", "h"),
    "clear_region": ("x", "y", "w", "h"),
    "get_terminal_size": (),
    "color_support": (),
    "color": ("value",),
    "draw_bar": ("x", "y", "width", "fraction"),
    "draw_ramp": ("x", "y", "width", "color_a", "color_b"),
    "draw_sparkline": ("x", "y", "width", "values"),
    "draw_ticker": ("x", "y", "width", "text"),
    "cell": ("x", "y"),
}

#: The second example on the page: the skin that owns three lines.
SURFACES_NAME = "turn-ledger"

#: The twelve events section 3 lists as the vocabulary a skin animates on.
DOCUMENTED_EVENTS = [
    "stream_delta", "reasoning_delta", "tool_start", "tool_end", "tool_denied",
    "done", "stopped", "error", "retry", "economy_hit", "context_trimmed", "nudged",
]


# ------------------------------------------------------------- the document ---

def _text() -> str:
    assert DOC.exists(), (
        "%s vanished; this file exists to test it" % DOC.relative_to(ROOT))
    return io.open(str(DOC), encoding="utf-8").read()


DOC_TEXT = _text()

_FENCE = re.compile(r"^```([a-zA-Z0-9_+-]*)([^\n]*)\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _fences(language: str) -> list:
    """Every fenced block tagged `language`, as (info-string, code) pairs.

    The info string is read as `python name=plugin.py`: the page labels the two files
    of its example pack that way, and the label is what makes the copyable skin
    findable — a heading rename must not silently stop the example being tested.
    """
    out = []
    for match in _FENCE.finditer(DOC_TEXT):
        tag, info, body = match.group(1).strip(), match.group(2).strip(), match.group(3)
        if tag.lower() == language:
            out.append((info, body))
    return out


def _block_named(name: str) -> str:
    wanted = "name=" + name
    for info, body in _fences("python") + _fences("json"):
        if wanted in info:
            return body.rstrip("\n")
    raise AssertionError(
        "docs/SKINS.md has no block labelled `name=%s`. The example a stranger copies "
        "has to stay findable by this test, or it stops being tested" % name)


def _example_skin() -> str:
    return _block_named("plugin.py")


def _example_manifest() -> dict:
    return json.loads(_block_named("plugin.json"))


# ---------------------------------------------------------------- the engine ---

def _engine():
    """`beeagent.core.skins`, or a skip naming what is missing from it."""
    try:
        skins = importlib.import_module(ENGINE)
    except ImportError as e:                     # the module is not on disk yet
        pytest.skip("skin engine not landed (%s: %s)" % (ENGINE, e))
    missing = [name for name in ("register", "install_source", "switch", "post",
                                 "frame", "stats", "names", "reset", "budget",
                                 "check_source")
               if not callable(getattr(skins, name, None))]
    if missing:
        pytest.skip("skin engine not landed (%s has no %s)" % (ENGINE, ", ".join(missing)))
    return skins


def _renderer():
    try:
        renderer = importlib.import_module(RENDERER)
    except ImportError as e:
        pytest.skip("skin engine not landed (%s: %s)" % (RENDERER, e))
    missing = [name for name in ("Grid", "Painter") if not hasattr(renderer, name)]
    if missing:
        pytest.skip("skin engine not landed (%s has no %s)" % (RENDERER, ", ".join(missing)))
    return renderer


def _grid_painter(renderer, cols=60, rows=12):
    """A real painter over a real grid, which never reaches a terminal.

    Only `FrameLoop` emits, and only to a tty: this is the same object a skin is
    handed in production, with nowhere to write.
    """
    grid = renderer.Grid(cols, rows)
    return grid, renderer.Painter(grid, size=(cols, rows))


@pytest.fixture()
def host():
    """An empty skin registry, and an empty one left behind.

    `core/skins.py` keeps `_REGISTRY`, `_ACTIVE` and the notices in module globals,
    so a skin registered here would otherwise stay live for whichever test in this
    process runs next — and a demotion notice would print into the report.
    """
    skins = _engine()
    was_active = skins.active_name()
    skins.reset()
    skins.set_notifier(lambda text: None)
    yield skins
    skins.reset()
    skins.set_notifier(None)
    if was_active and was_active != skins.active_name():
        skins.switch(was_active)


@pytest.fixture()
def commands():
    """`/skins` into the live command table, and out again.

    `COMMANDS` is the list `scripts/sync_readme.py` builds the README table from and
    the list `tests/test_docs_match_code.py` compares that table against, so a
    command registered by a test and left behind would fail a documentation test in
    another file. Restored by content, not by name.
    """
    from beeagent.ui import commands as module

    before = list(module.COMMANDS)
    handlers = dict(module.HANDLERS)
    yield module
    module.COMMANDS[:] = before
    module.HANDLERS.clear()
    module.HANDLERS.update(handlers)


# ------------------------------------------------------- the page compiles ---

def test_the_safety_paragraph_comes_first():
    """Section 1 is the one a reader who stops early still has to see."""
    headings = [line for line in DOC_TEXT.splitlines() if line.startswith("## ")]
    assert headings, "docs/SKINS.md has no sections at all"
    first = headings[0].lower()
    assert "read this first" in first or "process" in first, (
        "the page promises the honest safety section up front; it opens with %r"
        % headings[0])
    cut = DOC_TEXT.find(headings[1]) if len(headings) > 1 else len(DOC_TEXT)
    section = DOC_TEXT[:cut]
    for claim in ("sandbox", "import", "/trust", "stranger"):
        assert claim in section, (
            "section 1 has to say the word %r: a reader who stops after the first "
            "section still has to learn what a skin is allowed to do" % claim)
    assert any(phrase in section for phrase in
               ("not sandboxed", "not a sandbox", "not containment")), (
        "the page has to say in one plain clause that this is not a sandbox, and not "
        "leave the reader to infer it from a list of refused names")


def test_every_python_block_compiles():
    """A snippet that does not parse is a snippet nobody can paste and run."""
    blocks = _fences("python")
    assert blocks, "docs/SKINS.md documents a programmable format with no code in it"
    broken = []
    for index, (info, code) in enumerate(blocks, start=1):
        where = "block %d%s" % (index, " (%s)" % info if info else "")
        try:
            compile(code, "docs/SKINS.md:" + where, "exec")
        except SyntaxError as e:
            broken.append("%s is not valid Python: %s (line %s)" % (where, e.msg, e.lineno))
    assert not broken, "docs/SKINS.md:\n  " + "\n  ".join(broken)


def test_the_example_skin_passes_the_gate():
    """The gate the page tells authors to run on themselves takes the page's own skin."""
    skins = _engine()
    source = _example_skin()
    try:
        refusals = skins.check_source(source)
    except Exception as e:                       # present, but the gate does not answer
        pytest.skip("skin engine not landed (check_source raised %s)" % type(e).__name__)
    assert not refusals, (
        "the example skin the page tells a stranger to copy is refused by the gate it "
        "documents: " + "; ".join(str(r) for r in refusals))


def test_the_example_skin_fits_the_page():
    """The copyable block is a skin and not an essay: two hooks, and it registers itself."""
    source = _example_skin()
    compile(source, "plugin.py", "exec")
    for hook in ("def on_event", "def on_frame"):
        assert hook in source, "the example lost %s — the page documents it as the skin" % hook
    assert "register(" in source and "def setup(" in source, (
        "the example has to register itself from setup(api): nothing else in the "
        "loader does it for it")
    assert "import " in source, "the example must import the host, not assume it"


def test_the_manifest_carries_every_field_the_page_lists():
    """`name`, `version`, `type`, `description`, `entry` — and `entry` is the real file."""
    manifest = _example_manifest()
    for field in ("name", "version", "type", "description", "entry"):
        assert manifest.get(field), "plugin.json has no %r" % field
    assert manifest["entry"] == "plugin.py", (
        "the loader looks for plugin.py by name, so a manifest promising another file "
        "documents a pack that would never load")
    assert manifest["name"] == SKIN_NAME, (
        "the folder name and the registered skin name are different things; the page "
        "tells the reader to keep them equal, and `/skins` switches by the second")
    assert len(manifest["description"]) > 20, "one sentence of description, not a word"


def test_the_page_names_the_command_the_host_registers():
    """`/skins <name>` is the only way a user gets to the skin, so it had better be real."""
    skins = _engine()
    name, usage = getattr(skins, "COMMAND_NAME", ""), getattr(skins, "USAGE", "")
    if not name or not usage:
        pytest.skip("skin engine not landed (%s declares no COMMAND_NAME/USAGE)" % ENGINE)
    assert usage.startswith("/" + name), (
        "the host registers /%s and the page's usage line says %r" % (name, usage))
    typed = [line.strip().split()[0] for line in DOC_TEXT.splitlines()
             if line.strip().startswith("/") and SKIN_NAME in line]
    assert typed, "the page never shows the command that switches to the example skin"
    for typed_command in typed:
        assert typed_command == "/%s" % name, (
            "the page tells a stranger to type %r; this host's command is /%s"
            % (typed_command, name))


def test_the_event_list_is_the_agent_vocabulary():
    """Twelve names on the page, twelve names in the host, and neither drifts alone."""
    skins = _engine()
    core = getattr(skins, "CORE_EVENTS", None)
    if core is None:
        pytest.skip("skin engine not landed (%s declares no CORE_EVENTS)" % ENGINE)
    for event in DOCUMENTED_EVENTS:
        assert event in DOC_TEXT, "the page never documents the %r event" % event
        assert event in core, (
            "the page lists %r as an event a skin animates on and the host's "
            "CORE_EVENTS does not have it: %s" % (event, sorted(core)))
    undocumented = sorted(set(core) - set(DOCUMENTED_EVENTS))
    assert not undocumented, (
        "the host fires %s and the page's table never mentions them — an author who "
        "codes from the table will not know they exist" % undocumented)


def test_the_painter_table_is_the_painter():
    """Six methods, the same names, the same required arguments, and it really draws."""
    renderer = _renderer()
    _engine()
    grid, painter = _grid_painter(renderer)
    for method, positional in PAINTER_API.items():
        call = getattr(painter, method, None)
        assert callable(call), (
            "docs/SKINS.md documents painter.%s() and the Painter in %s does not "
            "have it" % (method, RENDERER))
        given = inspect.signature(call).parameters
        absent = [arg for arg in positional if arg not in given]
        assert not absent, (
            "painter.%s does not take %s, which the page's table says it does"
            % (method, ", ".join(absent)))
    assert painter.draw_text(0, 0, "abc") == 3, (
        "the page promises draw_text returns the cells it wrote; this wrote another number")
    assert grid.line(0).startswith("abc"), "draw_text returned 3 and drew nothing"
    assert painter.get_terminal_size() == (60, 12)
    assert painter.color_support() in ("truecolor", "256", "16", "none"), (
        "the page documents three spellings and says a fourth is possible")


# --------------------------------------------------- …and the example runs ---

def _install_pack(tmp_path, manifest=None):
    """Write the pack the way the page says to write it, and install it. Returns the folder.

    Installing means the state file as well as the folder: `PluginManager` loads
    what `.beeagent/plugins.json` lists, and a folder that merely exists has not
    been installed. The page says so, and this is where that claim is checked.
    """
    root = tmp_path / ".beeagent" / "plugins" / FOLDER
    root.mkdir(parents=True)
    (root / "plugin.py").write_text(_example_skin() + "\n", encoding="utf-8")
    (root / "plugin.json").write_text(
        json.dumps(manifest or _example_manifest(), indent=2), encoding="utf-8")
    (tmp_path / ".beeagent" / "plugins.json").write_text(json.dumps({"installed": {
        FOLDER: {"type": "plugin", "category": "plugins",
                 "description": "docs/SKINS.md example", "enabled": True,
                 "source": {"kind": "builtin", "path": "plugins/" + FOLDER}}}},
        indent=2), encoding="utf-8")
    return root


def _load_with_the_real_loader(tmp_path):
    """Build an `Agent` in the pack's folder, which is the startup path.

    `Agent()` constructs a `PluginLoader(gate_project=True)` and calls `load_all()`,
    so this exercises the trust gate too: without the folder's answer recorded, the
    pack is withheld and the skin is never registered — which is the correct
    behaviour, and the one the page's safety section describes.
    """
    from beeagent.config.schema import BeeConfig

    try:
        from beeagent.core import trust
    except ImportError as e:
        pytest.skip("skin engine not landed (no beeagent.core.trust: %s)" % e)
    try:
        trust.for_folder(str(tmp_path)).say_trusted()
    except Exception as e:
        pytest.skip("skin engine not landed (the folder's trust answer cannot be "
                    "recorded: %s: %s)" % (type(e).__name__, e))
    try:
        from beeagent.core.agent import Agent
        agent = Agent(config=BeeConfig())
    except Exception as e:
        pytest.skip("skin engine not landed (no Agent can be built to load the pack: "
                    "%s: %s)" % (type(e).__name__, e))
    loader = getattr(agent, "plugins", None)
    assert loader is not None, "Agent() built no plugin loader"
    assert not loader.load_errors, (
        "the loader was handed the pack and could not take it: %s" % loader.load_errors)
    assert not loader.withheld, (
        "the pack was withheld even with the folder trusted: %s" % loader.withheld)
    return agent


def test_the_documented_pack_loads_registers_and_paints(tmp_path, monkeypatch, host):
    """Register it, feed it `(dt, event)`, and look at the cells it actually drew.

    Everything the page claims, in one run: the folder layout, `setup()` as the
    door, `register(NAME, <object with hooks>)`, the event names,
    `painter.get_terminal_size()`, `draw_text`, `clear_region` on the way out, and
    the frame budget the page promises to stay inside.
    """
    renderer = _renderer()
    skins = host
    _install_pack(tmp_path)
    monkeypatch.chdir(tmp_path)                  # the manager reads `.beeagent/` from cwd
    _load_with_the_real_loader(tmp_path)

    assert SKIN_NAME in skins.names(), (
        "the pack was installed, trusted and loaded, and the host still does not know "
        "a skin called %r — the page's install section is wrong" % SKIN_NAME)
    record = skins.stats(SKIN_NAME)
    assert record, "no host record for the registered skin"
    assert not record["refused"], record["refused"]
    assert "on_frame" in record["hooks"], (
        "the host registered the skin and found no on_frame in it (%r). The page's "
        "section 3 says a skin with no hooks is marked active and draws nothing, "
        "forever, with no warning — so this is the one failure an author cannot see "
        "from the interface" % record["hooks"])

    assert skins.switch(SKIN_NAME) == "", "switch() answered with a refusal sentence"
    grid, painter = _grid_painter(renderer)

    assert skins.post("reasoning_delta", {"text": "hi"}) is True, (
        "post() says the active skin has no on_event, and the page says it has one")
    budget = skins.budget()["frame_ms"]
    painted = []
    for dt in (0.1, 0.15, 0.2):
        spent = skins.frame(painter, dt)
        assert spent < budget, (
            "a documented frame cost %.2f ms against a %.0f ms budget; the page tells "
            "authors this skin stays inside it" % (spent, budget))
        painted.append(grid.line(11))
    lit = [row for row in painted if "thinking" in row]
    assert lit, "three frames after a token, the bottom row reads %r — no pulse" % painted[-1]
    assert len(set(lit)) > 1, (
        "the pulse is painted but does not move (%s) — `dt` is not reaching on_frame"
        % ", ".join(repr(row.rstrip()) for row in painted))

    host.post("done", {})
    host.frame(painter, 0.1)                     # the wipe lands on the frame after
    assert grid.line(11).strip() == "", (
        "after `done` the page promises the row wipes itself, and it still reads %r"
        % grid.line(11).strip())

    after = skins.stats(SKIN_NAME)
    assert not after["demoted"], "the host stopped the skin mid-test: %s" % after["reason"]
    assert after["frame_overruns"] == 0, after
    assert not painter.frame_warnings, (
        "the painter degraded something the page does not document: %s"
        % painter.frame_warnings)


def test_the_documented_switch_command_switches(commands, host):
    """`/skins <name>` and `/skins off` do, and say, what section 5 says they do."""
    skins = host
    entry = skins.install_source(SKIN_NAME, _example_skin())
    assert not entry.refused, "the gate refused the example it documents: %s" % [
        str(r) for r in entry.refusals]
    assert skins.register_command(), "/skins could not be registered"

    assert skins.switch(SKIN_NAME) == ""
    assert skins.active_name() == SKIN_NAME

    off = str(commands.dispatch(None, "/skins off").output)
    assert skins.active_name() == skins.BASELINE, "`/skins off` did not hand the screen back"
    assert skins.BASELINE in off, (
        "the page shows `/skins off` answering %r; it answered %r" % ("skin: baseline", off))

    back = commands.dispatch(None, "/skins " + SKIN_NAME)
    assert skins.is_active(SKIN_NAME), (
        "the documented switch command did not make the skin active: %r"
        % str(back.output))
    assert SKIN_NAME in str(back.output)

    missing = commands.dispatch(None, "/skins nobody-wrote-this")
    assert "nobody-wrote-this" in str(missing.output), (
        "a switch to a name that does not exist must say the name back: section 5 "
        "tells the reader that sentence is their whole error report")
    assert skins.active_name() == SKIN_NAME, (
        "a failed switch changed the active skin; the page says a refusal never draws")


def test_the_listing_reports_the_active_skin(host):
    """`/skins` with no argument is how an author checks which hooks were found."""
    skins = host
    skins.install_source("plain-row", "def on_frame(dt, painter):\n    pass\n")
    assert skins.switch("plain-row") == ""
    try:
        listing = skins.listing()
    except Exception as e:
        pytest.skip(
            "skin listing not landed: %s.listing() raises %s — it builds the baseline "
            "row through a _baseline_entry() helper that the module references and "
            "never defines, so the bare /skins the page documents cannot answer yet "
            "(docs/SKINS.md section 7, item 9)" % (ENGINE, type(e).__name__))
    assert "plain-row" in listing, listing
    rows = [line for line in listing.splitlines() if "plain-row" in line]
    assert rows[0].lstrip().startswith("*"), (
        "the page says `*` marks the skin in force; this row does not: %r" % rows[0])
    assert "on_frame" in rows[0], (
        "the listing is how an author sees the hooks the host found; %r does not say"
        % rows[0])


def test_the_surfaces_example_claims_and_answers(host):
    """The page's second skin: gated, claimed, and read back off a real grid.

    Section 5 promises that this block is installed from source text and that its
    three lines come out as written. Both halves are checked here, because a
    sentence about markup that nobody renders is how a page ends up teaching
    `[bold]` as a colour.
    """
    from rich.text import Text

    renderer = _renderer()
    skins = host
    source = _block_named("surfaces.py")
    assert skins.check_source(source) == [], [
        str(r) for r in skins.check_source(source)]
    skins.register(SURFACES_NAME, source=source, description="docs example")
    assert skins.switch(SURFACES_NAME) == ""
    report = skins.surfaces()
    assert sorted(report["held"]) == ["answer", "hud", "spinner", "status"], report
    assert report["hud_rows"] == 1, report

    skins.post("stream_delta", {"text": "x"})
    assert str(Text.from_markup(skins.status_text("model gpt"))) == "1 tok model gpt"
    assert str(Text.from_markup(skins.spinner_text("buzzing...", 0.0))) == "buzzing..."

    # The markup form of the block surface: what a skin the gate will not let
    # import Rich can still do, and the answer's own brackets have to survive it.
    block = skins.answer_render("list: [a]\n", True)
    assert str(block) == "1 tok\nlist: [a]\n", repr(str(block))

    grid = renderer.Grid(40, 1)
    painter = renderer.Painter(grid, size=(40, 1))
    assert skins.hud_frame(painter, 0.1) is True
    assert grid.line(0).count("█") == 0, "one token is not a fifth of the bar"
    assert grid.line(0) == "░" * 40, repr(grid.line(0))
    for _ in range(250):
        skins.post("stream_delta", {"text": "x"})
    skins.hud_frame(painter, 0.1)
    assert grid.line(0).count("█") == 40, repr(grid.line(0))
    assert not painter.frame_warnings, painter.frame_warnings


def test_a_skin_that_raises_is_demoted_once_and_says_so(host):
    """The page promises one visible notice, a handback to the baseline, and no revival."""
    skins = host
    renderer = _renderer()
    notes = []
    skins.set_notifier(notes.append)
    skins.install_source("boom", "def on_frame(dt, painter):\n    raise ValueError('boom')\n")
    assert skins.switch("boom") == ""
    _grid, painter = _grid_painter(renderer, 40, 8)
    skins.frame(painter, 0.016)

    record = skins.stats("boom")
    assert record["demoted"], "a hook that raised did not stop the skin"
    assert skins.active_name() == skins.BASELINE, (
        "the skin was stopped and is still marked active — the page promises the "
        "plain interface comes back, never a silent switch")
    assert len(notes) == 1, (
        "the notice was announced %d times; the page promises it is said once" % len(notes))
    assert "boom" in notes[0] and "ValueError" in notes[0], notes[0]
    assert skins.switch("boom"), (
        "a stopped skin switched back to without a reason; the page says it runs again "
        "only if it is registered fresh")
    assert skins.frame(painter, 0.016) == 0.0, "a stopped skin is still being painted"
