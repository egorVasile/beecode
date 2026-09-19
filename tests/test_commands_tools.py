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


def test_tools_and_info_commands_return_output():
    ctx = make_ctx()
    for line in ("/tools", "/token", "/stats", "/config", "/about", "/history"):
        res = dispatch(ctx, line)
        assert res.output is not None, line


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
    assert res.output is not None


def test_unknown_tool_command_is_safe():
    ctx = make_ctx()
    res = dispatch(ctx, "/run")
    assert res.output is not None  # usage error text
