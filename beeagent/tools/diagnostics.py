"""`diagnostics` — a structured answer to "did my edit break anything?".

After an edit the model used to run `bash` and re-parse human-oriented tool
output hunting for its own mistake, and misread it. This tool runs the checkers
that already exist on the machine and answers in one honest line each:
`path:line[:col] severity message (checker)`.

Three rules shape every choice below.

* Nothing is ever installed or downloaded. A checker is used only when it is
  already importable (`python -m ruff`) or runnable (`npx --no-install tsc`);
  the `--no-install` flag is what stops npx reaching for the network. What is
  missing is reported as missing.
* Detection is by "can we run it", never by a guessed path: a Python checker is
  probed with `importlib.util.find_spec`, the node one by running its version.
  Each probe is cached for the process so we pay for it once.
* The report never reads as "the code is fine" unless a checker actually ran and
  returned clean. "Every checker was missing" is spelled out as such — confusing
  those two is this project's worst class of bug.

The program is launched with `run_argv_text` (see `shell.py`), which hands argv
straight to the OS with no shell in between, so a path containing a space or a
semicolon can never become a second command. The target is confined to the
working roots by `guard()` first.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from beeagent.i18n import L

from ._path_policy import guard, working_dir
from .base import BaseTool, ToolResult
from .shell import run_argv_text

# ---- tunables ---------------------------------------------------------------

MAX_SYNTAX_FILES = 2000
DEFAULT_TIMEOUT = 20          # seconds per checker
MIN_TIMEOUT = 1
MAX_TIMEOUT = 300
MAX_FINDINGS_SHOWN = 200      # a wall of linter output is not an answer
_PROBE_TIMEOUT = 25           # how long a version probe is allowed to take

# Outcome kinds for one checker run.
CLEAN = "clean"               # ran, exit 0, nothing to report
FINDINGS = "findings"         # ran, has something to say
CRASH = "crash"               # timed out or blew up — never silent
MISSING = "missing"           # not installed on this machine
SKIPPED = "skipped"           # installed but does not apply to this target

_SEVERITY_ORDER = {"error": 0, "warning": 1, "note": 2, "info": 2, "": 3}


# ---- small value types ------------------------------------------------------

@dataclass
class Finding:
    path: str
    line: str = ""
    col: str = ""
    severity: str = "error"
    message: str = ""
    checker: str = ""

    def render(self) -> str:
        """`path:line[:col] severity message (checker)`, the promised shape."""
        loc = self.path or "?"
        if self.line:
            loc += f":{self.line}"
            if self.col:
                loc += f":{self.col}"
        parts = [loc]
        if self.severity:
            parts.append(self.severity)
        parts.append(self.message)
        tail = f" ({self.checker})" if self.checker else ""
        return " ".join(p for p in parts if p) + tail


@dataclass
class Outcome:
    kind: str
    findings: list = field(default_factory=list)
    note: str = ""            # why it was missing / crashed / skipped


# ---- availability probes, cached once per process ---------------------------

_module_cache: dict[str, bool] = {}
_probe_cache: dict[str, tuple] = {}


_GRANTED = False


def set_granted(granted: bool) -> None:
    """Whether the user allowed this tool right now; mypy and tsc consult it.

    A module flag rather than an argument because the checkers are singletons
    built at import. The worst a race can do is run a gated checker under a grant
    or skip it without one — never write anything.
    """
    global _GRANTED
    _GRANTED = bool(granted)


def _module_available(name: str) -> bool:
    """Can `python -m <name>` run here? Importable == runnable, without guessing."""
    if name not in _module_cache:
        try:
            _module_cache[name] = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError, ModuleNotFoundError):
            _module_cache[name] = False
    return _module_cache[name]


def _reset_probe_cache() -> None:
    """Forget every cached probe. Tests use this to start from a cold process."""
    _module_cache.clear()
    _probe_cache.clear()


def _tsc_launcher_available() -> tuple[bool, str]:
    """Is a tsc already installed and runnable through `npx --no-install`?

    Cached; `--no-install` means a miss is a clean "not here", never a download.
    """
    if "tsc" not in _probe_cache:
        npx = shutil.which("npx")
        if not npx:
            _probe_cache["tsc"] = (False, L("npx is not installed", "npx не установлен"))
        else:
            try:
                out, err, rc = run_argv_text(
                    [npx, "--no-install", "tsc", "--version"], timeout=_PROBE_TIMEOUT)
            except (subprocess.TimeoutExpired, OSError) as e:
                _probe_cache["tsc"] = (False, L(
                    f"npx could not run: {e}", f"не удалось выполнить npx: {e}"))
            else:
                if rc == 0:
                    _probe_cache["tsc"] = (True, "")
                else:
                    _probe_cache["tsc"] = (False, L(
                        "typescript is not installed (npx --no-install declined to "
                        "fetch it)", "typescript не установлен (npx --no-install "
                        "отказался его скачивать)"))
    return _probe_cache["tsc"]


# ---- per-checker parse helpers ----------------------------------------------

def _tail(text: str, limit: int = 300) -> str:
    """The last meaningful line(s) of output, for a crash reason."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return (" … " if len(lines) > 2 else "") + " ".join(lines[-2:])[:limit]


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---- checkers ---------------------------------------------------------------

class Checker:
    """A single diagnostic program.

    Tests build fakes by passing callables for `applies`/`probe`/`run`; real
    checkers subclass and override `argv`, `parse` and friends.
    """
    name = "checker"
    module: str | None = None      # python module that must import for us to run
    # A checker that loads the project's own config can end up executing code the
    # repository supplied. Those run only when the user granted this tool.
    needs_grant = False

    def __init__(self, **overrides):
        # Instance attributes shadow the methods below, so a fake needs no subclass.
        self.__dict__.update(overrides)

    # -- gating ---------------------------------------------------------------
    def applies(self, target: Path) -> bool:
        return True

    def probe(self, target: Path) -> tuple[bool, str]:
        if self.module and not _module_available(self.module):
            return False, L(
                f"module `{self.module}` is not installed",
                f"модуль `{self.module}` не установлен")
        return True, ""

    # -- the work -------------------------------------------------------------
    def argv(self, target: Path) -> list[str]:
        raise NotImplementedError

    def crash_code(self, rc: int) -> bool:
        return False

    def parse(self, stdout: str, stderr: str, rc: int, target: Path) -> list:
        raise NotImplementedError

    def check(self, target: Path, timeout: int) -> Outcome:
        if not self.applies(target):
            return Outcome(SKIPPED, note=L("does not apply to this target",
                                           "не применима к этой цели"))
        if self.needs_grant and not _GRANTED:
            # Installed and applicable, but it imports plugins named in the
            # repository's own config — that is the folder running code.
            return Outcome(SKIPPED, note=L(
                "needs /allow diagnostics: it loads the project's own config",
                "нужен /allow diagnostics: он подхватывает настройки самого проекта"))
        ok, reason = self.probe(target)
        if not ok:
            return Outcome(MISSING, note=reason)
        return self.run(target, timeout)

    def run(self, target: Path, timeout: int) -> Outcome:
        argv = self.argv(target)
        try:
            stdout, stderr, rc = run_argv_text(argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            return Outcome(CRASH, note=L(f"timed out after {timeout}s",
                                         f"превышено {timeout} с"))
        except OSError as e:
            return Outcome(CRASH, note=L(f"could not run: {e}",
                                         f"не удалось запустить: {e}"))
        findings = self.parse(stdout, stderr, rc, target)
        if findings:
            return Outcome(FINDINGS, findings=findings)
        if rc == 0:
            return Outcome(CLEAN)
        if self.crash_code(rc):
            return Outcome(CRASH, note=L(f"exited {rc}: {_tail(stderr or stdout)}",
                                         f"код выхода {rc}: {_tail(stderr or stdout)}"))
        # Non-zero with nothing we could parse is still a finding, never a silent
        # pass: the program clearly complained and we must show that it did.
        return Outcome(FINDINGS, findings=[Finding(
            path=_display(str(target)), severity="error", checker=self.name,
            message=L(f"exited {rc} with no parseable output: {_tail(stderr or stdout)}",
                      f"код выхода {rc} без разборного вывода: {_tail(stderr or stdout)}")
        )])


class SyntaxChecker(Checker):
    """Compile every Python file in-process — no subprocess, and no byte-code.

    `py_compile` writes `__pycache__`, and `compileall` exists precisely to write
    it, so the one checker that always runs would dirty the user's tree and made
    this tool a writer. `compile()` on the source text gives the same verdict with
    no side effect: it never executes the file, so nothing the repository contains
    is run here either.
    """
    name = "syntax"

    def applies(self, target: Path) -> bool:
        return target.is_dir() or target.suffix.lower() in (".py", ".pyw", ".pyi")

    def check(self, target: Path, timeout: int) -> "Outcome":
        if not self.applies(target):
            return Outcome(SKIPPED, note=L("not a Python file", "это не Python-файл"))
        files = _python_files(target)
        if not files:
            return Outcome(SKIPPED, note=L("no Python file here", "здесь нет Python-файлов"))
        found = []
        for path in files:
            try:
                source = path.read_bytes()
            except OSError as exc:
                found.append(Finding(path=_display(str(path)), line="1", severity="error",
                                     message=L(f"cannot be read: {exc.strerror or exc}",
                                               f"не читается: {exc.strerror or exc}"),
                                     checker=self.name))
                continue
            try:
                compile(source, str(path), "exec")
            except SyntaxError as exc:
                found.append(Finding(
                    path=_display(str(path)), line=str(getattr(exc, "lineno", 1) or 1),
                    col=str(getattr(exc, "offset", "") or ""), severity="error",
                    message=f"{exc.__class__.__name__}: {getattr(exc, 'msg', exc)}",
                    checker=self.name))
            except ValueError as exc:       # a NUL byte, a broken encoding cookie
                found.append(Finding(
                    path=_display(str(path)), line="1", severity="error",
                    message=f"{exc.__class__.__name__}: {exc}", checker=self.name))
        return Outcome(FINDINGS, findings=found) if found else Outcome(CLEAN)


def _python_files(target: Path) -> list:
    """The .py files under a target, with the generated directories left out."""
    skip = {"__pycache__", ".venv", "venv", "node_modules", ".git", "build", "dist",
            ".mypy_cache", ".ruff_cache", ".beeagent"}
    if target.is_file():
        return [target]
    found = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if name.lower().endswith((".py", ".pyw", ".pyi")):
                found.append(Path(root) / name)
            if len(found) >= MAX_SYNTAX_FILES:
                return found
    return found


class RuffChecker(Checker):
    """`python -m ruff check --output-format json` — parsed as JSON, not regex."""
    name = "ruff"
    module = "ruff"

    def applies(self, target: Path) -> bool:
        return target.is_dir() or target.suffix.lower() in (".py", ".pyw", ".pyi")

    def argv(self, target: Path) -> list[str]:
        return [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "json", str(target)]

    def crash_code(self, rc: int) -> bool:
        return rc >= 2          # ruff: 1 = findings, >=2 = operational error

    def parse(self, stdout, stderr, rc, target):
        try:
            data = json.loads(stdout) if stdout.strip() else []
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            loc = item.get("location") or {}
            code = str(item.get("code") or "")
            out.append(Finding(
                path=_display(str(item.get("filename", ""))),
                line=str(loc.get("row", "")), col=str(loc.get("column", "")),
                severity=_ruff_severity(code),
                message=f"{code} {item.get('message', '')}".strip(),
                checker=self.name))
        return out


class PyflakesChecker(Checker):
    """`python -m pyflakes <file>` — the classic `path:line: message` shape."""
    name = "pyflakes"
    module = "pyflakes"

    _RE = re.compile(r'^(?P<path>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s+(?P<msg>\S.*)$')

    def applies(self, target: Path) -> bool:
        return target.is_dir() or target.suffix.lower() in (".py", ".pyw")

    def argv(self, target: Path) -> list[str]:
        return [sys.executable, "-m", "pyflakes", str(target)]

    def crash_code(self, rc: int) -> bool:
        return rc >= 2

    def parse(self, stdout, stderr, rc, target):
        out = []
        for line in (stdout + "\n" + stderr).splitlines():
            m = self._RE.match(line.strip())
            if not m:
                continue
            message = m.group("msg").strip()
            low = message.lower()
            severity = "error" if ("syntax" in low or "undefined" in low) else "warning"
            out.append(Finding(
                path=_display(m.group("path")), line=m.group("line"),
                col=m.group("col") or "", severity=severity,
                message=message, checker=self.name))
        return out


class MyPyChecker(Checker):
    # mypy imports plugin modules named in the repository's own config, so running
    # it is running something the folder chose. Needs the grant.
    needs_grant = True
    """`python -m mypy --no-error-summary` — `path:line: severity: message [code]`."""
    name = "mypy"
    module = "mypy"

    _RE = re.compile(r'^(?P<path>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s+'
                     r'(?P<sev>error|warning|note):\s+(?P<msg>.*)$')

    def applies(self, target: Path) -> bool:
        return target.is_dir() or target.suffix.lower() in (".py", ".pyw", ".pyi")

    def argv(self, target: Path) -> list[str]:
        return [sys.executable, "-m", "mypy", "--no-incremental", "--no-error-summary", str(target)]

    def crash_code(self, rc: int) -> bool:
        return rc >= 2

    def parse(self, stdout, stderr, rc, target):
        out = []
        for line in stdout.splitlines():
            m = self._RE.match(line.strip())
            if not m:
                continue
            out.append(Finding(
                path=_display(m.group("path")), line=m.group("line"),
                col=m.group("col") or "", severity=m.group("sev"),
                message=m.group("msg").strip(), checker=self.name))
        return out


class TscChecker(Checker):
    # tsconfig is read from the repository too; same reasoning as mypy.
    needs_grant = True
    """`npx --no-install tsc --noEmit -p <tsconfig>` for a TypeScript project.

    Runs only when a `tsconfig.json` exists next to the target *and* a tsc is
    already installed. `--no-install` is the whole point: it forbids npx from
    reaching the network to fetch a compiler we are not allowed to add. `-p`
    targets the config directly so the tool never has to change the process cwd.
    """
    name = "tsc"

    _RE = re.compile(r'^(?P<path>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+'
                     r'(?P<sev>error|warning|info)(?: TS(?P<code>\d+))?:\s+'
                     r'(?P<msg>.*)$')

    def _config(self, target: Path):
        base = target if target.is_dir() else target.parent
        cfg = base / "tsconfig.json"
        return cfg if cfg.is_file() else None

    def applies(self, target: Path) -> bool:
        return self._config(target) is not None

    def probe(self, target: Path):
        return _tsc_launcher_available()

    def argv(self, target: Path) -> list[str]:
        npx = shutil.which("npx") or "npx"
        return [npx, "--no-install", "tsc", "--noEmit", "--pretty", "false",
                "-p", str(self._config(target))]

    def crash_code(self, rc: int) -> bool:
        return True         # any non-zero without parseable output is a crash

    def parse(self, stdout, stderr, rc, target):
        out = []
        for line in (stdout + "\n" + stderr).splitlines():
            m = self._RE.match(line.strip())
            if not m:
                continue
            message = m.group("msg").strip()
            code = m.group("code")
            if code:
                message = f"TS{code} {message}".strip()
            out.append(Finding(
                path=_display(m.group("path")), line=m.group("line"),
                col=m.group("col"), severity=m.group("sev"),
                message=message, checker=self.name))
        return out


def _ruff_severity(code: str) -> str:
    """Ruff's JSON has no severity field; derive one a model can triage by.

    Only the classes that break a build or name something undefined are errors;
    style and unused-name lints are warnings, everything else a note. This keeps
    ruff and pyflakes in agreement on the same underlying finding.
    """
    c = code or ""
    if c.startswith(("E9", "F82", "F63", "F7", "F5", "F6")):
        return "error"
    if c[:1] in ("E", "F", "W"):
        return "warning"
    return "note"


def _display(path: str) -> str:
    """Shorten an absolute finding path against the working dir, for readability.

    Forward slashes keep the output identical across Windows and Termux, so the
    model parses one shape no matter where the check ran.
    """
    text = str(path)
    try:
        rel = os.path.relpath(text, working_dir())
    except (ValueError, OSError):
        rel = text
    if rel and not rel.startswith(".."):
        text = rel
    return text.replace("\\", "/")


# The order the task specifies. The syntax checker is first because it is the
# only one guaranteed to exist, so the tool is useful on a bare machine.
CHECKERS: list = [SyntaxChecker(), RuffChecker(), PyflakesChecker(),
                  MyPyChecker(), TscChecker()]


# ---- the tool ---------------------------------------------------------------

class DiagnosticsTool(BaseTool):
    name = "diagnostics"
    description = (
        "Check a file or directory for problems and return them as "
        "`path:line[:col] severity message (checker)` lines. Runs only the "
        "checkers already on this machine (a Python syntax check always; ruff, "
        "pyflakes, mypy and tsc only if installed — nothing is ever downloaded) "
        "and names the ones it could not run. Use it after an edit instead of "
        "re-reading bash output."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "File or directory to check; omit for the working directory"},
            "tool": {"type": "string",
                     "description": "Force one checker: syntax, ruff, pyflakes, mypy, tsc"},
            "timeout": {"type": "integer",
                        "description": f"Seconds per checker (1-{MAX_TIMEOUT})",
                        "default": DEFAULT_TIMEOUT},
        },
        "required": [],
    }
    # Nothing is written any more: the syntax pass compiles in-process, ruff and
    # mypy are told not to cache. It stays `/allow`-free, but the checkers that
    # load a repo's own config (mypy plugins, tsconfig) are skipped until the user
    # grants the tool — so out of the box it reads code and runs none of it.
    writes_files = False

    def is_safe(self) -> bool:
        return True

    def execute(self, path: str = "", tool: str = "", timeout=DEFAULT_TIMEOUT) -> ToolResult:
        seconds = _clamp_timeout(timeout)
        set_granted(bool(getattr(self, "granted", False)))

        raw = path if str(path or "").strip() else "."
        target, refusal = guard(raw, "diagnostics")
        if refusal:
            return ToolResult(output=refusal, error=True,
                              metadata={"refused": "outside-working-directory"})

        checkers = self._select(tool, target)
        if isinstance(checkers, ToolResult):
            return checkers

        outcomes = [c.check(target, seconds) for c in checkers]

        findings, ran_clean, crashed, missing, skipped = [], [], [], [], []
        executed = []
        for checker, outcome in zip(checkers, outcomes):
            if outcome.kind == FINDINGS:
                findings.extend(outcome.findings)
                executed.append(checker.name)
            elif outcome.kind == CLEAN:
                ran_clean.append(checker.name)
                executed.append(checker.name)
            elif outcome.kind == CRASH:
                crashed.append((checker.name, outcome.note))
            elif outcome.kind == MISSING:
                missing.append((checker.name, outcome.note))
            elif outcome.kind == SKIPPED:
                skipped.append(checker.name)

        body, total, dropped = _render_findings(findings)
        lines = [L(f"diagnostics on {_display(str(target)) or target.name or '.'}",
                   f"диагностика: {_display(str(target)) or target.name or '.'}"),
                 _availability_line(executed, crashed, missing, skipped)]
        if body:
            lines.append(body)
        verdict, hard_error = _verdict(total, dropped, ran_clean, crashed, missing,
                                       skipped, bool(findings))
        lines.append(L(f"verdict: {verdict}", f"итог: {verdict}"))

        metadata = {
            "timeout": seconds,
            "executed": executed,
            "clean": ran_clean,
            "findings": total,
            "dropped": dropped,
            "missing": [name for name, _ in missing],
            "skipped": skipped,
            "errors": {name: note for name, note in crashed},
        }
        # A checker that crashed is an error naming it — never a swallowed warning.
        return ToolResult(output="\n".join(lines), error=hard_error, metadata=metadata)

    def _select(self, tool: str, target: Path):
        names = [c.name for c in CHECKERS]
        wanted = str(tool or "").strip().lower()
        if not wanted:
            return list(CHECKERS)
        if wanted not in names:
            return ToolResult(
                output=L(f"diagnostics: no checker named `{tool}`. Available checkers: "
                         f"{', '.join(names)}. Omit `tool` to run every one that is installed.",
                         f"diagnostics: нет проверки `{tool}`. Доступные проверки: "
                         f"{', '.join(names)}. Уберите `tool`, чтобы запустить все "
                         f"установленные."),
                error=True, metadata={"available": names})
        return [c for c in CHECKERS if c.name == wanted]

def _clamp_timeout(value) -> int:
    """Fit the model's `timeout` into [MIN, MAX]; a bad value is the default.

    A string, `null`, `0`, a negative or a 10**12 all arrived from the model at
    one point; none should disable the timeout or fire the instant a checker
    starts.
    """
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if seconds <= 0:
        return DEFAULT_TIMEOUT
    return max(MIN_TIMEOUT, min(seconds, MAX_TIMEOUT))


def _render_findings(findings: list) -> tuple[str, int, int]:
    """Group findings by severity, cap the list, and say how many were dropped."""
    order = {"error": 0, "warning": 1, "note": 2, "info": 2, "": 3}
    findings = sorted(findings, key=lambda f: (order.get(f.severity, 3),
                                               f.path, _as_int(f.line)))
    total = len(findings)
    shown = findings[:MAX_FINDINGS_SHOWN]
    dropped = total - len(shown)
    blocks, counts = [], {}
    for finding in shown:
        counts.setdefault(finding.severity or "note", []).append(finding)
    for sev in ("error", "warning", "note", ""):
        group = counts.get(sev)
        if not group:
            continue
        label = {"error": L("errors", "ошибки"), "warning": L("warnings", "предупреждения"),
                 "note": L("notes", "заметки"), "": L("other", "прочее")}[sev]
        blocks.append(f"{label} ({len(group)}):")
        blocks.extend(f"  {f.render()}" for f in group)
    body = "\n".join(blocks)
    if dropped:
        body += "\n" + L(f"… {dropped} more finding(s) not shown "
                         f"({total} total)",
                         f"… ещё {dropped} замечаний не показано (всего {total})")
    return body, total, dropped


def _availability_line(ran, crashed, missing, skipped) -> str:
    """What ran, what broke, what was absent — always printed, never hidden."""
    parts = [f"ran[{', '.join(ran) or '-'}]"]
    if crashed:
        parts.append("errored[" + ", ".join(
            f"{name}: {note}" for name, note in crashed) + "]")
    if missing:
        parts.append("missing[" + ", ".join(
            f"{name} ({note})" if note else name for name, note in missing) + "]")
    if skipped:
        parts.append("skipped[" + ", ".join(skipped) + "]")
    return "checkers: " + " ".join(parts)


def _verdict(total, dropped, ran, crashed, missing, skipped, has_findings):
    """One line that cannot be misread as a clean bill of health.

    Returns (text, hard_error). `hard_error` is True when the run is not a
    trustworthy verdict at all — a checker crashed, or nothing ran.
    """
    if has_findings and total > 0:
        text = L(f"{total} problem(s) found", f"найдено проблем: {total}")
        if dropped:
            text += L(f" ({dropped} more not shown)", f" (ещё {dropped} не показано)")
        if crashed:
            text += L(f"; {', '.join(n for n, _ in crashed)} crashed — that code was "
                      f"NOT verified",
                      f"; {', '.join(n for n, _ in crashed)} упала — этот код НЕ проверен")
        return text, bool(crashed)

    if not ran:
        # Nothing produced a verdict: this must never read as "fine".
        if crashed:
            return L(f"COULD NOT CHECK: {', '.join(n for n, _ in crashed)} errored, "
                     f"no checker returned a result",
                     f"НЕ ПРОВЕРЕНО: {', '.join(n for n, _ in crashed)} упала, "
                     f"ни одна проверка не дала результата"), True
        if missing or skipped:
            absent = [n for n, _ in missing] + list(skipped)
            return L(f"NOT CHECKED: no checker was available to run on this machine "
                     f"(missing/skipped: {', '.join(absent)}). This is NOT a clean "
                     f"result — the code was never examined.",
                     f"НЕ ПРОВЕРЕНО: на этой машине не нашлось ни одной доступной "
                     f"проверки (отсутствуют/пропущены: {', '.join(absent)}). Это НЕ "
                     f"значит, что код чист, — его не проверяли."), True
        return L("NOT CHECKED: no checker ran", "НЕ ПРОВЕРЕНО: ни одна проверка не "
                 "выполнена"), True

    text = L("no problems found", "проблем не найдено")
    suffix = ""
    if crashed:
        suffix += L(f"; {', '.join(n for n, _ in crashed)} crashed and was not verified",
                    f"; {', '.join(n for n, _ in crashed)} упала и не проверена")
    if missing:
        suffix += L(f"; not installed here: {', '.join(n for n, _ in missing)}",
                    f"; не установлены: {', '.join(n for n, _ in missing)}")
    return text + suffix, bool(crashed)
