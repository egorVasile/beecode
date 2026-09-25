"""The git tool's wall: a parsed argv, and proofs on disk that the guess fails.

The audit's finding was that `git` is a grant to run arbitrary executables as
long as the filter looks at the joined string: `git -c alias.pwn='!python
pwn.py' pwn` holds its metacharacters inside git's own value, clears a deny-list
of shell shapes, and git spawns a shell for the alias. So every rejected form
here is asserted twice — the refusal names the exact token, and a snapshot of
the filesystem proves nothing happened. Every allowed form is run for real.

These drive the actual tool against the actual git binary inside throwaway
repositories under tmp_path; the project's own repository is never involved.
"""
import os
import shutil
import subprocess

import pytest

from beeagent import i18n
from beeagent.tools import _seen
from beeagent.tools import git as git_module
from beeagent.tools.git import GitTool, _harden, split_command


def _git(*args, cwd, env=None):
    """Setup only: the fixtures need `-c`, aliases and bare repos of their own.

    An identity is passed in because the template repository is built before any
    test's fake `$HOME` is in place — without it `git commit` fails with "please
    tell me who you are" and the fixture quietly ends up with no history.
    """
    full = dict(os.environ)
    full.update({"GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
    full.update(env or {})
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, env=full)


def _bare(path):
    """A bare repository to push to: git wants the path, not a missing cwd."""
    return subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)],
                          capture_output=True)


def _tree(root):
    """Every file under a directory, contents included: what a refusal protects."""
    out = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        key = str(path.relative_to(root))
        out[key] = path.read_bytes() if path.is_file() else "<dir>"
    return out


@pytest.fixture(autouse=True)
def _english():
    i18n.set_lang("en")
    yield
    i18n.set_lang("en")


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """Two committed repositories, built once: `git` costs a second per call here.

    A copy of a small repository is milliseconds, and no test is allowed to see
    another one's commits, so each test copies this and works on its own.
    """
    root = tmp_path_factory.mktemp("git-template")
    work = root / "repo"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "a.txt").write_text("committed\n", encoding="utf-8")
    assert _git("add", "-A", cwd=work).returncode == 0
    assert _git("commit", "-qm", "base", cwd=work).returncode == 0
    (work / "a.txt").write_text("committed\nsecond line\n", encoding="utf-8")
    assert _git("add", "-A", cwd=work).returncode == 0
    assert _git("commit", "-qm", "second", cwd=work).returncode == 0
    # two commits, so HEAD~1 and a real undo have something to talk about
    assert _git("rev-list", "--count", "HEAD", cwd=work).stdout.strip() == b"2"
    # The working tree is dirty on purpose: it is the thing a refused
    # `reset --hard` or `checkout -- .` must leave alone.
    (work / "a.txt").write_text("committed\n", encoding="utf-8")
    # A script an alias could point at: if the marker appears, the wall failed.
    (work / "pwn.py").write_text(
        "import os\n"
        "open(os.path.join(os.environ['SANDBOX_MARKER']), 'w').write('ran by git')\n",
        encoding="utf-8")

    other = root / "other_repo"
    other.mkdir()
    _git("init", "-q", "-b", "main", cwd=other)
    (other / "b.txt").write_text("committed there\n", encoding="utf-8")
    assert _git("add", "-A", cwd=other).returncode == 0
    assert _git("commit", "-qm", "base", cwd=other).returncode == 0
    # The user's work over there: one edit, one untracked file.
    (other / "b.txt").write_text("THE USER'S UNCOMMITTED WORK\n", encoding="utf-8")
    (other / "scratch").mkdir()
    (other / "scratch" / "notes.md").write_text("do not delete me\n", encoding="utf-8")
    return root


@pytest.fixture
def repo(tmp_path, monkeypatch, template):
    """A repository to work in, a second one to attack, and a fake home.

    The second repository is the point of several tests: `--git-dir`/`--work-tree`
    is how one git call reaches another project's files, and the tool is run from
    the first one.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "gitconfig").write_text(
        "[user]\n\tname = Tester\n\temail = tester@example.invalid\n", encoding="utf-8")
    # The child inherits the environment, so a global config outside the sandbox —
    # someone's real aliases, signer, hooks — must not be reachable either.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(home / "no-such-config"))

    work = tmp_path / "repo"
    other = tmp_path / "other_repo"
    shutil.copytree(template / "repo", work)
    shutil.copytree(template / "other_repo", other)

    marker = tmp_path / "MARKER"
    monkeypatch.setenv("SANDBOX_MARKER", str(marker))

    _seen.STAMPS.clear()
    git_module._STAGED.clear()
    monkeypatch.chdir(work)
    return {"work": work, "other": other, "home": home, "marker": marker,
            "tmp": tmp_path, "git": GitTool()}


HEAD = "committed\nsecond line\n"     # what the last commit holds
EDIT = "the user's edit\n"            # what the user changed it to


def refused(result, *tokens):
    """A refusal that names the token it rejected, as every one below must."""
    assert result.error, result.output
    assert result.output.startswith("refused"), result.output
    named = result.output.split("—")[0]
    assert any(token in named for token in tokens), f"{named!r} names none of {tokens}"


# --------------------------------------------------------------------------
# 1. the grant: git must not become a way to run programs
# --------------------------------------------------------------------------

def test_dash_c_with_an_alias_never_reaches_a_shell(repo):
    """The audit's reproduction, in both argv shapes a model sends."""
    for command, tokens in (("-c alias.pwn='!python pwn.py' pwn", ("-c",)),
                            ("pwn -c alias.pwn='!python pwn.py'", ("-c", "pwn")),
                            ("status -c alias.pwn='!python pwn.py'", ("-c",)),
                            ("-c core.pager='!python pwn.py' log", ("-c",))):
        refused(repo["git"].execute(command), *tokens)
    assert not repo["marker"].exists(), "a shell ran the alias"


def test_repo_own_alias_is_refused_without_any_config_flag(repo):
    """A clone can ship the alias in its own config; the word alone is no grant."""
    _git("config", "alias.pwn", "!python pwn.py", cwd=repo["work"])
    result = repo["git"].execute("pwn")
    refused(result, "pwn")
    assert "not a git command this tool was written to run" in result.output
    assert not repo["marker"].exists()


def test_config_cannot_write_an_alias(repo):
    refused(repo["git"].execute("config alias.boom '!python pwn.py'"), "alias.boom")
    assert not repo["marker"].exists()
    assert "alias.boom" not in _git("config", "--list", cwd=repo["work"]).stdout.decode()


def test_metacharacter_hidden_in_quoting_is_still_refused(repo):
    """shlex drops the quotes; the parser then reads `alias.pwn=!python pwn.py`."""
    with pytest.raises(ValueError) as info:
        split_command('status -c "alias.pwn=!python pwn.py"')
    assert str(info.value).split("—")[0].count("-c") >= 1, str(info.value)
    with pytest.raises(ValueError) as info:
        split_command('commit -c "alias.pwn=!python pwn.py"')
    assert "!" in str(info.value), str(info.value)
    assert not repo["marker"].exists()


def test_config_cannot_write_outside_the_repository(repo):
    victim = repo["home"] / "gitconfig"
    before = victim.read_text(encoding="utf-8")
    refused(repo["git"].execute(
        f"config --file {victim.as_posix()} core.askpass calc"), "--file")
    assert victim.read_text(encoding="utf-8") == before
    refused(repo["git"].execute("config --global core.editor evil"), "--global")


def test_flags_that_name_a_program_are_refused(repo):
    for command, token in (
            ("fetch --upload-pack=/tmp/evil origin", "--upload-pack"),
            ("fetch -u /bin/sh origin", "-u"),
            ("remote add origin ext::sh -c whoami", "ext::sh"),
            ("clone --template=/tmp/hooks https://x.invalid/y.git", "--template"),
            ("init --template=/tmp/hooks", "--template"),
            ("push --exec=/bin/sh origin", "--exec"),
            ("diff --output=/tmp/x", "--output")):
        refused(repo["git"].execute(command), token)


def test_the_repository_cannot_be_told_to_run_a_converter(repo):
    """diff.external, textconv and the pager are all command lines from config."""
    for command, token in (("diff --ext-diff", "--ext-diff"),
                           ("log -p --textconv", "--textconv"),
                           ("log --paginate", "--paginate")):
        refused(repo["git"].execute(command), token)


# --------------------------------------------------------------------------
# 2. another repository is not this one
# --------------------------------------------------------------------------

def test_git_dir_and_work_tree_cannot_revert_another_repo(repo):
    other = repo["other"]
    before = _tree(other)
    for command in (f"--git-dir {other.as_posix()}/.git --work-tree "
                    f"{other.as_posix()} checkout -- .",
                    f"checkout --git-dir={other.as_posix()}/.git "
                    f"--work-tree={other.as_posix()} -- ."):
        refused(repo["git"].execute(command), "--git-dir")
    assert _tree(other) == before
    assert "UNCOMMITTED" in (other / "b.txt").read_text(encoding="utf-8")


def test_run_as_if_started_elsewhere_is_refused(repo):
    before = _tree(repo["other"])
    refused(repo["git"].execute(f"-C {repo['other'].as_posix()} status"), "-C")
    refused(repo["git"].execute(f"status -C {repo['other'].as_posix()}"), "-C")
    assert _tree(repo["other"]) == before


def test_clean_cannot_delete_anyones_files(repo):
    before = _tree(repo["other"])
    refused(repo["git"].execute(f"--git-dir {repo['other'].as_posix()}/.git "
                                f"--work-tree {repo['other'].as_posix()} clean -fdx"),
            "--git-dir")
    assert _tree(repo["other"]) == before

    refused(repo["git"].execute("clean -fdx"), "-f")
    refused(repo["git"].execute("clean -d"), "clean")
    refused(repo["git"].execute("clean"), "clean")
    assert (repo["other"] / "scratch" / "notes.md").exists()


# --------------------------------------------------------------------------
# 3. destroying the user's own work stays the user's decision
# --------------------------------------------------------------------------

def test_reset_hard_is_refused_and_history_is_intact(repo):
    head = _git("rev-parse", "HEAD", cwd=repo["work"]).stdout
    refused(repo["git"].execute("reset --hard HEAD~1"), "--hard")
    refused(repo["git"].execute("checkout --hard main"), "--hard")
    assert _git("rev-parse", "HEAD", cwd=repo["work"]).stdout == head


def test_whole_tree_undo_is_refused(repo):
    (repo["work"] / "a.txt").write_text(EDIT, encoding="utf-8")
    for command, token in (("checkout -- .", "."), ("restore .", "."),
                           ("checkout .", "."), ("restore -- .", "."),
                           ("rm -r .", "."), ("reset --hard .", "--hard")):
        refused(repo["git"].execute(command), token)
    assert (repo["work"] / "a.txt").read_text(encoding="utf-8") == EDIT


def test_restore_undoes_only_what_this_session_changed(repo):
    """The agent may undo its own file; a path nobody here opened is refused."""
    (repo["work"] / "a.txt").write_text(EDIT, encoding="utf-8")
    result = repo["git"].execute("restore a.txt")
    refused(result, "a.txt")
    assert "never opened or staged" in result.output
    assert (repo["work"] / "a.txt").read_text(encoding="utf-8") == EDIT

    assert not repo["git"].execute("add a.txt").error       # now the session owns it
    assert not repo["git"].execute("restore --source=HEAD -- a.txt").error
    assert (repo["work"] / "a.txt").read_text(encoding="utf-8") == HEAD


def test_a_file_a_read_tool_showed_us_is_mine_to_undo(repo):
    """`read` remembers the file, which is what makes it ours to undo."""
    (repo["work"] / "a.txt").write_text(EDIT, encoding="utf-8")
    _seen.remember(repo["work"] / "a.txt")        # what read.py does after a read
    assert not repo["git"].execute("restore a.txt").error
    # `restore` with no --source reads the index, and the index still holds HEAD
    assert (repo["work"] / "a.txt").read_text(encoding="utf-8") == HEAD


def test_checkout_naming_a_file_on_disk_is_a_restore(repo):
    """`git checkout HEAD~1 a.txt` reads like a branch and is not one."""
    (repo["work"] / "a.txt").write_text(EDIT, encoding="utf-8")
    refused(repo["git"].execute("checkout HEAD~1 a.txt"), "a.txt")
    assert (repo["work"] / "a.txt").read_text(encoding="utf-8") == EDIT


def test_unstaging_is_not_undoing_the_users_work(repo):
    """`rm --cached` and `restore --staged` move the index and stop there."""
    (repo["work"] / "a.txt").write_text(EDIT, encoding="utf-8")
    assert not repo["git"].execute("add a.txt").error
    assert not repo["git"].execute("restore --staged a.txt").error
    assert not repo["git"].execute("rm --cached a.txt").error
    assert (repo["work"] / "a.txt").read_text(encoding="utf-8") == EDIT


def test_force_push_and_deleting_refs_are_refused(repo):
    remote = repo["tmp"] / "remote.git"
    _bare(remote)
    assert not repo["git"].execute(f"remote add origin {remote.as_posix()}").error
    pushed = repo["git"].execute("push -u origin main")
    assert not pushed.error, pushed.output
    first = _git("rev-parse", "refs/heads/main", cwd=remote).stdout

    _git("commit", "-q", "--amend", "-m", "rewritten", cwd=repo["work"])
    for command, token in (("push --force", "--force"), ("push -f origin main", "-f"),
                           ("push --force-with-lease", "--force-with-lease"),
                           ("push --delete origin main", "--delete")):
        refused(repo["git"].execute(command), token)
    assert _git("rev-parse", "refs/heads/main", cwd=remote).stdout == first


def test_branch_and_tag_deletion_are_refused(repo):
    for command, token in (("branch -D old", "-D"), ("tag -d v1", "-d"),
                           ("branch -M x y", "-M"), ("switch -C main", "-C")):
        refused(repo["git"].execute(command), token)


def test_history_rewriting_and_strangers_code_are_refused(repo):
    for command, token in (("rebase -i main", "rebase"),
                           ("filter-branch -f", "filter-branch"),
                           ("worktree add ../../../evil", "add"),
                           ("stash drop", "drop"), ("stash pop", "pop"),
                           ("gc --prune=now", "gc"), ("apply patch.diff", "apply"),
                           ("submodule update --init", "submodule"),
                           ("credential fill", "credential"),
                           ("commit --no-verify -m x", "--no-verify"),
                           ("push --no-verify", "--no-verify"),
                           ("add -p a.txt", "-p")):
        refused(repo["git"].execute(command), token)


# --------------------------------------------------------------------------
# 4. the workflow the user asked for still runs
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "status", "status -s", "status --porcelain=v1", "diff", "diff HEAD~1",
    "log --oneline", "log -n 3", "log --pretty=format:%h", "show --stat HEAD",
    "branch -v", "branch --show-current", "remote -v", "stash list",
    "blame a.txt", "grep -n committed", "ls-files", "rev-parse HEAD",
    "config --get user.name", "config --list", "describe --always",
    "clean -nd", "worktree list", "symbolic-ref --short HEAD",
])
def test_reading_commands_still_run(repo, command):
    result = repo["git"].execute(command)
    assert not result.error, f"{command}: {result.output}"
    assert not result.output.startswith("refused")


def test_the_commit_and_push_a_user_asks_for(repo):
    (repo["work"] / "a.txt").write_text("committed\nfixed\n", encoding="utf-8")
    assert not repo["git"].execute("add a.txt").error
    assert "a.txt" in _git("diff", "--cached", "--name-only",
                           cwd=repo["work"]).stdout.decode()

    committed = repo["git"].execute("commit -m 'fix: a second line'")
    assert not committed.error, committed.output
    assert _git("log", "-1", "--pretty=%s", cwd=repo["work"]).stdout.decode() == \
        "fix: a second line\n"

    remote = repo["tmp"] / "push.git"
    _bare(remote)
    assert not repo["git"].execute(f"remote add origin {remote.as_posix()}").error
    pushed = repo["git"].execute("push -u origin main")
    assert not pushed.error, pushed.output
    assert _git("log", "-1", "--pretty=%s", cwd=remote).stdout.decode().strip() == \
        "fix: a second line"


def test_branches_tags_and_stashes_the_user_names(repo):
    assert not repo["git"].execute("switch -c feature/x").error
    assert "feature/x" in _git("branch", "--list", cwd=repo["work"]).stdout.decode()
    assert not repo["git"].execute("checkout main").error
    assert not repo["git"].execute("tag -a v1 -m 'release: 1.0'").error
    assert not repo["git"].execute("stash push -m 'my work'").error
    assert not repo["git"].execute("stash list").error
    assert not repo["git"].execute("merge main").error


def test_a_message_keeps_its_punctuation_and_everything_else_does_not(repo):
    """`feat!:` and `a; b` are prose inside `-m`; anywhere else they are syntax."""
    empty = repo["git"].execute("commit --allow-empty -m 'feat!: a breaking; change'")
    assert not empty.error, empty.output
    assert _git("log", "-1", "--pretty=%s", cwd=repo["work"]).stdout.decode() == \
        "feat!: a breaking; change\n"
    for command in ("log --format=!x", "grep '!x'", "commit -m x --author=!y"):
        result = repo["git"].execute(command)
        assert result.error and "!" in result.output, (command, result.output)


def test_the_same_letter_means_different_things_per_subcommand(repo):
    """`commit -s` signs; `restore -s <tree>` names a source to undo to."""
    assert not repo["git"].execute("add a.txt").error
    assert not repo["git"].execute("commit -s -m 'feat!: signed'").error
    message = _git("log", "-1", "--pretty=%s", cwd=repo["work"]).stdout.decode().strip()
    assert message == "feat!: signed"
    assert "Signed-off-by" in _git("log", "-1", "--pretty=%B",
                                   cwd=repo["work"]).stdout.decode()
    assert not repo["git"].execute("restore -s HEAD~1 -- a.txt").error


def test_a_newline_asks_for_two_commands(repo):
    result = repo["git"].execute("status\nlog")
    assert result.error and "newline" in result.output


# --------------------------------------------------------------------------
# 5. the parser, and what the model is told
# --------------------------------------------------------------------------

REJECTED = [
    "-c core.pager=cat status", "status -c core.pager=cat", "-c",
    "--git-dir x status", "--work-tree=x checkout main", "--exec-path /tmp status",
    "--namespace ns status", "--config-env x.y status", "--super-prefix x status",
    "-C /tmp status", "config alias.x !cmd", "config --global user.email a@b",
    "config user.email a@b", "push --force", "push -f", "clean -fdx", "clean",
    "reset --hard", "rebase main", "filter-branch", "checkout -- .", "restore .",
    "worktree add x", "stash pop", "pwn", "diff --output=/tmp/x",
    "fetch -u /bin/sh origin", "log :!*.md", "clone ext::sh -c id",
    "diff --no-ext-diff --ext-diff", "log --format=%h;ls", "commit -F - ; rm",
]

ALLOWED = [
    "status", "status --porcelain=v1", "diff HEAD~1", "diff --stat", "log -n 3",
    "log --pretty=format:%h", "log --oneline --decorate", "add a.txt", "add .",
    "add -A", "commit -m msg", "commit -am msg", "commit --amend --no-edit",
    "branch", "branch -a", "branch --show-current", "show HEAD", "show --stat",
    "remote -v", "remote -vv", "stash list", "stash show", "blame a.txt",
    "grep -n TODO", "ls-files", "ls-tree HEAD", "rev-parse --abbrev-ref HEAD",
    "describe --tags", "config --get user.name", "config --list", "tag -a v1 -m m",
    "checkout -b new", "checkout main", "switch -c new", "switch main",
    "push", "push -u origin main", "pull --ff-only", "fetch origin", "init",
    "merge main", "revert --no-edit HEAD", "cherry-pick abc123", "reset",
    "reset --soft HEAD~1", "mv a.txt b.txt", "rm --cached a.txt", "version",
    "--version", "clean -nd", "worktree list", "symbolic-ref --short HEAD",
    "commit -m 'feat!: x'", "commit -m 'a; b && c'", "status -sb",
]


@pytest.mark.parametrize("command", REJECTED)
def test_guard_refuses_and_names_the_token(command):
    with pytest.raises(ValueError) as info:
        split_command(command)
    text = str(info.value)
    assert text.startswith("refused"), text
    named = text.split("—")[0].replace("refused:", "").strip().strip("'")
    assert named.split(" ")[0] in command, f"{command!r}: names {named!r}"
    assert "—" in text and len(text) > 60, "a refusal must say why and what instead"


@pytest.mark.parametrize("command", ALLOWED)
def test_guard_lets_the_users_own_git_through(command):
    argv = split_command(command)
    assert argv[0] == "git" and argv[1] == command.split()[0].strip("'\"")


def test_both_argv_shapes_of_the_same_guess_are_refused():
    """A filter that searched the joined string would care where the flag sits."""
    for command in ("-c a=b status", "status -c a=b", "status -c alias.x='!y'",
                    "--git-dir .git status", "status --git-dir .git",
                    "status --work-tree=/tmp", "status --exec-path=/tmp"):
        with pytest.raises(ValueError) as info:
            split_command(command)
        assert str(info.value).startswith("refused"), command


@pytest.mark.parametrize("lang", ["en", "ru"])
@pytest.mark.parametrize("command, named, english, russian", [
    ("-c alias.pwn='!python pwn.py' pwn", "-c", "config value", "конфигурац"),
    ("rebase -i main", "rebase", "human decision", "решение человека"),
    ("diff --ext-diff", "--ext-diff", "external", "конфигурац"),
    ("fetch --upload-pack=/x origin", "--upload-pack", "command line", "командн"),
])
def test_refusals_are_bilingual_in_every_table(lang, command, named, english, russian):
    """The reason comes from a table, so `L()` must run when the refusal is written.

    A dict built at import time with `L()` already resolved freezes the language
    of whoever imported the module first, and a Russian user then reads half a
    sentence of English about a refusal they cannot act on.
    """
    i18n.set_lang(lang)
    output = GitTool().execute(command).output
    assert output.startswith("refused"), output
    assert named in output.split("—")[0], output
    want = russian if lang == "ru" else english
    assert want in output, output
    assert len(output) > 60, "a refusal must say why and what to run instead"


def test_harden_asks_git_for_its_noninteractive_forms(repo):
    assert _harden(split_command("status")) == ["git", "--no-pager", "status"]
    # git reads its own options only before the subcommand and the subcommand's
    # only after it: in the wrong half, `--no-ext-diff` is an unknown option.
    assert _harden(split_command("diff -- a.txt")) == [
        "git", "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "--", "a.txt"]
    assert _harden(split_command("log -1")) == [
        "git", "--no-pager", "log", "--no-ext-diff", "--no-textconv", "-1"]
    assert _harden(split_command("status -s")) == ["git", "--no-pager", "status", "-s"]


def test_no_command_ever_becomes_a_shell_string(repo, monkeypatch):
    """The argv the child actually receives, for the widest mix of calls."""
    seen = []

    def fake_popen(argv, *args, **kwargs):
        seen.append(list(argv))

        class Done:
            returncode = 0

            def wait(self, timeout=None):
                return 0

        return Done()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    for command in ("status", "commit -m 'x; y'", "log --oneline", "add .",
                    "diff HEAD~1", "blame a.txt"):
        repo["git"].execute(command)
    assert seen, "the tool never ran anything"
    for argv in seen:
        assert argv[0] == "git", argv
        assert not any(shell in str(part) for part in argv
                       for shell in ("cmd.exe", "/c", "-c ", "bash", "sh")), argv
        assert not any(part.startswith("-c") and "=" in part for part in argv), argv
