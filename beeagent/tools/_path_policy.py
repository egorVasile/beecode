"""Where the file tools may go — the one place that decides.

The model that picks these arguments is a free endpoint, and a README in the
repository being worked on is an instruction to it.  The audit proved `write`
rewrote an absolute `~/.ssh/authorized_keys`, that `../../x` created directories
beside the project, and that a *lexical* check is not enough: a symlink inside
the folder, a Windows junction and an alternate data stream (`readme.md:evil.svg`)
each name one file and touch another.  So containment is computed from
`os.path.realpath`, never from the spelling that arrived.

It is a policy, not a wall, because a coding agent legitimately edits the folder
it was started in and sometimes a folder the user named:

* inside the working directory after realpath -> run, behaviour unchanged;
* inside the interpreter's own scratch/temp directory, or a directory the user
  added (`BEECODE_TRUSTED_DIRS`, or `grant_root()` once a human has confirmed)
  -> run, that is a folder the user pointed us at;
* anything else -> REFUSE: `error=True`, the message names the resolved target
  and tells the human how to confirm.  Never a silent allow, never a silent
  rewrite of the path.

The temp directory is a root because an agent writes scratch files there
constantly and because that is where this project's own `tmp_path` fixtures live
— a rule the tools' own test suite has to work under beats one that only the
tests bypass.  None of the audit's targets (`.ssh`, `~/.beeagent`, a neighbouring
project) are under it.

Every tool that takes a path calls :func:`guard` before it touches the disk, and
uses the resolved path it gets back for the operation, so the check and the work
can never disagree.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from beeagent.i18n import L

# What a user sets when they want the tools to reach a second folder: the path
# separator list of their own platform, exactly like PYTHONPATH.
TRUSTED_DIRS_ENV = "BEECODE_TRUSTED_DIRS"

# Names Windows resolves to something else than what they say.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_DRIVE = re.compile(r"^[A-Za-z]:$")
_granted: list[str] = []


def grant_root(path) -> None:
    """Trust one more directory.  Only a human decision belongs here."""
    try:
        _granted.append(_norm(os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))))
    except (OSError, ValueError):
        pass


def forget_granted_roots() -> None:
    _granted.clear()


def _norm(path: str) -> str:
    """Case- and separator-folded, with the trailing separator trimmed."""
    text = os.path.normcase(str(path))
    return text.rstrip("\\/") or text


def _roots() -> list[str]:
    """The directories the tools may work in, resolved the same way as a target."""
    found: list[str] = []
    for base in (os.getcwd(), tempfile.gettempdir(), *_granted,
                 *os.environ.get(TRUSTED_DIRS_ENV, "").split(os.pathsep)):
        if not str(base).strip():
            continue
        try:
            found.append(_norm(os.path.realpath(os.path.abspath(os.path.expanduser(str(base))))))
        except (OSError, ValueError):
            continue                      # a root we cannot resolve is not a root
    return found


def _inside(candidate: str, root: str) -> bool:
    if candidate == root:
        return True
    base = root if root.endswith(("\\", "/")) else root + os.sep
    return candidate.startswith(base)


def working_dir() -> str:
    """What the refusal calls 'the working directory'."""
    return _norm(os.path.realpath(os.getcwd()))


def _name_problem(raw: str) -> str:
    """A spelling that does not name the file it looks like — no realpath can see
    these: an NTFS alternate data stream, a trailing dot Windows drops, and a
    device name are all *inside* the folder lexically and somewhere else in fact.
    """
    if "\x00" in raw:
        return L(f"`{raw!r}` contains a NUL character and is not a file name",
                 f"`{raw!r}` содержит NUL и именем файла не является")
    if os.name != "nt":
        return ""                       # on POSIX a colon is just a character
    parts = re.split(r"[\\/]", raw)
    for part in parts:
        if ":" not in part:
            continue
        if _DRIVE.match(part) or part.startswith("?\\") or part.startswith("\\\\"):
            continue          # C:\agent\x, \\?\C:\x and \\server\share are plain paths
        # Anything else — `notes.txt:evil.svg`, or the drive-relative `C:x.svg`
        # that resolves against a directory the model cannot see — is a name
        # whose bytes do not land where the name says they do.
        return L(
            f"`{raw}` is not an ordinary file name on Windows: `{part}` is an alternate "
            f"data stream of another file, or a drive-relative name that resolves against a "
            f"directory nobody can see. Refused; pass a plain path inside the working "
            f"directory",
            f"`{raw}` — не обычное имя файла в Windows: `{part}` либо альтернативный поток "
            f"другого файла, либо путь относительно диска, ведущий в папку, которую никто не "
            f"видит. Отказано; нужен обычный путь внутри рабочей папки")
    last = parts[-1] if parts else raw
    if last in (".", ".."):
        return ""                       # `list_directory .` is the common call, not a trick
    if last and (last.endswith(".") or last.endswith(" ")):
        return L(
            f"`{raw}` ends in a dot or a space, which Windows silently drops — the bytes "
            f"would land in `{last.rstrip('. ')}`, a different file than the one asked for",
            f"`{raw}` заканчивается точкой или пробелом, которые Windows молча отбрасывает, "
            f"— байты попали бы в `{last.rstrip('. ')}`, то есть в другой файл")
    stem = re.split(r"[.]", last, maxsplit=1)[0].upper()
    if stem in _RESERVED:
        return L(
            f"`{raw}` is the Windows device name {stem}, not a file — writing it would "
            f"discard the content or block on a port",
            f"`{raw}` — это имя устройства Windows ({stem}), а не файл: запись в него "
            f"уничтожит содержимое или зависнет на порту")
    return ""


def _outside(raw, resolved: Path, action: str) -> str:
    root = working_dir()
    return L(
        f"`{action}` refused: {raw!r} resolves to `{resolved}`, which is OUTSIDE the "
        f"working directory (`{root}`). BeeCode only touches files inside the folder it "
        f"was started in. Do not retry it and do not look for another path to the same "
        f"file. If the user really wants this one, they have to confirm it themselves: "
        f"start BeeCode inside `{Path(resolved).parent}` or list that folder in "
        f"{TRUSTED_DIRS_ENV}.",
        f"`{action}` отказал: {raw!r} — это `{resolved}`, а он ВНЕ рабочей папки "
        f"(`{root}`). BeeCode трогает файлы только внутри папки, откуда его запустили. "
        f"Не повторяй вызов и не ищи другой путь к тому же файлу. Если файл правда нужен "
        f"пользователю, пусть подтвердит это сам: запустит BeeCode в папке "
        f"`{Path(resolved).parent}` или добавит её в {TRUSTED_DIRS_ENV}.")


def guard(raw, action: str = "read") -> tuple[Path | None, str]:
    """(usable path, refusal) — exactly one of the two is set.

    The path returned is `realpath`'d, so a tool that works on it cannot be
    redirected by a symlink, a junction or a `..` after the check ran.
    """
    text = "" if raw is None else str(raw)
    if not text.strip():
        return None, L(
            f"`{action}` refused: `path` is empty, so there is no target to check",
            f"`{action}` отказал: `path` пуст, проверять нечего")
    problem = _name_problem(text)
    if problem:
        return None, problem + L(". Refused.", " — отказано.")
    try:
        resolved = Path(os.path.realpath(os.path.abspath(os.path.expanduser(text))))
    except (OSError, ValueError) as e:
        return None, L(f"`{action}` refused: `{raw}` could not be resolved ({e}).",
                       f"`{action}` отказал: не удалось разобрать путь `{raw}` ({e}).")
    key = _norm(str(resolved))
    if any(_inside(key, root) for root in _roots()):
        return resolved, ""
    return None, _outside(text.strip(), resolved, action)


def is_inside(raw) -> bool:
    """Whether `raw` may be touched at all — for callers that only need yes/no."""
    target, refusal = guard(raw)
    return bool(target) and not refusal
