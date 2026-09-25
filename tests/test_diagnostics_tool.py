"""The diagnostics tool: honest verdicts, structured findings, nothing installed.

Everything except the two stdlib runs uses fake checkers monkeypatched into
`DiagnosticsTool`'s registry, because the whole point of the tool is that it
behaves correctly when a checker is *absent* — and this suite has to pass whether
or not ruff/mypy/tsc happen to be on the box. The syntax checker is exercised for
real (it is stdlib and always present): `python -m py_compile` on a broken file
must come back with the right line.

The bug class under test throughout is the dangerous confusion: "no problems"
versus "nobody looked". A run with no checker available must never read as clean.
"""
import os
import re
from pathlib import Path

import pytest

from beeagent import i18n
from beeagent.tools import diagnostics as D
from beeagent.tools.diagnostics import DiagnosticsTool


@pytest.fixture(autouse=True)
def _cold_probes():
    """Each runner is cached per process; start every test from a cold cache."""
    D._reset_probe_cache()
    yield
    D._reset_probe_cache()


# ---- fake checkers ----------------------------------------------------------

def _fake(name, kind, findings=(), note="", seen=None):
    """A checker that returns a canned outcome without launching anything."""
    def run(target, timeout):
        if seen is not None:
            seen.append(timeout)
        return D.Outcome(kind, findings=list(findings), note=note)
    return D.Checker(name=name, applies=lambda target: True,
                     probe=lambda target: (True, ""), run=run)


def _missing(name, note):
    """A checker that reports itself unavailable — the way an uninstalled one does."""
    return D.Checker(name=name, applies=lambda target: True,
                     probe=lambda target: (False, note),
                     run=lambda target, timeout: pytest.fail(
                         f"{name} was absent but ran anyway"))


def _finding(path="f.py", line="1", col="", severity="error", message="boom", checker="syntax"):
    return D.Finding(path=path, line=line, col=col, severity=severity,
                     message=message, checker=checker)


# ---- the real stdlib syntax check ------------------------------------------

def test_real_syntax_error_comes_back_with_the_right_line(tmp_path, monkeypatch):
    """`python -m py_compile` for real: a broken file must name the file and line."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "oops.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(D, "CHECKERS", [D.SyntaxChecker()])

    result = DiagnosticsTool().execute("oops.py")

    assert not result.error, result.output
    assert re.search(r"oops\.py:1\b", result.output), result.output
    assert "(syntax)" in result.output
    assert result.metadata["findings"] >= 1
    # It found a problem, so it must NOT claim the file is clean.
    assert "no problems found" not in result.output
    assert "problem" in result.output


def test_real_clean_file_reports_no_problems(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fine.py").write_text("x = 1\ny = [i for i in range(3)]\n", encoding="utf-8")
    monkeypatch.setattr(D, "CHECKERS", [D.SyntaxChecker()])

    result = DiagnosticsTool().execute("fine.py")

    assert not result.error, result.output
    assert "no problems found" in result.output
    assert result.metadata["findings"] == 0
    assert result.metadata["clean"] == ["syntax"]


def test_real_compileall_walks_a_directory(tmp_path, monkeypatch):
    """The directory form uses `compileall`, which reports nested bad files."""
    monkeypatch.chdir(tmp_path)
    nested = tmp_path / "pkg" / "sub"
    nested.mkdir(parents=True)
    (nested / "bad.py").write_text("def g(:\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(D, "CHECKERS", [D.SyntaxChecker()])

    result = DiagnosticsTool().execute("pkg")

    assert not result.error
    assert re.search(r"sub/bad\.py:1\b", result.output.replace("\\", "/")), result.output


# ---- absence must never read as success ------------------------------------

def test_a_missing_checker_is_listed_as_missing_not_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(D, "CHECKERS", [_missing("ruff", "module `ruff` is not installed")])

    result = DiagnosticsTool().execute("any.py")

    out = result.output
    assert result.error, out                       # nothing ran -> not a verdict
    assert "missing[ruff" in out
    assert "no problems found" not in out
    assert "NOT CHECKED" in out


def test_every_checker_missing_says_so_explicitly(tmp_path, monkeypatch):
    """The single worst failure: reporting a green light nobody earned."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(D, "CHECKERS", [
        _missing("ruff", "not installed"),
        _missing("mypy", "not installed"),
    ])

    result = DiagnosticsTool().execute("any.py")

    assert result.error
    assert "no checker was available to run" in result.output
    assert "never examined" in result.output
    assert "no problems found" not in result.output
    # Both absent checkers are named, so the user knows what to install.
    assert "ruff" in result.output and "mypy" in result.output


# ---- a checker that explodes is an error naming it --------------------------

def test_a_crashed_checker_is_reported_as_a_crash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    # syntax runs for real and is clean; ruff is faked to blow up.
    monkeypatch.setattr(D, "CHECKERS",
                        [D.SyntaxChecker(), _fake("ruff", D.CRASH, note="exited 2: internal panic")])

    result = DiagnosticsTool().execute("any.py")

    assert result.error, result.output
    assert "errored[ruff" in result.output
    assert "ruff" in result.output and "crash" in result.output.lower()
    assert result.metadata["errors"] == {"ruff": "exited 2: internal panic"}
    # A clean syntax result must not be reported as a full green light.
    assert "was not verified" in result.output


def test_nonzero_exit_without_findings_is_a_finding_not_a_pass(tmp_path, monkeypatch):
    """A checker that exits non-zero but says nothing we parsed is still surfaced."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(D, "CHECKERS", [D.RuffChecker()])
    monkeypatch.setattr(D, "_module_available", lambda name: True)
    monkeypatch.setattr(D, "run_argv_text",
                        lambda argv, timeout=60: ("", "some error happened", 1))

    result = DiagnosticsTool().execute("any.py")

    assert not result.error                      # a finding, not a tool error
    assert "problem" in result.output
    assert "no problems found" not in result.output
    assert "(ruff)" in result.output


# ---- the third-party parsers run for real (their output, not a subprocess) --

def test_ruff_output_is_parsed_as_json_not_regex(tmp_path, monkeypatch):
    """A message full of colons/quotes must not confuse a proper JSON parse."""
    import json
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    payload = json.dumps([{
        "code": "E501",
        "message": 'line too long (120 > 79): use "quotes"',
        "filename": "a/b.py",
        "location": {"row": 42, "column": 80},
    }])
    monkeypatch.setattr(D, "_module_available", lambda name: True)
    monkeypatch.setattr(D, "run_argv_text", lambda argv, timeout=60: (payload, "", 1))
    monkeypatch.setattr(D, "CHECKERS", [D.RuffChecker()])

    result = DiagnosticsTool().execute("any.py")

    assert not result.error
    assert "a/b.py:42:80 warning E501" in result.output
    assert "(ruff)" in result.output


def test_ruff_operational_error_is_a_crash_not_a_finding(tmp_path, monkeypatch):
    """ruff exits 2 on a bad config: that is an error naming it, not 'no problems'."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(D, "_module_available", lambda name: True)
    monkeypatch.setattr(D, "run_argv_text",
                        lambda argv, timeout=60: ("", "error: invalid TOML", 2))
    monkeypatch.setattr(D, "CHECKERS", [D.RuffChecker()])

    result = DiagnosticsTool().execute("any.py")

    assert result.error
    assert "errored[ruff" in result.output
    assert "no problems found" not in result.output


@pytest.mark.parametrize("checker,stdout,expect", [
    ("pyflakes", "foo.py:4: 'os' imported but unused\n", "foo.py:4 warning"),
    ("mypy", "app.py:12: error: bad type  [assignment]\n", "app.py:12 error"),
])
def test_classic_text_parsers(checker, stdout, expect, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(D, "_module_available", lambda name: True)
    monkeypatch.setattr(D, "run_argv_text", lambda argv, timeout=60: (stdout, "", 1))
    obj = {"pyflakes": D.PyflakesChecker, "mypy": D.MyPyChecker}[checker]()
    monkeypatch.setattr(D, "CHECKERS", [obj])

    result = DiagnosticsTool().execute("any.py")

    assert expect in result.output, result.output


# ---- path handling ----------------------------------------------------------

def test_path_traversal_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ran = []
    monkeypatch.setattr(D, "CHECKERS", [_fake("syntax", D.CLEAN, seen=ran)])
    outside = os.path.abspath(os.path.join(os.sep, ".."))   # one level above the drive root

    result = DiagnosticsTool().execute(outside)

    assert result.error
    assert "refused" in result.output.lower() or "outside" in result.output.lower()
    assert not ran, "a refused path must never reach a checker"


def test_relative_parent_escape_is_refused(tmp_path, monkeypatch):
    """`../..` walking out of every root is refused before any checker runs."""
    monkeypatch.chdir(tmp_path)
    ran = []
    monkeypatch.setattr(D, "CHECKERS", [_fake("syntax", D.CLEAN, seen=ran)])
    deep_escape = os.path.join(*([".."] * 12))   # climbs past the working and temp roots

    result = DiagnosticsTool().execute(deep_escape)

    assert result.error
    assert "refused" in result.output.lower() or "outside" in result.output.lower()
    assert not ran, "a refused path must never reach a checker"


# ---- capping ----------------------------------------------------------------

def test_huge_output_is_capped_and_says_how_many_were_dropped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    many = [_finding(path=f"f{i}.py", line=str(i)) for i in range(250)]
    monkeypatch.setattr(D, "MAX_FINDINGS_SHOWN", 50)
    monkeypatch.setattr(D, "CHECKERS", [_fake("syntax", D.FINDINGS, findings=many)])

    result = DiagnosticsTool().execute("any.py")

    assert result.metadata["findings"] == 250
    assert result.metadata["dropped"] == 200
    assert "200 more" in result.output
    # Errors sort first, so what is shown is the important half, and the count is
    # honest: 50 rendered finding lines, not 250.
    assert len(re.findall(r"\(syntax\)\n?", result.output)) == 50


# ---- forcing one checker ----------------------------------------------------

def test_forcing_an_absent_tool_refuses_with_a_clear_sentence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(D, "_module_available", lambda name: False)   # ruff absent

    result = DiagnosticsTool().execute("any.py", tool="ruff")

    assert result.error
    assert "NOT CHECKED" in result.output and "ruff" in result.output
    assert "no problems found" not in result.output


def test_forcing_an_unknown_tool_names_the_valid_ones(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")

    result = DiagnosticsTool().execute("any.py", tool="clang")

    assert result.error
    assert "no checker named" in result.output
    assert "syntax" in result.output and "ruff" in result.output


def test_forcing_a_present_tool_runs_only_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    seen = []
    monkeypatch.setattr(D, "CHECKERS", [
        D.SyntaxChecker(),
        _fake("ruff", D.FINDINGS, findings=[_finding(message="unused", checker="ruff")], seen=seen),
    ])

    result = DiagnosticsTool().execute("any.py", tool="ruff")

    assert seen, "the forced checker ran"
    assert "(ruff)" in result.output
    # Only ruff ran, so the syntax checker is absent from the report entirely.
    assert "syntax" not in result.output


# ---- timeout clamping -------------------------------------------------------

@pytest.mark.parametrize("given,expected", [
    ("soon", D.DEFAULT_TIMEOUT),      # un-parseable -> default
    (0, D.DEFAULT_TIMEOUT),           # zero/negative -> default, not instant
    (-5, D.DEFAULT_TIMEOUT),
    (None, D.DEFAULT_TIMEOUT),
    (10 ** 9, D.MAX_TIMEOUT),         # huge -> clamped to the ceiling
    (7, 7),                           # sane value passes through
    (5000, D.MAX_TIMEOUT),
])
def test_timeout_is_clamped(given, expected):
    assert D._clamp_timeout(given) == expected


def test_execute_passes_the_clamped_timeout_to_checkers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    seen = []
    monkeypatch.setattr(D, "CHECKERS", [_fake("syntax", D.CLEAN, seen=seen)])

    result = DiagnosticsTool().execute("any.py", timeout=999999)

    assert seen == [D.MAX_TIMEOUT]
    assert result.metadata["timeout"] == D.MAX_TIMEOUT


# ---- flags and i18n ---------------------------------------------------------

def test_flags_require_a_grant_and_are_honest_about_the_disk():
    tool = DiagnosticsTool()
    # It runs programs, so it needs /allow; and py_compile/compileall/mypy leave
    # __pycache__/ .mypy_cache__ bytes behind, so writes_files is the honest True.
    assert tool.is_safe() is False
    assert tool.writes_files is True


def test_verdict_language_follows_i18n(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(D, "CHECKERS", [_missing("ruff", "no")])
    monkeypatch.setattr(i18n, "_lang", "ru")

    result = DiagnosticsTool().execute("any.py")

    assert "НЕ ПРОВЕРЕНО" in result.output
    assert "no problems found" not in result.output


# ---- the checker set is complete and ordered as specified ------------------

def test_checker_registry_order_and_names():
    names = [c.name for c in D.CHECKERS]
    assert names == ["syntax", "ruff", "pyflakes", "mypy", "tsc"]


def test_tsc_never_installs_and_skips_without_a_tsconfig(tmp_path, monkeypatch):
    """No tsconfig -> tsc is skipped; it must not spawn npx to fetch a compiler."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    called = []

    def boom(argv, timeout=60):
        called.append(argv)
        raise AssertionError("tsc must not run when there is no tsconfig.json")

    monkeypatch.setattr(D, "run_argv_text", boom)
    monkeypatch.setattr(D, "CHECKERS", [_fake("syntax", D.CLEAN), D.TscChecker()])

    result = DiagnosticsTool().execute("any.py")

    assert called == [], "npx was launched for a non-TS project"
    assert "skipped[tsc" in result.output


def test_tsc_runs_with_no_install_and_parses(tmp_path, monkeypatch):
    """When a TS project IS present, tsc runs with --no-install and its findings parse."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "any.py").write_text("x = 1\n", encoding="utf-8")
    launched = []

    def fake_run(argv, timeout=60):
        launched.append(list(argv))
        return ("src/a.ts(3,7): error TS2304: Cannot find name 'foo'\n", "", 2)

    monkeypatch.setattr(D, "run_argv_text", fake_run)
    monkeypatch.setattr(D, "_tsc_launcher_available", lambda: (True, ""))
    monkeypatch.setattr(D, "CHECKERS", [_fake("syntax", D.CLEAN), D.TscChecker()])

    result = DiagnosticsTool().execute("any.py")

    assert launched, "tsc should have run for a project with a tsconfig.json"
    argv = launched[0]
    assert "--no-install" in argv, "npx must never be allowed to download a compiler"
    assert "--noEmit" in argv
    assert result.metadata["findings"] >= 1
    assert "src/a.ts:3:7" in result.output and "TS2304" in result.output
    assert "(tsc)" in result.output
