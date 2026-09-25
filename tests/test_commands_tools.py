from rich.console import Console

from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.ui.commands import ReplContext, dispatch


def make_ctx():
    cfg = BeeConfig()
    return ReplContext(agent=Agent(config=cfg), config=cfg, session=Session())


def render(res_output) -> str:
    c = Console(width=200)
    with c.capture() as cap:
        c.print(res_output)
    return cap.get()


def test_tools_and_info_commands_answer_in_words():
    """`output is not None` was the whole assertion.

    An empty string, a wall of "unknown command", or the same generic paragraph
    for all six commands would each have passed it, which is the point of a test
    that checks nothing. Each command is now pinned to the thing only it says.
    """
    ctx = make_ctx()
    expected = {
        "/tools": ("read", "bash"),           # the tool roster, by name
        "/token": ("token",),                 # a count of them
        "/stats": ("model",),                 # what it is talking about
        "/config": ("model",),                # the settings table
        "/about": ("BeeCode",),
        "/history": (),                       # empty session: anything, but it must render
    }
    seen = {}
    for line, needles in expected.items():
        res = dispatch(ctx, line)
        text = render(res.output)
        assert text.strip(), f"{line} rendered nothing at all"
        for needle in needles:
            assert needle.lower() in text.lower(), f"{line} never mentioned {needle!r}: {text[:200]}"
        seen[line] = text

    assert len(set(seen.values())) >= 5, \
        "six different commands produced the same paragraph: " + repr(seen)


def test_config_command_does_not_echo_a_secret(tmp_path, monkeypatch):
    """/config prints the settings; a key in it would end up in a screenshot."""
    ctx = make_ctx()
    ctx.config.api_keys = {"groq": "gsk_supersecret_value"}
    ctx.config.pool_token = "seat-token-supersecret"
    text = render(dispatch(ctx, "/config").output)
    assert "gsk_supersecret_value" not in text
    assert "seat-token-supersecret" not in text


def test_unknown_tool_command_is_safe():
    """A bare `/run` must say what is missing, not merely produce a string."""
    ctx = make_ctx()
    res = dispatch(ctx, "/run")
    text = render(res.output)
    assert text.strip(), "a usage error that prints nothing is not a usage error"
    assert "run" in text.lower() or "usage" in text.lower() or "expected" in text.lower(), text[:200]


def test_read_file(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("bee was here")
    ctx = make_ctx()
    res = dispatch(ctx, f"/read {f}")
    assert "bee was here" in render(res.output)


def test_find_glob(tmp_path):
    (tmp_path / "mod.py").write_text("pass")
    ctx = make_ctx()
    res = dispatch(ctx, f"/find *.py {tmp_path}")
    assert "mod.py" in render(res.output)


def test_search_grep(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("def needle():\n    pass\n")
    ctx = make_ctx()
    res = dispatch(ctx, f"/search needle {tmp_path}")
    assert "needle" in render(res.output)


def test_export_writes_file(tmp_path):
    ctx = make_ctx()
    ctx.session.add_user_message("hi")
    out = tmp_path / "s.md"
    res = dispatch(ctx, f"/export {out}")
    assert out.exists()
    assert "hi" in out.read_text(encoding="utf-8")
    assert render(res.output).strip(), "the export has to tell the user where it wrote"
