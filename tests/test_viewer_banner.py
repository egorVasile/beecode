import re

from rich.console import Console
from rich.panel import Panel

from beeagent.config.schema import BeeConfig
from beeagent.core.session import Message, Session
from beeagent.ui import components as comp
from beeagent.ui.commands import ReplContext, dispatch, history_body
from beeagent.ui.components import (
    BANNER_ROWS, _shimmer_color, banner_frame, banner_static, print_banner,
)
from beeagent.ui.viewer import viewer_key_bindings


# --- banner shimmer ---------------------------------------------------------

def test_shimmer_color_hits_the_ramp_ends():
    assert _shimmer_color(0.0) == "#fff9c4"
    assert _shimmer_color(1.0) == "#43a047"
    # out-of-range positions clamp instead of crashing
    assert _shimmer_color(-2.0) == _shimmer_color(0.0)
    assert _shimmer_color(3.0) == _shimmer_color(1.0)


def test_shimmer_color_emits_hex():
    for p in (0.0, 0.17, 0.5, 0.83, 1.0):
        assert re.fullmatch(r"#[0-9a-f]{6}", _shimmer_color(p))


def test_shimmer_frame_keeps_the_pixel_art():
    # Animation must only move colours around, never change the logo shape.
    assert banner_frame(0.0).plain == banner_static().plain
    assert banner_frame(0.5).plain == "\n".join(BANNER_ROWS)


def _colours(text):
    console = Console()
    return [text.get_style_at_offset(console, i) for i in range(len(text.plain))]


def test_shimmer_colours_move_between_frames():
    a, b = banner_frame(0.0), banner_frame(0.35)
    assert _colours(a) != _colours(b)
    # a full cycle lands back on the starting picture
    assert _colours(banner_frame(1.0)) == _colours(a)


def test_banner_stays_static_without_a_terminal():
    # pytest captures stdout, so the animation must not even be attempted.
    assert comp.console.is_terminal is False
    assert comp._banner_animates(None) is False
    print_banner()  # must not raise


def test_banner_animation_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(type(comp.console), "is_terminal", property(lambda self: True))
    assert comp._banner_animates(False) is False
    assert comp._banner_animates(None) is True
    monkeypatch.setenv("BEECODE_NO_ANIM", "1")
    assert comp._banner_animates(None) is False


# --- animation acts: reveal -> shimmer -> resting palette -------------------

def _cells(text):
    """Colours of every drawn block, in reading order."""
    console = Console()
    return [text.get_style_at_offset(console, i) for i, ch in enumerate(text.plain) if ch == "█"]


def test_reveal_act_only_draws_what_appeared():
    early = banner_frame(0.0)  # fully drawn baseline
    front = comp.banner_reveal_frame(comp.BANNER_GLOW_SPAN)
    assert 0 < front.plain.count("█") < early.plain.count("█")
    assert len(front.plain) == len(early.plain)  # same canvas, no jumping layout


def test_reveal_act_hands_over_to_the_shimmer_without_a_jump():
    last_reveal = comp.banner_reveal_frame(comp.BANNER_WIDTH + comp.BANNER_GLOW_SPAN)
    first_shimmer = comp.banner_settle_frame(0.0, 0.0)
    assert last_reveal.plain == first_shimmer.plain
    assert _cells(last_reveal) == _cells(first_shimmer)


def test_shimmer_act_cools_into_the_resting_palette():
    assert _cells(comp.banner_settle_frame(0.5, 1.0)) == _cells(banner_static())
    # and the whole sequence ends exactly on the static logo
    assert _cells(comp._banner_frames()[-1]) == _cells(banner_static())


def test_flash_is_brighter_than_the_ramp_it_settles_into():
    assert sum(comp._rgb(comp.FLASH)) > sum(comp._rgb(comp.HONEY))
    assert comp._blend("#000000", "#ffffff", 0.5) == "#808080"


# --- block-frame theme ------------------------------------------------------

def test_frames_are_the_classic_rounded_box_but_bold():
    from rich import box as rich_box
    table = comp.commands_table([])
    assert table.box is rich_box.ROUNDED          # the old frame, not blocks
    assert table.border_style == comp.BORDER
    assert comp.BORDER.startswith("bold")
    console = Console(width=40, color_system="truecolor", force_terminal=True)
    with console.capture() as cap:
        console.print(Panel("x", box=rich_box.ROUNDED, border_style=comp.BORDER))
    # 1;38;2 = bold attribute + truecolour leaf on the border glyphs
    assert "[1;38;2;" in cap.get()


def _cells_text_styles(text):
    console = Console()
    return [str(text.get_style_at_offset(console, i)) for i in range(len(text.plain))]


def test_bee_title_runs_along_the_honey_ramp():
    styles = _cells_text_styles(comp.bee_title("BeeCode"))
    assert styles[0] != styles[-1]
    assert all("#" in s for s in styles)


# --- scrollable history -----------------------------------------------------

def _ctx_with(pairs):
    ctx = ReplContext(agent=None, config=BeeConfig(), session=Session())
    for role, content in pairs:
        ctx.session.messages.append(Message(role, content))
    return ctx


def test_history_body_shows_full_messages():
    long_text = "Пчела " + "x" * 300 + "\nвторая строка"
    ctx = _ctx_with([("user", long_text), ("assistant", "привет")])
    body = history_body(ctx.session)
    assert long_text in body          # no more 80-char snippets
    assert "привет" in body
    assert "user" in body and "assistant" in body


def test_history_command_opens_the_viewer():
    ctx = _ctx_with([("user", "ку")])
    res = dispatch(ctx, "/history")
    assert res.action == "history_pager"
    assert res.output is not None


def test_history_list_stays_inline():
    ctx = _ctx_with([("user", "ку")])
    res = dispatch(ctx, "/history list")
    assert res.action is None
    console = Console(width=120)
    with console.capture() as cap:
        console.print(res.output)
    assert "ку" in cap.get()


def test_viewer_keys_scroll_and_close():
    # (constructing the Application itself needs a real Windows console, so the
    # bindings are checked on their own.)
    kb = viewer_key_bindings()
    names = {str(key).split(".")[-1].lower() for binding in kb.bindings for key in binding.keys}
    for key in ("up", "down", "pageup", "pagedown", "home", "end", "q", "escape", "controlm"):
        assert key in names, key
