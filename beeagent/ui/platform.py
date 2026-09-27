"""Where BeeCode runs, and how much chrome that screen can take.

A phone on Termux is narrow and slow: the 76-cell animated logo wraps into a
mess and stutters through 38 frames, so such a screen gets a small static one
instead. Desktops keep the full shimmer.

The user is asked exactly once. "Yes" turns on adaptive mode — every launch
detects the platform and picks the logo; "no" means the full logo everywhere,
and nothing is ever detected or asked again. The answer lives next to the
install key (`~/.beecode/display.json`), not in a project file, because it is
about the machine, not the checkout.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def is_termux() -> bool:
    """Is this Termux on Android — the slow narrow screen."""
    if sys.platform == "android":
        return True
    if os.environ.get("TERMUX_VERSION"):
        return True
    return "com.termux" in (os.environ.get("PREFIX") or "")


def is_windows() -> bool:
    return os.name == "nt"


def terminal_width(default: int = 80) -> int:
    """Console columns, without ever raising for it."""
    try:
        import shutil

        return shutil.get_terminal_size(fallback=(default, 24)).columns or default
    except Exception:
        return default


def display_path() -> Path:
    return Path.home() / ".beecode" / "display.json"


def load_choice() -> dict:
    """{"asked": bool, "adaptive": bool} — absent file means never asked."""
    try:
        data = json.loads(display_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_choice(choice: dict) -> None:
    """Remember the one answer. A refused write only skips the memory of it."""
    try:
        path = display_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(choice), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _read_answer(prompt: str, timeout: float = 120.0) -> str | None:
    """One input line, or None when nobody answers in time.

    A first launch that blocks forever on a question is a hang, not a prompt:
    a terminal left unattended (or a pty that looks like a tty but has nobody
    behind it) gets the default instead. The reader thread is daemonic, so a
    late answer after the timeout lands nowhere.
    """
    import threading

    box: dict = {}

    def _read() -> None:
        try:
            box["answer"] = input(prompt)
        except Exception as exc:
            box["error"] = exc

    worker = threading.Thread(target=_read, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return None
    if "error" in box:
        raise box["error"]
    return box.get("answer", "")


def ask_once() -> None:
    """The single question, asked once, answered by tapping Enter.

    Skipped when there is nobody to tap it (piped stdin, one-shot runs, tests)
    or when `BEECODE_DISPLAY_ASK=0` (whitespace tolerated — `set FOO=0 ` in
    cmd.exe keeps the space). Enter/yes adapts the logo to the screen;
    anything else keeps the full Windows logo and never checks again. Silence
    past two minutes counts as Enter.
    """
    if os.environ.get("BEECODE_DISPLAY_ASK", "").strip() == "0":
        return
    if load_choice().get("asked"):
        return
    try:
        is_tty = sys.stdin.isatty()
    except Exception:
        return
    if not is_tty:
        return
    from beeagent.i18n import L

    try:
        answer = _read_answer(L("Compact logo and calmer animations for a phone "
                                "screen? [Y/n] ",
                                "Сжать логотип и успокоить анимации для экрана "
                                "телефона? [Y/n] "))
    except (EOFError, OSError, KeyboardInterrupt):
        return
    if answer is None:
        # Nobody home: the phone-friendly default, asked never again.
        save_choice({"asked": True, "adaptive": is_termux()})
        return
    save_choice({"asked": True,
                 "adaptive": answer.strip().lower() in ("", "y", "yes", "д", "да", "yep")})


_cached: dict = {}


def want_compact() -> bool:
    """Should this launch use the small static logo and calmer motion.

    The answer file is re-read when it changes (mtime); the environment is
    read fresh every time, so a test that flips `TERMUX_VERSION` cannot leak
    compact mode into the next test through this cache.
    """
    try:
        path = display_path()
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = -1
        if _cached.get("stamp") != stamp:
            _cached["stamp"] = stamp
            _cached["adaptive"] = bool(load_choice().get("adaptive", False))
        return bool(_cached["adaptive"] and is_termux())
    except Exception:
        return False


def reset_cache() -> None:
    """Tests flip the answer file; the process must re-read it."""
    _cached.clear()
