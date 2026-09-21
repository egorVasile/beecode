import asyncio

from beeagent.config.schema import BeeConfig
from beeagent.core.session import Session
from beeagent.ui.commands import ReplContext
from beeagent.ui.components import brand_ramp
from beeagent.ui.repl import try_picker, _picker_specs


def _ctx():
    return ReplContext(agent=None, config=BeeConfig(), session=Session())


def test_picker_skips_when_arg_given():
    # With an explicit argument the plain dispatch path is used, no dialog.
    assert asyncio.run(try_picker(_ctx(), "model", ["gpt-4o"])) is None


def test_picker_skips_non_list_commands():
    assert asyncio.run(try_picker(_ctx(), "help", [])) is None
    assert asyncio.run(try_picker(_ctx(), "about", [])) is None


def test_picker_titles_are_bee_ramp():
    # The dialog heading is painted per character, so it must be handed to
    # prompt_toolkit as (style, char) tokens rather than markup.
    for name, (title, *_rest) in _picker_specs(_ctx()).items():
        ramp = brand_ramp(title)
        assert "".join(ch for _, ch in ramp) == title, name
        assert all(style.startswith("bold #") for style, _ in ramp), name


def test_picker_specs_cover_list_commands():
    specs = _picker_specs(_ctx())
    for name in ("models", "providers", "mode", "theme", "sessions"):
        assert name in specs
    # each spec maps to an apply-command that starts with a slash
    for title, values_fn, apply_cmd, current_fn in specs.values():
        assert apply_cmd.startswith("/")
        assert isinstance(title, str)
        assert callable(values_fn)
        assert callable(current_fn)


def test_picker_returns_none_when_no_values(monkeypatch):
    # Force an empty value list so no dialog is attempted.
    # (try_picker is a coroutine: prompt_toolkit dialogs must be awaited,
    #  their blocking .run() cannot nest in the REPL's event loop.)
    import beeagent.ui.repl as repl
    monkeypatch.setattr(repl, "available_models", lambda ctx: [])
    assert asyncio.run(try_picker(_ctx(), "models", [])) is None


def test_model_picker_marks_recommended_and_shows_windows():
    from beeagent.core import windows
    from beeagent.ui.commands import model_choices

    windows.remember("command-a-03-2025", 65536)
    pairs = model_choices(_ctx())
    values = [value for value, _ in pairs]
    labels = dict(pairs)

    assert values[0] == "command-a-03-2025", "the measured wide route leads the list"
    assert labels[values[0]].lstrip().startswith("★"), "recommended is marked, not implied"
    assert "✔ 65k" in labels[values[0]], "measured windows are told apart from claims"
    assert any("~" in label for label in labels.values())
