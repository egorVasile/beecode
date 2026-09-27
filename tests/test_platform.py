"""The phone screen: Termux detection, the one-time question, the small logo.

A 76-cell animated logo wraps into a mess and stutters on a phone, so a screen
that agreed once gets a 35-cell static one. Everything here runs against the
faked home, never the developer's real `~/.beecode`.
"""
import io
import json
import sys

from beeagent.ui import platform as pf


def _clean(monkeypatch):
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("BEECODE_DISPLAY_ASK", raising=False)
    old_prefix = __import__("os").environ.get("PREFIX", "")
    monkeypatch.setenv("PREFIX", "/usr" if "com.termux" in old_prefix else old_prefix)
    pf.reset_cache()


class _Tty(io.StringIO):
    def isatty(self):
        return True


def _type(monkeypatch, text: str):
    monkeypatch.setattr(sys, "stdin", _Tty(text))


def test_termux_is_recognised_by_platform_env_or_prefix(monkeypatch):
    _clean(monkeypatch)
    assert pf.is_termux() is False
    monkeypatch.setenv("TERMUX_VERSION", "0.119")
    assert pf.is_termux() is True
    _clean(monkeypatch)
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    assert pf.is_termux() is True
    _clean(monkeypatch)
    monkeypatch.setattr(sys, "platform", "android")
    assert pf.is_termux() is True


def test_the_question_is_asked_once_and_enter_means_yes(monkeypatch):
    _clean(monkeypatch)
    _type(monkeypatch, "\n")
    pf.ask_once()
    saved = json.loads(pf.display_path().read_text(encoding="utf-8"))
    assert saved == {"asked": True, "adaptive": True}
    # A second launch never prompts again, whatever stdin holds now.
    _type(monkeypatch, "n\n")
    pf.ask_once()
    assert json.loads(pf.display_path().read_text(encoding="utf-8")) == saved


def test_no_means_the_full_logo_forever(monkeypatch):
    _clean(monkeypatch)
    _type(monkeypatch, "n\n")
    pf.ask_once()
    monkeypatch.setenv("TERMUX_VERSION", "0.119")
    assert pf.want_compact() is False


def test_yes_on_termux_means_the_small_logo(monkeypatch):
    _clean(monkeypatch)
    _type(monkeypatch, "\n")
    pf.ask_once()
    assert pf.want_compact() is False, "not a phone, nothing to shrink"
    monkeypatch.setenv("TERMUX_VERSION", "0.119")
    pf.reset_cache()
    assert pf.want_compact() is True


def test_env_zero_with_whitespace_skips(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("BEECODE_DISPLAY_ASK", "0 ")
    _type(monkeypatch, "\n")
    pf.ask_once()
    assert not pf.display_path().exists()


def test_no_tty_means_no_question_and_no_file(monkeypatch, tmp_path):
    _clean(monkeypatch)

    class DeadStdin:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", DeadStdin())
    pf.ask_once()
    assert not pf.display_path().exists()


def test_the_compact_logo_fits_a_phone_screen():
    from beeagent.ui.components import COMPACT_ROWS, banner_compact

    assert COMPACT_ROWS, "the word did not render"
    assert max(len(r) for r in COMPACT_ROWS) <= 48, "must fit a phone screen"
    text = banner_compact()
    assert len(text.plain.splitlines()) == len(COMPACT_ROWS)
    assert text.plain.strip(), "the small logo is not blank"


def test_compact_banner_prints_without_animation(monkeypatch, capsys):
    from beeagent.ui import components as comp

    _clean(monkeypatch)
    _type(monkeypatch, "\n")
    pf.ask_once()
    monkeypatch.setenv("TERMUX_VERSION", "0.119")
    pf.reset_cache()
    comp.print_banner()
    out = capsys.readouterr().out
    assert "BeeCode" in out
