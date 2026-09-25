"""The git tool.

A grant that reads "may use version control" must not be a grant to run
anything at all, so this file has one job beyond building an argv: it decides
what a *guessing model* is allowed to point git at.

The first version of this tool tested the joined command string against a list
of shell metacharacters and believed it. That check could be swallowed by a
value — `git -c alias.pwn='!python pwn.py' pwn` contains nothing the filter
recognised, git stored the alias, ran it, and handed `/bin/sh` the payload. So
the string check is politeness now (it teaches the model that this tool is not a
shell); the wall is `guard()`, which *parses* the argv: the subcommand, every
flag token, every value position and every pathspec are tested separately, and
every refusal names the exact token it rejected and what to run instead.
"""
import os
import shlex

from ..i18n import L
from . import _seen
from .base import BaseTool, ToolResult
from .shell import run_argv_text

# Refused token by token, after the split: these are the shapes a model reaches
# for when it believes this tool is a terminal. They are inert inside an argv —
# nothing here ever hands a string to a shell — but a bare `;` sitting where a
# pathspec belongs is a model asking for two commands, and a tool that runs one
# of them silently is worse than a refusal that says so. A commit message keeps
# its punctuation: `-m "fix: a; b"` is prose, not syntax.
_SHELL_SHAPES = (";", "&&", "||", "|", "`", "$(", ">", "<")


class Refusal(ValueError):
    """A token we will not run, with the reason and the alternative.

    A ValueError, because that is what `split_command` has always raised and
    what the loop hands back to the model.
    """

    def __init__(self, token: str, why: str, instead: str):
        super().__init__(f"refused: {token!r} — {why}. {instead}")
        self.token = token


# --------------------------------------------------------------------------
# tokens refused wherever they sit in the argv
# --------------------------------------------------------------------------

# Each of these tables holds a (english, russian) pair rather than a finished
# string: `L()` is called when the refusal is written, not at import, so a
# session that switches language mid-run keeps answering in the language it is in.
#
# Each of these re-aims git: at another repository, another config file, another
# set of binaries — or hands it a config value, and a config value is a command
# (`alias.x='!cmd'`, `core.pager`, `diff.external`, `credential.helper`) the
# moment git reads it. Position is irrelevant on purpose: a free model sends
# both `git -c a=b status` and `git status -c a=b`, so both are refused alike.
_REDIRECTS = {
    "-c": ("it hands git a config value, and a config value is how "
            "`alias.x='!cmd'` turns a git grant into a grant to run anything",
            "он передаёт git значение конфигурации, а через `alias.x='!cmd'` "
            "право на git становится правом выполнить что угодно"),
    "--config-env": ("it is `-c` with the value taken out of the environment",
                      "это `-c`, только значение берётся из переменных окружения"),
    "-C": ("it runs git as if it had started in another directory — and then "
            "another repository's files are the ones being changed",
            "он запускает git так, будто тот начался в другой папке, — и меняются "
            "тогда файлы другого репозитория"),
    "--git-dir": ("it points git at someone else's repository",
                   "он указывает git на чужой репозиторий"),
    "--work-tree": ("it says which working tree to overwrite",
                     "он указывает, какое рабочее дерево перезаписывать"),
    "--namespace": ("it hides which repository git is really talking to",
                     "он скрывает, с каким репозиторием git на самом деле говорит"),
    "--exec-path": ("it says where git's own binaries — and every `git-*` "
                     "helper — come from",
                     "он задаёт, откуда берутся собственные бинарники git и "
                     "хелперы `git-*`"),
    "--super-prefix": ("it re-anchors git at another repository, the way "
                        "`--git-dir` does",
                        "он снова привязывает git к другому репозиторию, как `--git-dir`"),
    "--html-dir": ("it is a directory git reads outside the repository",
                    "это папка вне репозитория, которую git прочитает"),
}

# Flags whose value is a command line, which git runs itself.
_EXEC_FLAGS = {
    "--upload-pack": ("it is a program `git fetch`/`git pull` run",
                       "это программа, которую запускает `git fetch`/`git pull`"),
    "--receive-pack": ("it is a program `git push` runs",
                        "это программа, которую запускает `git push`"),
    "--exec": ("it is a program `git push` runs",
                "это программа, которую запускает `git push`"),
    "--notify": ("git runs it as a hook", "git запускает его как хук"),
    "--template": ("a template directory carries hooks git will run later",
                     "каталог шаблонов содержит хуки, которые git потом выполнит"),
    "--objects-template": ("a template directory carries hooks git will run later",
                             "каталог шаблонов содержит хуки, которые git потом выполнит"),
    "--separate-git-dir": ("it moves the repository somewhere else",
                            "он переносит репозиторий в другое место"),
    "--reference": ("it borrows objects from a repository the user never named",
                     "он берёт объекты из репозитория, который пользователь не называл"),
    "--reference-if-able": ("it borrows objects from a repository the user never named",
                              "он берёт объекты из репозитория, который пользователь не называл"),
    "--alternates": ("it borrows objects from a repository the user never named",
                       "он берёт объекты из репозитория, который пользователь не называл"),
    "--output": ("it writes the diff into a file instead of showing it",
                   "он пишет diff в файл вместо того, чтобы показать его"),
}

# Flags that switch on a program the *repository* names: `diff.external`,
# `textconv=…` in a shipped .gitattributes, and the pager — git hands the pager
# to a shell. A clone the user has never looked at can carry all three, so these
# are refused, and the tool passes the opposite flags itself (see `_harden`).
_CONFIG_RUNNERS = {
    "--ext-diff": ("it runs `diff.external` from the repository's own configuration",
                    "он запускает `diff.external` из конфигурации самого репозитория"),
    "--textconv": ("it runs the `textconv` filter the repository's .gitattributes names",
                    "он запускает фильтр `textconv`, который задаёт .gitattributes "
                    "репозитория"),
    "--paginate": ("git shows the output through the pager, and the pager is a "
                    "command line taken from configuration",
                    "git показывает вывод через пейджер, а пейджер — командная "
                    "строка из конфигурации"),
}

# `switch -c`, `branch -c` and `commit -c` are the subcommand's own letters, not
# git's global `-c`: they mean create, copy and re-edit. Position is the whole
# difference, which is exactly why the argv is parsed instead of searched. The
# capital `-C` is the same letter with a different meaning — see `_check_flag`.
_SUBCOMMAND_OWN_C = {"switch", "branch", "commit", "checkout", "tag"}

# Subcommands that must never run, each with the sentence telling the user what
# to do instead. Unknown subcommands are refused too (see `_COMMANDS`); these
# get a named reason because they are the ones that bite.
_DENIED = {
    "rebase": ("rewriting history is a human decision",
                "переписывать историю — решение человека"),
    "filter-branch": ("it rewrites every commit and can run a command per commit",
                        "он переписывает все коммиты и может выполнить команду на каждый"),
    "am": ("it turns a patch into commits and runs the hooks that come with it",
            "он превращает патч в коммиты и запускает его хуки"),
    "apply": ("it writes files straight from a patch the model guessed",
                  "он пишет файлы прямо из патча, который модель придумала"),
    "bisect": ("`bisect run` executes an arbitrary command per commit",
                "`bisect run` выполняет произвольную команду для каждого коммита"),
    "submodule": ("it clones and runs code from repositories living inside this one",
                     "он клонирует и запускает код репозиториев внутри этого"),
    "difftool": ("it opens an editor configured outside this tool",
                  "он открывает редактор, заданный вне этого инструмента"),
    "mergetool": ("it opens an editor configured outside this tool",
                   "он открывает редактор, заданный вне этого инструмента"),
    "gui": ("it starts a graphical program", "он запускает графическую программу"),
    "gitk": ("it starts a graphical program", "он запускает графическую программу"),
    "daemon": ("it starts a network service", "он поднимает сетевой сервис"),
    "http-backend": ("it starts a network service", "он поднимает сетевой сервис"),
    "shell": ("it starts a login shell for git over ssh",
               "он поднимает оболочку входа для git по ssh"),
    "instaweb": ("it starts a web server", "он поднимает веб-сервер"),
    "send-email": ("it sends mail", "он отправляет почту"),
    "imap-send": ("it sends mail", "он отправляет почту"),
    "request-pull": ("it mails a request to someone", "он отправляет кому-то письмо"),
    "credential": ("it reads and writes the user's stored passwords",
                    "он читает и пишет сохранённые пароли пользователя"),
    "archive": ("it exports the tree into a file", "он выгружает дерево в файл"),
    "bundle": ("it writes the repository out to a file",
                "он выгружает репозиторий в файл"),
    "gc": ("it prunes objects the user may still want back",
            "он вычищает объекты, которые пользователь ещё хотел бы вернуть"),
    "prune": ("it deletes unreachable objects", "он удаляет недостижимые объекты"),
    "rerere": ("it replays a recorded conflict resolution",
               "он проигрывает записанное разрешение конфликта"),
    "replace": ("it hides one commit behind another", "он подменяет один коммит другим"),
    "alias": ("an alias is a config value, and a config value is a command",
               "алиас — это значение конфигурации, а git выполнит его как команду"),
    "help": ("documentation is read outside this tool",
              "документацию читают не этим инструментом"),
}

# The commands this tool knows. Anything outside this set is refused, which is
# what makes a repository's own alias (`git pwn`) a refusal instead of a shell.
_COMMANDS = {
    # looking
    "status", "diff", "log", "show", "blame", "annotate", "grep", "cherry",
    "ls-files", "ls-tree", "cat-file", "rev-parse", "rev-list", "describe",
    "shortlog", "for-each-ref", "show-ref", "merge-base", "name-rev", "reflog",
    "diff-files", "diff-index", "diff-tree", "version", "config",
    "ls-remote", "symbolic-ref", "worktree", "stash", "remote", "branch", "tag",
    # the user's own workflow, on request
    "init", "clone", "fetch", "pull", "push", "add", "rm", "mv", "commit",
    "reset", "checkout", "switch", "restore", "merge", "revert", "cherry-pick",
    # `clean` is here only so that the refusal can name `-f`: without `-n` it is
    # never allowed to run at all (see `_check_arguments`).
    "clean",
}

# Asked for on its own, these say nothing about a repository: git answers about
# itself and never opens a working tree, so they are not worth a refusal.
_ABOUT_GIT = {"--version", "--help", "-v", "-h"}

# Global options accepted before the subcommand. `--no-pager` is added by this
# tool itself; a model asking for it is harmless, and anything else there is one
# of the redirects above.
_GLOBAL_OK = {"--no-pager", "--bare", "--no-optional-locks", "--literal-pathspecs"}

# Flags that take the next token (or their own `=value`) as *data*. Without this
# table a message like `git commit -m "--force is not a flag"` would be read as a
# flag; with it, a value can neither masquerade as an option nor carry a refusal
# the model never asked for.
_DATA_VALUE_FLAGS = {
    "-m", "--message", "-F", "--file", "--author", "--date", "--format",
    "--pretty", "--since", "--until", "--after", "--before", "-G", "-S",
    "-n", "--max-count", "-U", "-O", "--find-object", "--exec",
    "--upload-pack", "--receive-pack", "--notify", "--template", "--reference",
    "--reference-if-able", "--git-dir", "--work-tree", "--namespace", "-c",
    "-C", "--config-env", "--separate-git-dir", "--objects-template",
    "--super-prefix", "--html-dir", "--output", "--alternates", "--type",
    "--abbrev", "--depth", "--file-mode", "--trace", "--trace-perf",
}

# The same letters mean different things to different subcommands, and a wrong
# guess here only ever shifts which token is scanned as data — the argv git
# receives is the model's own, never rebuilt. `commit -s` signs (boolean) while
# `restore -s <tree>` names a source, so the two are kept apart per subcommand.
_SUBCOMMAND_VALUE_FLAGS = {
    "restore": {"-s", "--source"},
    "merge": {"-s", "--strategy", "-X", "--strategy-option"},
    "cherry-pick": {"-X", "--strategy-option"},
    "revert": {"-X", "--strategy-option"},
}


def _value_flags(sub: str) -> set:
    return _DATA_VALUE_FLAGS | _SUBCOMMAND_VALUE_FLAGS.get(sub, set())


# A commit message is the one place a `!` means prose and not a shell (`feat!:`
# is the usual), so these flags' values are the only tokens allowed to hold one.
_MESSAGE_FLAGS = {"-m", "--message", "-F", "--file"}

# `git config` reads and writes the same file that stores the commands git runs.
_CONFIG_WRITE_FLAGS = {
    "--global", "--system", "--local", "--file", "-f", "--edit", "-e",
    "--replace-all", "--add", "--unset", "--unset-all", "--remove-section",
    "--rename-section", "--blob",
}

# "Everything" is never what the model was asked for.
_ALL_PATHS = {".", "./", "*", "./*", "**", ":/", "..", "./..", "/"}

# Paths this tool staged, so that `git add x` followed by `git restore x` — the
# agent undoing its own mistake — stays possible, while a blind
# `git restore src/a.c` (the user's uncommitted edit) is not.
_STAGED: set = set()

# The commands that print a diff, and therefore read `diff.external` and the
# `textconv` filters out of the repository's own configuration. Measured against
# git 2.55: `status` and `grep` reject these flags outright, so they are not on
# this list — injecting an option git would call unknown would break a command
# the user asked for.
_DIFF_LIKE = {"diff", "log", "show", "blame", "revert", "cherry-pick"}


def _flag_of(token: str) -> str:
    """The flag part of a token: `--git-dir=/x` names the flag `--git-dir`."""
    if token.startswith("--"):
        return token.split("=", 1)[0]
    return token


def _norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return os.path.normcase(path)


def _touched_by_session(path: str) -> bool:
    """Did this session put its hands on that file?"""
    target = _norm(path)
    if target in _STAGED:
        return True
    return any(target == _norm(known) for known in _seen.STAMPS)


def _reject_escaping_path(token: str) -> None:
    if os.path.splitdrive(token)[0] or token.startswith(("/", "\\")):
        raise Refusal(token,
                      L("it is an absolute path", "это абсолютный путь"),
                      L("pass a path relative to the repository root",
                        "укажи путь относительно корня репозитория"))
    if os.path.normpath(token).split(os.sep)[:1] == [".."]:
        raise Refusal(token,
                      L("it climbs out of the repository",
                        "он выходит за пределы репозитория"),
                      L("stay inside the working directory",
                        "останься внутри рабочей папки"))


def _scan(token: str, *, value: bool = False, sub: str | None = None) -> None:
    """The checks that hold no matter where in the argv a token sits."""
    flag = _flag_of(token)
    if flag in ("-c", "-C") and sub in _SUBCOMMAND_OWN_C:
        return          # `switch -c`, `branch -c`, `commit -c` — the subcommand's
                        # own letter, not git's global config option
    if flag in _REDIRECTS:
        raise Refusal(token,
                      L(f"this is git's global option `{flag}`: "
                        f"{L(*_REDIRECTS[flag])}",
                        f"это глобальная опция git `{flag}`: {L(*_REDIRECTS[flag])}"),
                      L("choose the repository by running the tool from inside it; "
                        "git is never handed a config value",
                        "репозиторий выбирается тем, что инструмент запущен из него; "
                        "git никогда не передают значение конфигурации"))
    if flag in _EXEC_FLAGS:
        raise Refusal(token,
                      L(f"`{flag}` takes a command line — "
                        f"{L(*_EXEC_FLAGS[flag])}",
                        f"`{flag}` принимает командную строку: "
                        f"{L(*_EXEC_FLAGS[flag])}"),
                      L("leave the flag out and let git use its own default",
                        "убери эту опцию — пусть git возьмёт значение по умолчанию"))
    if flag in _CONFIG_RUNNERS:
        raise Refusal(token,
                      L(f"`{flag}` — {L(*_CONFIG_RUNNERS[flag])}",
                        f"`{flag}` — {L(*_CONFIG_RUNNERS[flag])}"),
                      L("run it without the flag: this tool already asks git for "
                        "`--no-pager` and `--no-ext-diff`",
                        "запусти без этой опции: инструмент и так просит у git "
                        "`--no-pager` и `--no-ext-diff`"))
    if value:
        return
    for shape in _SHELL_SHAPES:
        if shape in token:
            raise Refusal(token,
                          L(f"`{shape}` is not allowed — it is how a shell chains "
                            "commands, and this tool runs git alone, never a shell",
                            f"`{shape}` не разрешён — так shell связывает команды, а "
                            "этот инструмент запускает только git, не shell"),
                          L("make one call per step",
                            "выполняй каждый шаг отдельным вызовом"))
    if "!" in token:
        raise Refusal(token,
                      L("`!` is how a git config value or an alias smuggles a shell "
                        "command; a plain argument has no use for it",
                        "`!` — способ передать git команду оболочки через значение "
                        "конфигурации или алиас; в обычном аргументе он не нужен"),
                      L("drop it; to exclude a path in a pathspec write `:^name` "
                        "instead of `:!name`",
                        "убери его; чтобы исключить путь в pathspec, пиши `:^имя` "
                        "вместо `:!имя`"))
    if "::" in token:
        raise Refusal(token,
                      L("a `<name>::<command>` URL tells git to talk to a program "
                        "instead of a server",
                        "URL вида `<имя>::<команда>` велит git говорить с программой "
                        "вместо сервера"),
                      L("use an https:// or ssh:// remote",
                        "используй remote https:// или ssh://"))
    if flag == "alias" or flag.startswith("alias."):
        raise Refusal(token,
                      L("an alias is a config value, and a config value is a "
                        "command git will run",
                        "алиас — это значение конфигурации, а git выполнит его как "
                        "команду"),
                      L("run the real git command instead of the alias",
                        "выполни настоящую команду git вместо алиаса"))


def _check_flag(sub: str, name: str) -> None:
    """Flags a specific subcommand must not carry: the ones that destroy work."""
    if name in ("-f", "--force", "--force-with-lease") and sub not in ("add", "grep"):
        # `add -f` means "stage this ignored file" (the user's call, nothing is
        # lost); `grep -f FILE` is a list of patterns. Everywhere else the letter
        # means force.
        if sub == "clean":
            raise Refusal(name,
                          L("`git clean -f` deletes untracked files, ignored ones "
                            "included — the user's work, with no copy left anywhere",
                            "`git clean -f` удаляет неотслеживаемые файлы, включая "
                            "исключённые из vcs — работу пользователя, копии от "
                            "которой не остаётся"),
                          L("use `git clean -nd` to see what would go; removing it "
                            "is the user's call",
                            "используй `git clean -nd`, чтобы посмотреть; удаляет "
                            "пользователь"))
        raise Refusal(name,
                      L("it forces git to overwrite or discard work the user has "
                        "not committed",
                        "он заставляет git перезаписать или выбросить "
                        "незакоммиченную работу"),
                      L("run it without the flag and let git refuse, or ask the "
                        "user to run the forced form themselves",
                        "запусти без этой опции и пусть git сам откажет, либо "
                        "попроси пользователя выполнить принудительную форму"))
    if name in ("-C", "-M", "--force-create", "--force-move") and sub in _SUBCOMMAND_OWN_C \
            and sub != "commit":
        raise Refusal(name,
                      L("it force-moves a name that already exists, which drops the "
                        "commits it pointed at",
                        "он переставляет уже существующее имя, а это теряет коммиты, "
                        "на которые оно указывало"),
                      L("create a new name with `-b`, or ask the user to move the "
                        "branch themselves",
                        "создай новое имя через `-b`, а переставлять ветку попроси "
                        "пользователя"))
    if name == "--hard" and sub in ("reset", "checkout", "restore", "switch"):
        raise Refusal(name,
                      L("`--hard` throws away every uncommitted change in the tree",
                        "`--hard` выбрасывает все незакоммиченные изменения в дереве"),
                      L("use `git reset` (mixed) to unstage, or `git stash` to keep "
                        "the changes; `--hard` is the user's to run",
                        "используй `git reset` (mixed), чтобы снять индекс, или "
                        "`git stash`, чтобы сохранить изменения; `--hard` — за "
                        "пользователем"))
    if name in ("-p", "--patch") and sub in ("add", "reset", "checkout", "restore",
                                             "commit", "switch"):
        raise Refusal(name,
                      L("it needs an interactive terminal, and this tool has none",
                        "нужен интерактивный терминал, которого у этого инструмента нет"),
                      L("stage a whole file with `git add <file>`",
                        "добавляй файл целиком: `git add <файл>`"))
    if name in ("--no-verify", "--no-gpg-sign") and sub in ("commit", "push"):
        raise Refusal(name,
                      L("it skips the checks and the hooks the repository asks for",
                        "он пропускает проверки и хуки, которые запрашивает репозиторий"),
                      L("let the hooks run", "пусть хуки отработают"))
    if name in ("-d", "-D", "--delete") and sub in ("branch", "tag"):
        raise Refusal(name,
                      L("deleting a branch or a tag can drop the only copy of a commit",
                        "удаление ветки или тега может оставить коммит без копии"),
                      L("show them with `git branch -v` and let the user delete one",
                        "покажи через `git branch -v`, а удалять — пользователю"))
    if sub == "push" and name in ("--prune", "--delete"):
        raise Refusal(name,
                      L("it deletes refs on the remote that other people work on",
                        "он удаляет на удалённом репозитории ссылки, с которыми "
                        "кто-то работает"),
                      L("push the commit; deleting refs is the user's call",
                        "отправь коммит; удалять ссылки — решение пользователя"))
    if name == "-u" and sub in ("fetch", "pull"):
        raise Refusal(name,
                      L("on `fetch`/`pull`, `-u` is `--upload-pack`: a command line "
                        "git runs",
                        "у `fetch`/`pull` `-u` — это `--upload-pack`: командная "
                        "строка, которую запускает git"),
                      L("to set an upstream, `git push -u <remote> <branch>` or "
                        "`git branch --set-upstream-to=<remote>/<branch>`",
                        "чтобы задать upstream: `git push -u <remote> <ветка>` или "
                        "`git branch --set-upstream-to=<remote>/<ветка>`"))
    if sub == "config" and name in _CONFIG_WRITE_FLAGS:
        raise Refusal(name,
                      L("it either writes git's configuration or points `git config` "
                        "at a file outside the repository — and configuration is "
                        "where the commands git later runs live (`alias.*`, "
                        "`core.pager`, `diff.external`, `credential.helper`)",
                        "он либо пишет конфигурацию git, либо направляет `git config` "
                        "на файл вне репозитория — а в конфигурации живут команды, "
                        "которые git потом выполняет (`alias.*`, `core.pager`, "
                        "`diff.external`, `credential.helper`)"),
                      L("read with `git config --get <key>` and tell the user which "
                        "value you wanted",
                        "прочитай через `git config --get <ключ>` и скажи "
                        "пользователю, какое значение тебе нужно"))


def _parse(sub: str, rest: list) -> tuple:
    """Split a subcommand's argv into the flags it really used and its arguments.

    Short options are unpacked character by character, because `-fdx` is three
    flags and a deny-list that only sees the string `-fdx` sees none of them.
    """
    takes = _value_flags(sub)
    flags: list = []
    positionals: list = []
    paths_only = False
    index = 0
    while index < len(rest):
        token = rest[index]
        index += 1
        if paths_only:
            # after `--`, git reads pathspecs and never another option
            positionals.append(token)
            _scan(token, sub=sub)
            continue
        if token == "--":
            paths_only = True
            continue
        if not token.startswith("-") or token == "-":
            positionals.append(token)
            _scan(token, sub=sub)
            continue
        if token.startswith("--"):
            _scan(token, sub=sub)
            name, _, inline = token.partition("=")
            _check_flag(sub, name)
            flags.append(name)
            if name in takes:
                if inline:
                    _scan(inline, value=name in _MESSAGE_FLAGS, sub=sub)
                elif index < len(rest):
                    _scan(rest[index], value=name in _MESSAGE_FLAGS, sub=sub)
                    index += 1
            continue
        # short, possibly bundled: -f, -fdx, -am, -mmsg
        body = token[1:]
        position = 0
        while position < len(body):
            name = "-" + body[position]
            position += 1
            _scan(name, sub=sub)
            _check_flag(sub, name)
            flags.append(name)
            if name in takes:
                inline = body[position:]
                if not inline and index < len(rest):
                    inline = rest[index]
                    index += 1
                if inline:
                    _scan(inline, value=name in _MESSAGE_FLAGS, sub=sub)
                break
    return flags, positionals, paths_only


def _discards_worktree(sub: str, flags: list) -> bool:
    """Does this form of the command overwrite files in the working tree?

    Only those need a path the session owns. `git rm --cached` and
    `git restore --staged` move the index, leave the user's file alone, and are
    the ordinary way to unstage something — refusing them would be refusing the
    workflow, not the risk.
    """
    used = set(flags)
    if sub == "rm":
        return "--cached" not in used
    if sub == "restore":
        return "--staged" not in used or "--worktree" in used
    return True


def _acted_paths(sub: str, positionals: list, paths_only: bool) -> list:
    """The paths `checkout`/`restore`/`rm` will act on.

    `restore` and `rm` only ever take paths. `git checkout <branch>` names a
    branch, so a checkout argument is a path when it follows a literal `--` — or
    when the file is really standing in the working tree, because
    `git checkout HEAD~1 notes.md` restores that file and looks exactly like a
    branch name to anything that does not look.
    """
    if sub != "checkout":
        return list(positionals)
    if paths_only:
        return list(positionals)
    return [p for p in positionals if os.path.isfile(p)]


def _check_arguments(sub: str, flags: list, positionals: list, paths_only: bool) -> None:
    """Sub-actions and pathspecs: what the command will really touch."""
    plain = list(positionals)
    # The commands that destroy work rather than record it. `add` is absent on
    # purpose: staging a whole tree is normal and nothing of the user's is lost.
    destructive = sub in ("clean", "checkout", "restore", "rm", "reset", "stash")

    if destructive:
        hit = sorted(set(plain) & _ALL_PATHS)
        if hit:
            raise Refusal(hit[0],
                          L(f"`git {sub} {hit[0]}` means the whole tree, including "
                            "the user's own uncommitted work",
                            f"`git {sub} {hit[0]}` — это всё дерево, включая "
                            "незакоммиченную работу пользователя"),
                          L("name the individual paths, one per call",
                            "назови пути по одному, по одному на вызов"))
    if sub in ("clean", "checkout", "restore", "rm", "reset", "add", "commit",
               "stash", "switch", "merge", "revert", "cherry-pick"):
        for path in plain:
            _reject_escaping_path(path)
        if sub == "add":
            _STAGED.update(_norm(p) for p in plain)

    if sub in ("restore", "checkout", "rm"):
        for path in _acted_paths(sub, plain, paths_only):
            if not _discards_worktree(sub, flags) or _touched_by_session(path):
                continue
            raise Refusal(path,
                          L(f"this session never opened or staged that path, so "
                            f"`git {sub}` would throw away changes someone else made",
                            f"эта сессия не открывала и не добавляла этот путь, "
                            f"поэтому `git {sub}` выбросил бы чужие изменения"),
                          L("look first with `git status` or `git diff -- <path>`, "
                            "and ask the user before undoing their work",
                            "сначала посмотри `git status` или `git diff -- "
                            "<путь>`, а работу пользователя переспроси"))

    if sub == "clean" and not ({"-n", "--dry-run"} & set(flags)):
        raise Refusal("clean",
                      L("`git clean` without `-n` deletes files this session never "
                        "created",
                        "`git clean` без `-n` удаляет файлы, которые эта сессия "
                        "не создавала"),
                      L("run `git clean -nd` to see what would go, and show the user "
                        "the list",
                        "запусти `git clean -nd`, чтобы посмотреть, и покажи список "
                        "пользователю"))

    if sub == "config" and len(plain) >= 2:
        raise Refusal(" ".join(plain[:2]),
                      L("it writes git's configuration, and configuration is where "
                        "the commands git later runs live (`alias.*`, `core.pager`, "
                        "`diff.external`, `credential.helper`)",
                        "он пишет конфигурацию git, а в конфигурации живут команды, "
                        "которые git потом выполняет (`alias.*`, `core.pager`, "
                        "`diff.external`, `credential.helper`)"),
                      L("read it with `git config --get <key>` and tell the user what "
                        "you wanted set",
                        "прочитай через `git config --get <ключ>` и скажи "
                        "пользователю, что ты хотел задать"))

    if sub == "stash":
        action = plain[0] if plain else "list"
        if action not in ("list", "show", "push", "create"):
            raise Refusal(action,
                          L(f"`git stash {action}` replaces the working tree with a "
                            "stash the model picked",
                            f"`git stash {action}` возвращает рабочее дерево из "
                            "выбранного моделью stash"),
                          L("show `git stash list` and `git stash show -p \"stash@{0}\"`; "
                            "popping is the user's call",
                            "покажи `git stash list` и `git stash show -p "
                            "\"stash@{0}\"`; возвращать — пользователю"))

    if sub == "worktree":
        action = plain[0] if plain else "list"
        if action != "list":
            raise Refusal(action,
                          L("a second working tree is a second place for the user's "
                            "files to disagree",
                            "второе рабочее дерево — второе место, где файлы "
                            "пользователя могут разойтись"),
                          L("use `git worktree list` to look",
                            "посмотри через `git worktree list`"))

    if sub == "remote":
        action = plain[0] if plain else "-v"
        if action in ("remove", "rm", "rename", "set-head", "set-url", "prune",
                      "get-url"):
            raise Refusal(action,
                          L("the remotes are the user's configuration, not a step of "
                            "this task",
                            "remote — это конфигурация пользователя, а не шаг этой "
                            "задачи"),
                          L("show them with `git remote -v`",
                            "покажи через `git remote -v`"))

    if sub == "symbolic-ref" and len(plain) > 1:
        raise Refusal(plain[1],
                      L("writing a ref moves a branch pointer without a commit",
                        "запись ссылки двигает указатель ветки без коммита"),
                      L("read it with `git symbolic-ref --short HEAD`",
                        "прочитай через `git symbolic-ref --short HEAD`"))


def split_command(command: str) -> list:
    """`git`'s own arguments, or a ValueError if the string wants a shell."""
    text = (command or "").strip()
    if not text:
        raise ValueError(L("no git command given", "команда git не задана"))
    if "\n" in text or "\r" in text:
        # A newline is two commands in one box. shlex would fold it into
        # whitespace and git would then see one command with stray arguments,
        # which is the silent half-success this tool exists to avoid.
        raise ValueError(
            L("a newline is not allowed — the git tool runs git alone, not a shell. "
              "Chain steps with separate calls",
              "перенос строки запрещён — инструмент git запускает только git, не "
              "shell. Шаги — отдельными вызовами"))
    argv = ["git"] + shlex.split(text, posix=True)
    guard(argv)
    return argv


def guard(argv: list) -> list:
    """Parse an argv and refuse what it must not run. Raises ValueError."""
    if not argv or argv[0] != "git":
        raise ValueError(L("the git tool runs git", "инструмент git запускает git"))
    tokens = argv[1:]
    if not tokens:
        raise ValueError(L("no git command given", "команда git не задана"))
    if tokens[0] in _ABOUT_GIT and len(tokens) == 1:
        return argv                      # `git --version` reads no repository
    index = 0
    while index < len(tokens) and tokens[index].startswith("-"):
        flag = _flag_of(tokens[index])
        if flag in _GLOBAL_OK:
            index += 1
            continue
        _scan(tokens[index])
        raise Refusal(tokens[index],
                      L(f"`{flag}` before the subcommand is a global git option, and "
                        "the ones still standing here are redirects",
                        f"`{flag}` перед подкомандой — глобальная опция git, а из "
                        "оставшихся там только перенаправляющие"),
                      L("start the command with the subcommand, e.g. `status -s`",
                        "начинай команду с подкоманды, например `status -s`"))
    if index >= len(tokens):
        raise ValueError(L("no git subcommand given", "не задана подкоманда git"))
    sub = tokens[index]
    _scan(sub)
    if sub in _DENIED:
        raise Refusal(sub,
                      L(f"this tool does not run `git {sub}` — "
                        f"{L(*_DENIED[sub])}",
                        f"этот инструмент не запускает `git {sub}` — "
                        f"{L(*_DENIED[sub])}"),
                      L("ask the user to run it in a terminal",
                        "попроси пользователя выполнить это в терминале"))
    if sub not in _COMMANDS:
        raise Refusal(sub,
                      L("it is not a git command this tool was written to run — and an "
                        "unknown word here is exactly what a repository's own alias "
                        "looks like",
                        "этой команды git инструмент не знает — а незнакомое слово "
                        "здесь выглядит ровно как алиас самого репозитория"),
                      L("name one of status, diff, log, show, add, commit, push, "
                        "branch, remote, stash, blame — or ask the user",
                        "назови одну из status, diff, log, show, add, commit, push, "
                        "branch, remote, stash, blame — или спроси пользователя"))
    flags, positionals, paths_only = _parse(sub, tokens[index + 1:])
    _check_arguments(sub, flags, positionals, paths_only)
    return argv


def _harden(argv: list) -> list:
    """Ask git for the non-interactive forms of itself, whatever the model said.

    `--no-pager` matters more than it looks: git runs the pager through a shell,
    and `core.pager` is a value a cloned repository can carry. `--no-ext-diff`
    and `--no-textconv` close the two doors a `.gitattributes` or a
    `diff.external` in that same config can open. The tool passes them itself so
    that the model never needs the flags that would open them.

    The two kinds of flag live in different halves of the argv — git reads its
    own options only before the subcommand and the subcommand's only after it —
    so the split point is found rather than assumed.
    """
    tokens = argv[1:]
    index = 0
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 1
    globals_kept, rest = tokens[:index], tokens[index:]
    if rest and rest[0] in _DIFF_LIKE:
        rest = [rest[0], "--no-ext-diff", "--no-textconv"] + rest[1:]
    return ["git", "--no-pager"] + globals_kept + rest


class GitTool(BaseTool):
    name = "git"
    description = (
        "Run git — pass the command without the leading 'git'. Commit, push, amend or open PRs "
        "only when the user asks: look at status, diff and recent log first, stage only intended "
        "files, never commit secrets, force-push, skip hooks or rewrite published history. "
        "This is git alone, never a shell: no -c, no config writes, no --git-dir/--work-tree/-C, "
        "no -f/--force, no reset --hard, no clean without -n, and only the subcommands it knows. "
        "A refusal names the token it rejected and what to run instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Git command (without 'git' prefix)"},
        },
        "required": ["command"],
    }

    # What git must not read on its way to the repository: a system-wide config,
    # a system attributes file, a pager — git runs the pager through a shell, and
    # a cloned repository can name one — and a credentials prompt nobody is there
    # to answer.
    _ENV = {
        "GIT_PAGER": "cat",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def execute(self, command: str) -> ToolResult:
        try:
            argv = split_command(command)
        except ValueError as e:
            text = str(e)
            return ToolResult(output=text if text.startswith("refused")
                              else f"refused: {text}", error=True)
        try:
            stdout, stderr, returncode = run_argv_text(
                _harden(argv), timeout=30, env=self._ENV)
            output = stdout
            if stderr:
                output += f"\n[stderr]\n{stderr}"
            return ToolResult(
                output=output or "(no output)",
                error=returncode != 0,
                metadata={"returncode": returncode},
            )
        except Exception as e:
            return ToolResult(output=str(e), error=True)

    def is_safe(self) -> bool:
        return False
