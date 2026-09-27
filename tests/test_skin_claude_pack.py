"""`skin-claude` — the Claude Code look as nine surfaces and no animation.

What these tests hold shut: the pack registers under its shelf name and is worn
by it, its wordmark is coral pixel letters of one width (a ragged logo is not a
logo), every panel role gets a coral border with errors staying red, the tool
lines carry the `●` mark, and the model's own bytes — `stream`, `answer` — are
never claimed, so nothing the model wrote is ever restyled.

Nothing here reaches the network or the developer's working tree.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from beeagent.core import skins

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "beeagent" / "plugins" / "templates" / "plugins" / "skin-claude" / "plugin.py"
JSON = ROOT / "beeagent" / "plugins" / "templates" / "plugins" / "skin-claude" / "plugin.json"


@pytest.fixture(scope="module")
def claude_source():
    return PACK.read_text(encoding="utf-8")


@pytest.fixture
def clean_skins(monkeypatch):
    """An empty registry and a cold clock, so this file is not reading a neighbour."""
    captured = []
    monkeypatch.setattr(skins, "_REGISTRY", {})
    monkeypatch.setattr(skins, "_NOTICES", __import__("collections").deque(maxlen=50))
    monkeypatch.setattr(skins, "_UNKNOWN", {})
    monkeypatch.setattr(skins, "_LOOP", {})
    monkeypatch.setattr(skins, "_ACTIVE", "")
    monkeypatch.setattr(skins, "_NOTIFIER", captured.append)
    monkeypatch.setattr(skins, "_BUDGET", dict(skins.budget()))
    return captured


@pytest.fixture
def claude(clean_skins, claude_source):
    """The pack loaded the way the plugin loader loads it, worn, and its module."""
    from beeagent.ext.api import ExtensionRegistry

    spec = importlib.util.spec_from_file_location("claude_under_test", str(PACK))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class Api:
        plugin = "skin-claude"

        def __init__(self):
            self.registry = ExtensionRegistry()

        def skin_hooks(self, name, hooks, description=""):
            return not skins.register(name, hooks, description=description,
                                      pack=self.plugin).refused

    module.setup(Api())
    assert skins.switch("claude") == "", "the pack registered a skin that cannot be worn"
    module.worn = skins
    yield module
    sys.modules.pop(spec.name, None)


def _plain(markup: str) -> str:
    return re.sub(r"\[[^\]]*\]", "", markup)


# ------------------------------------------------------------------- the contract --

def test_the_pack_passes_the_gate(claude_source):
    """A stranger's file that reaches this folder is still read by the gate."""
    import ast

    assert skins.check_source(claude_source) == [], "the shipped pack is refused by the gate"
    reached = set()
    for node in ast.walk(ast.parse(claude_source)):
        if isinstance(node, ast.Import):
            reached |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            reached.add(node.module)
    allowed = skins.IMPORT_ALLOWLIST
    outside = {name for name in reached if name.split(".")[0] not in
               {a.split(".")[0] for a in allowed}}
    assert not outside, f"the pack imports {sorted(outside)}, which the gate does not allow"
    assert not any(name.startswith("beeagent.ui") for name in reached), \
        "a pack that reaches the interface modules bypasses the surfaces"


def test_the_manifest_names_the_pack(claude_source):
    import json

    manifest = json.loads(JSON.read_text(encoding="utf-8"))
    assert manifest["name"] == "skin-claude" and manifest["entry"] == "plugin.py"


def test_the_shelf_name_wears_it(claude):
    assert skins.resolve("skin-claude") == "claude"
    assert skins.for_pack("skin-claude") == ["claude"]
    report = skins.surfaces("claude")
    assert sorted(report["held"]) == ["banner", "error", "frame", "spinner", "status",
                                      "thinking", "tool_end", "tool_start", "welcome"], report
    assert report["refused"] == {}, report


def test_the_model_s_bytes_are_never_claimed(claude):
    """No `stream`, no `answer`: the model's text is evidence, not decoration."""
    assert not skins.owns("stream"), "the pack rewrote the streaming surface"
    assert not skins.owns("answer"), "the pack restyled the model's answer"


# ------------------------------------------------------------------- the logo ---

def test_the_wordmark_is_coral_pixel_letters_of_one_width(claude):
    out = claude.on_banner(0.0, 0, 5, None)
    assert "#D77757" in out, "the clawd coral from the client itself"
    lines = _plain(out).split("\n")
    assert len(lines) == 5
    assert len({len(line) for line in lines}) == 1, "a ragged logo is not a logo"
    assert max(len(line) for line in lines) <= 72, "must fit an 80-column terminal"
    joined = "".join(lines)
    assert "█" in joined, "pixel blocks, not plain text"


def test_the_wordmark_survives_a_narrow_terminal(claude):
    assert claude.on_banner(0.0, 10, 5, None) == "CLAUDE CODE"


# ------------------------------------------------------------------- the lines ---

def test_each_panel_role_sits_in_coral_and_errors_stay_red(claude):
    answer = claude.on_frame_color("answer", 0.0)
    tool = claude.on_frame_color("tool", 0.0)
    assert answer.startswith("bold #") and tool.startswith("bold #")
    assert answer != tool, "roles share one cycle point otherwise"
    assert "E05555" in claude.on_frame_color("error", 0.0).upper()
    assert claude.on_frame_color("no-such-role", 0.0) == answer


def test_the_tool_lines_carry_the_dot(claude):
    assert claude.on_tool_start("read a", "read").startswith("● ")
    assert claude.on_tool_end("read done", "read", False).startswith("● ")
    assert claude.on_tool_end("read bad", "read", True).startswith("✗ ")


def test_the_welcome_has_the_crab_and_the_host_line(claude):
    out = claude.on_welcome("hello")
    assert "Welcome back!" in out
    assert "▐▛" in out, "the clawd crest did not come"
    assert "hello" in out, "the host's own line must survive"


def test_the_spinner_reads_the_handed_clock(claude):
    assert claude.on_spinner("x", 0.0) != claude.on_spinner("x", 0.75)
    assert claude.on_spinner("x", 0.0) == claude.on_spinner("x", 0.0)


def test_a_claimed_welcome_answers_through_the_host(claude):
    assert skins.welcome_text("hi") != "hi"
    assert "Welcome back!" in skins.welcome_text("hi")
