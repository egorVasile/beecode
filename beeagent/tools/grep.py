"""Regex search over files.

Walk is lazy and pruned: `rglob("*")` materialised the whole tree before the
first file was read, so pointing this at a repository with a .git or a
node_modules paid for every path in it — and `.git` objects then produced
matches nobody asked for.

What the audit added: an answer of "No matches found" has to mean *nothing in
this tree matches*, which is a claim about every file that was skipped as well.
So a cp1251 file is decoded as cp1251, a binary or oversized file is named as
not searched, and neither is ever reported as a non-match.
"""
import locale
import os
import re
from pathlib import Path, PurePosixPath

from beeagent.i18n import L

from ._path_policy import guard, is_inside
from .base import BaseTool, ToolResult

MAX_FILES = 200
MAX_MATCHES = 50
# Read whole, or not at all: a 3.5 MB single-line "log" used to come back as a
# 4,000,041-character answer with nothing saying so.
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_MATCH_CHARS = 400
MAX_OUTPUT_CHARS = 200_000
SNIFF_BYTES = 4096
_LOCALE_ENCODING = (locale.getpreferredencoding(False) or "utf-8").lower()

# Longest BOM first: the UTF-32-LE mark starts with the UTF-16-LE one.
_BOMS = ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
         (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"),
         (b"\xef\xbb\xbf", "utf-8-sig"))

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv",
             "venv", ".beeagent", ".mypy_cache", "dist", "build"}

_SKIP_KEYS = ("binary", "undecodable", "oversized", "unreadable", "escaped")
_PLAIN_UTF8 = ("utf-8", "utf-8-sig")


def _nfiles(n: int) -> str:
    return "1 file" if n == 1 else f"{n} files"


def _encoding_names(raw: bytes):
    """What the file says it is: UTF-8 first, then its own cookie, then the
    machine's text encoding — never `errors="replace"`, which turns a Cyrillic
    source file into a confident lie about its contents."""
    yield "utf-8"
    head = raw[:512].split(b"\n")[:2]
    for line in head:
        found = re.search(rb"coding[:=]\s*([-\w.]+)", line)
        if found:
            name = found.group(1).decode("ascii", "replace").lower()
            if name not in ("utf-8", "utf8"):
                yield name
            break
    if _LOCALE_ENCODING not in ("utf-8", "utf8"):
        yield _LOCALE_ENCODING


def _read_lines(path):
    """(lines, encoding used) or (None, why the file was not searched)."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, "oversized"
        raw = path.read_bytes()
    except OSError:
        return None, "unreadable"
    for bom, name in _BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(name).splitlines(), name
            except (UnicodeDecodeError, LookupError):
                return None, "undecodable"
    if b"\x00" in raw[:SNIFF_BYTES]:
        return None, "binary"
    for name in _encoding_names(raw):
        try:
            return raw.decode(name).splitlines(), name
        except (UnicodeDecodeError, LookupError):
            continue
    return None, "undecodable"


def _glob_to_regex(pattern: str) -> re.Pattern:
    """A glob the way it is typed: `*` stops at a separator, `**/` crosses any
    number of them, and a slash means the path relative to the search root."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:[^/]*/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            close = pattern.find("]", i + 1)
            if close < 0:
                out.append(re.escape(char))
                i += 1
            else:
                body = pattern[i + 1:close]
                out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
                i = close + 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE if os.name == "nt" else 0)


def _compile_include(include: str):
    """(matcher, refusal) for `*.py`, `src/*.py`, `**/tests/*_test.py`."""
    text = str(include).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return None, L("`include` is empty after trimming — drop it or name a pattern "
                       "like *.py",
                       "`include` пуст после обрезки — убери его или назови образец вида "
                       "*.py")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or ".." in PurePosixPath(text).parts:
        return None, L(
            f"`include` must be a file name or a path relative to the search root — "
            f"{include!r} points somewhere else, and grep would answer \"no matches\" "
            f"about files it never opened",
            f"`include` должен быть именем файла или путём относительно корня поиска — "
            f"{include!r} указывает в другое место, и grep ответил бы «нет совпадений» о "
            f"файлах, которые не открывал")
    return _glob_to_regex(text), ""


class GrepTool(BaseTool):
    name = "grep"
    description = (
        "Search file contents with a regular expression (log.*Error, class Foo) and return "
        "path:line matches. Filter filenames with include, e.g. *.py or src/*.py — the pattern "
        "matches the file name or the path relative to the search root. Files that cannot be "
        "searched are named, never counted as non-matches. Prefer this over running grep or rg "
        "through bash."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern"},
            "path": {"type": "string", "description": "Directory or file to search (inside the working directory)"},
            "include": {"type": "string", "description": "File name filter, e.g. *.py or src/*.py"},
        },
        "required": ["pattern", "path"],
    }

    def _files(self, root: Path, matcher, stats: dict):
        """Yield (path, path-relative-to-root), pruning the directories that are
        never interesting and the links that leave the project."""
        if root.is_file():
            if self._wanted(root, root.name, matcher, stats):
                yield root, root.name
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            base = Path(dirpath)
            for name in sorted(filenames):
                full = base / name
                rel = full.relative_to(root).as_posix()
                if self._wanted(full, rel, matcher, stats):
                    yield full, rel

    @staticmethod
    def _wanted(full: Path, rel: str, matcher, stats: dict) -> bool:
        if matcher is None:
            return True
        stats["candidates"] += 1
        if matcher.fullmatch(rel) or matcher.fullmatch(PurePosixPath(rel).name):
            stats["include_matched"] += 1
            return True
        return False

    def execute(self, pattern: str, path: str, include: str = None) -> ToolResult:
        target, refusal = guard(path, "grep")
        if refusal:
            return ToolResult(output=refusal, error=True,
                              metadata={"refused": "outside-working-directory"})
        if not isinstance(pattern, str):
            return ToolResult(output=L("`pattern` must be text, not " + type(pattern).__name__,
                                       "`pattern` должен быть текстом, а не "
                                       + type(pattern).__name__), error=True)
        matcher = None
        if include is not None and str(include).strip():
            matcher, refusal = _compile_include(str(include))
            if refusal:
                return ToolResult(output=refusal, error=True)
        try:
            regex = re.compile(pattern)
        except (re.error, TypeError, ValueError) as e:
            return ToolResult(output=L(f"Invalid regex: {e}", f"Неверное регулярное выражение: {e}"),
                              error=True)

        matches: list[str] = []
        extra = 0
        scanned = 0
        stopped = False
        skipped = dict.fromkeys(_SKIP_KEYS, 0)
        other_encodings: dict[str, int] = {}
        stats = {"candidates": 0, "include_matched": 0}

        for f, _rel in self._files(target, matcher, stats):
            if scanned >= MAX_FILES:
                stopped = True
                break
            scanned += 1
            if f.is_symlink() and not is_inside(f):
                skipped["escaped"] += 1
                continue
            lines, info = _read_lines(f)
            if lines is None:
                skipped[info] += 1
                continue
            if info not in _PLAIN_UTF8:
                other_encodings[info] = other_encodings.get(info, 0) + 1
            for number, line in enumerate(lines, 1):
                if regex.search(line):
                    if len(matches) < MAX_MATCHES:
                        body = line if len(line) <= MAX_MATCH_CHARS else line[:MAX_MATCH_CHARS] + "…"
                        matches.append(f"{f}:{number}: {body}")
                    else:
                        extra += 1

        not_searched = sum(skipped.values())
        notes = []
        if other_encodings:
            listed = ", ".join(f"{count}×{name}" for name, count in sorted(other_encodings.items()))
            notes.append(L(f"… files that are not UTF-8 were decoded as {listed} so that this "
                           f"search could read them",
                           f"… файлы не в UTF-8 для поиска были прочитаны как {listed}"))
        if skipped["binary"]:
            notes.append(L(f"… {_nfiles(skipped['binary'])} were NOT searched: binary, not text",
                           f"… {skipped['binary']} файлов НЕ проверялись: они двоичные, а не "
                           f"текстовые"))
        if skipped["undecodable"]:
            notes.append(L(f"… {_nfiles(skipped['undecodable'])} were NOT searched: not text in "
                           f"an encoding we can read (tried UTF-8, a coding cookie and "
                           f"{_LOCALE_ENCODING})",
                           f"… {skipped['undecodable']} файлов НЕ проверялись: это не текст в "
                           f"читаемой кодировке (пробовали UTF-8, cookie и {_LOCALE_ENCODING})"))
        if skipped["oversized"]:
            notes.append(L(f"… {_nfiles(skipped['oversized'])} were NOT searched: bigger than "
                           f"{MAX_FILE_BYTES // 1024} KB each",
                           f"… {skipped['oversized']} файлов НЕ проверялись: каждый больше "
                           f"{MAX_FILE_BYTES // 1024} КБ"))
        if skipped["unreadable"]:
            notes.append(L(f"… {skipped['unreadable']} files could not be read",
                           f"… {skipped['unreadable']} файлов не читались"))
        if skipped["escaped"]:
            notes.append(L(f"… {skipped['escaped']} links point outside the working directory "
                           f"and were NOT searched",
                           f"… {skipped['escaped']} ссылок ведут вне рабочей папки и НЕ "
                           f"проверялись"))
        if stopped:
            notes.append(L(f"… stopped after the first {MAX_FILES} files (narrow the path or "
                           f"pass include)",
                           f"… остановлено на первых {MAX_FILES} файлах (сузь путь или укажи "
                           f"include)"))

        if not matches:
            if matcher is not None and stats["candidates"] and not stats["include_matched"]:
                # "No matches found" here would be a statement about files the
                # filter kept closed.
                return ToolResult(
                    output=L(f"`include` matched none of the {stats['candidates']} files under "
                             f"{path} — nothing was searched. Patterns match the file name "
                             f"(README.*) or the path relative to the search root (src/*.py); "
                             f"they are not absolute and never carry \"..\"",
                             f"`include` не совпал ни с одним из {stats['candidates']} файлов "
                             f"в {path} — поиск не выполнялся. Образец сравнивается с именем "
                             f"файла (README.*) или путём относительно корня поиска (src/*.py); "
                             f"он не бывает абсолютным и не содержит «..»"),
                    error=True, metadata={"count": 0, "scanned": scanned})
            head = L("No matches found", "Совпадений нет")
            if not_searched:
                head += L(f" in the {_nfiles(scanned)} that were searched — {not_searched} more "
                      f"were NOT searched, so this says nothing about them",
                      f" в {scanned} проверенных файлах — ещё {not_searched} файлов не "
                      f"проверялись, так что об них этого ответа нет")
            elif scanned == 0:
                head += L(f" — {path} holds no file to search",
                          f" — в {path} нет файлов для поиска")
            return ToolResult(output="\n".join([head] + notes), error=False,
                              metadata={"count": 0, "scanned": scanned})

        output, used = "", 0
        shown = 0
        for entry in matches:
            if used + len(entry) + 1 > MAX_OUTPUT_CHARS:
                extra += len(matches) - shown
                break
            output = entry if not output else output + "\n" + entry
            used += len(entry) + 1
            shown += 1
        if extra:
            notes.insert(0, L(f"… and {extra} more matches not shown",
                              f"… и ещё {extra} совпадений не показано"))
        if notes:
            output += "\n" + "\n".join(notes)
        return ToolResult(output=output, error=False,
                          metadata={"count": len(matches) + extra, "scanned": scanned})

    def is_safe(self) -> bool:
        return True
