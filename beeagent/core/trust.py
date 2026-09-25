"""Who is allowed to change how BeeCode behaves — decided by the user, not by a folder.

Everything in BeeCode is project-local by design: extensions live in
`<project>/.beeagent/plugins/`, and `beeagent.json` in the project decides the
permission gate. That is right for a folder the user works in and wrong for one
they only *looked at*: `git clone` and `cd` are enough to put a stranger's Python
in front of the loader, and a stranger's config can say
`{"permissions": {"mode": "auto"}}` and name the two tools that cost the most.
Both happen before the first model call, before `/allow`, before anything the
user sees.

So the decision is stored where a project cannot reach it — one file in the
user's own home, `~/.beecode/trusted.json` — and it is keyed by the folder's
resolved path. A repository can copy itself anywhere it likes; it cannot write
into someone's home directory, so it cannot grant itself trust.

What "not trusted" costs, by design:

* no `plugin.py` from the project is exec'd, and no MCP server from the project's
  `mcp.json` is registered — both are "code the folder asked to run";
* `permissions.mode`, `permissions.allowed` and `vpn_command` in that folder's
  `beeagent.json` are read but not applied — the gate stays at the defaults the
  program ships with;
* everything that cannot hurt (model, language, theme, timeouts) still loads, so
  opening a stranger's checkout to read code with BeeCode stays pleasant.

Three things let a legitimate folder through without a prompt, because each is an
act the user already performed on this machine:

1. the file's bytes are one of the plugins BeeCode ships (a copy of our own code
   is not a stranger's code);
2. the extension was installed into that folder by `PluginManager` on this
   machine — the install records the exact bytes it wrote (that is what
   `/plugin install … --trust` means: somebody read it and asked for it);
3. the folder is trusted, by the user answering the prompt or typing `/trust yes`.

Anything else is withheld *and said out loud*: a plugin that did not load is
reported as not loaded, never as loaded.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from beeagent.i18n import L

TRUST_FILENAME = "trusted.json"
TRUSTED = "trusted"
DECLINED = "declined"

# The store is a preference file, not a database: it holds one record per folder
# the user ever answered about. Long-running installs accumulate throwaway
# directories (and pytest does so by the hundred), so the oldest records are
# dropped once there are more than this many.
MAX_FOLDERS = 500

_STORE: "TrustStore | None" = None
_GATES: dict[str, "ProjectTrust"] = {}
_SHIPPED: set[str] | None = None


# --- where the answers live ---------------------------------------------------

def home_dir() -> Path | None:
    """The user's own folder, or None when this process has none.

    Without a home there is nowhere out-of-band to write a decision, and the safe
    reading of "I cannot remember that you agreed" is "you have not agreed".
    """
    try:
        return Path.home().expanduser()
    except (RuntimeError, OSError, ValueError):
        return None


def store_path() -> Path | None:
    """`~/.beecode/trusted.json`, or `BEECODE_TRUST_FILE` for a test or a profile.

    The override exists so a test can answer for a folder without touching the
    developer's real preferences — the same reason `pool.install_key()` honors
    `BEECODE_POOL_KEY_FILE`.
    """
    home = home_dir()
    if home is None:
        return None
    return Path(os.environ.get("BEECODE_TRUST_FILE") or home / ".beecode" / TRUST_FILENAME)


def folder_key(workdir=None) -> str:
    """The store's key for a folder: absolute, resolved, case-folded where that
    means something.

    Resolving matters twice over: a relative "." and the project's own path have
    to land on one record, and a folder reached through a symlink is the folder
    its files actually live in.
    """
    raw = os.getcwd() if workdir in (None, "") else str(workdir)
    try:
        path = Path(raw).expanduser().resolve()
    except OSError:
        path = Path(os.path.abspath(raw))
    return os.path.normcase(str(path))


def digest_file(path) -> str:
    """sha256 of the bytes we are about to run, or "" when they cannot be read."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, ValueError):
        return ""


def shipped_plugin_digests() -> set[str]:
    """Every `plugin.py` BeeCode itself ships, as a set of hashes.

    Computed from the installed package rather than from a list of names, so a
    template added next release is covered without touching this file. A project
    that carries a byte-identical copy of one of these is running our code, not
    the project author's.
    """
    global _SHIPPED
    if _SHIPPED is None:
        found = set()
        try:
            from beeagent.plugins.catalog import TEMPLATES_DIR

            for entry in sorted((TEMPLATES_DIR / "plugins").glob("*/plugin.py")):
                found.add(digest_file(entry))
        except Exception:
            found = set()
        _SHIPPED = {d for d in found if d}
    return _SHIPPED


class TrustStore:
    """The JSON file in the user's home, read once and written on every answer."""

    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict = {}
        self.read()

    def read(self) -> None:
        self.data = {"version": 1, "folders": {}}
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return                      # absent or torn: nobody has agreed yet
        folders = raw.get("folders") if isinstance(raw, dict) else None
        if isinstance(folders, dict):
            self.data["folders"] = {str(k): v for k, v in folders.items()
                                    if isinstance(v, dict)}

    def save(self) -> None:
        if self.path is None:
            return
        folders = self.data["folders"]
        if len(folders) > MAX_FOLDERS:
            by_age = sorted(folders.items(),
                            key=lambda kv: str(kv[1].get("updated") or ""))
            for key, _ in by_age[:len(folders) - MAX_FOLDERS]:
                folders.pop(key, None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)
            if os.name == "posix":
                try:
                    self.path.chmod(0o600)
                except OSError:
                    pass
        except OSError:
            pass                        # a refused write must not crash the agent

    def record(self, key: str) -> dict:
        return self.data["folders"].get(key) or {}

    def decisions(self, key: str) -> str:
        return str(self.record(key).get("decision") or "")

    def grants(self, key: str) -> dict:
        grants = self.record(key).get("grants")
        return grants if isinstance(grants, dict) else {}

    def _touch(self, key: str) -> dict:
        entry = self.data["folders"].setdefault(key, {})
        entry["updated"] = datetime.now().isoformat(timespec="seconds")
        return entry

    def set_decision(self, key: str, decision: str, what: str = "") -> bool:
        if self.path is None:
            return False
        entry = self._touch(key)
        entry["decision"] = decision
        if what:
            entry["asked"] = what[:600]
        self.save()
        return True

    def grant_bytes(self, key: str, digest: str, name: str = "", kind: str = "plugin",
                    how: str = "install") -> None:
        """Remember the exact bytes the user asked for in this folder.

        Keyed by hash rather than by name because that is the thing that was
        approved: `/plugin install … --trust` means somebody read *these* bytes,
        and an edit afterwards is a different question.
        """
        if self.path is None or not digest:
            return
        entry = self._touch(key)
        grants = entry.setdefault("grants", {})
        grants[digest] = {"kind": kind, "name": name, "how": how, "at": entry["updated"]}
        self.save()

    def granted_bytes(self, key: str, digest: str) -> bool:
        if not digest:
            return False
        return digest in self.grants(key)

    def clear(self, key: str) -> bool:
        if self.path is None or key not in self.data["folders"]:
            return False
        self.data["folders"].pop(key, None)
        self.save()
        return True


def store() -> TrustStore:
    global _STORE
    if _STORE is None:
        _STORE = TrustStore(store_path())
    return _STORE


# --- what one folder asks for -------------------------------------------------

def shipped_source_root() -> Path:
    """The folder the running BeeCode's own package lives in."""
    return Path(__file__).resolve().parent.parent.parent


def is_beeagent_source(path) -> bool:
    """Is this folder BeeCode's own checkout?

    Trusting it is not a shortcut around the rule: whoever can put a `beeagent/`
    package in a folder can already run Python as this user, and the everyday
    reason a project folder carries extensions is that somebody is working on
    BeeCode itself. Everything else on disk — including a directory cloned five
    minutes ago — is somebody else's claim until the user says otherwise.
    """
    try:
        return Path(str(path)).resolve() == shipped_source_root()
    except OSError:
        return False


class ProjectTrust:
    """One folder's claims, what was withheld from it, and the user's answer.

    `enforced` is off for a gate nobody installed: `Agent()` turns it on for the
    folder it starts in (that is the folder a clone put there), while a loader
    built by hand is an embedding program vouching for the bytes itself.
    """

    def __init__(self, workdir=".", enforced: bool = True):
        self.key = folder_key(workdir)
        self.path = Path(self.key)
        self.enforced = enforced
        self.withheld: list[str] = []
        self.announced = False

    # --- the answer --------------------------------------------------------

    @property
    def decision(self) -> str:
        return store().decisions(self.key)

    @property
    def trusted(self) -> bool:
        """Did the user agree to this folder — or is it BeeCode's own source?"""
        if not self.enforced:
            return True
        return self.decision == TRUSTED or is_beeagent_source(self.path)

    def allow(self, kind: str, name: str, digest: str = "") -> bool:
        """May this piece of the folder have its way?"""
        if self.trusted:
            return True
        if not digest:
            return False
        if kind == "plugin" and digest in shipped_plugin_digests():
            return True
        return store().granted_bytes(self.key, digest)

    def withhold(self, line: str) -> None:
        if line not in self.withheld:
            self.withheld.append(line)

    def needs_command(self) -> bool:
        """Should this folder's interface offer `/trust`?

        Only where there is something to decide. A command nobody in this folder
        can act on would sit in `/help` and in the sidebar as an unanswered
        question — and the registry is the one place this project's README is
        tested against, so an always-on entry there is a documentation claim the
        program has to keep honoring in folders where it says nothing.
        """
        return bool(self.withheld or self.decision in (TRUSTED, DECLINED)
                    or self.claims())

    # --- the question ------------------------------------------------------

    def claims(self) -> list[str]:
        """The plain sentence: what this folder would do to BeeCode.

        Read off the files, not off the loaded objects — the user is being asked
        about a folder they have not run yet, and a claim we refused to apply
        still has to be named.
        """
        lines: list[str] = []
        plugins = _project_plugin_files(self.path)
        hidden = [name for name, _d, _d2 in plugins if not self.allow("plugin", name, _d2)]
        if hidden:
            lines.append(L(f"runs {len(hidden)} Python file(s) from its own folder as "
                           f"BeeCode code: {', '.join(hidden[:6])}"
                           + (" …" if len(hidden) > 6 else ""),
                           f"исполняет {len(hidden)} Python-файл(ов) из своей папки как код "
                           f"BeeCode: {', '.join(hidden[:6])}"
                           + (" …" if len(hidden) > 6 else "")))
        gate = _config_gate_claims(self.path)
        lines.extend(gate)
        servers = _project_mcp_claims(self.path, self)
        if servers:
            lines.append(L(f"registers {servers} MCP server(s) defined in .beeagent/mcp.json",
                           f"регистрирует серверов(ов) MCP: {servers} из .beeagent/mcp.json"))
        return lines

    def question(self) -> list[str]:
        """The whole block the user is shown: claims, what was skipped, what to type."""
        claims = self.claims()
        head = L(f"BeeCode found a folder that wants to change how BeeCode behaves:",
                 "BeeCode нашёл папку, которая хочет изменить поведение BeeCode:")
        body = [head, f"  {self.path}", ""]
        body.append("  " + L("It would:", "Он хочет:"))
        body.extend(f"    - {line}" for line in claims)
        body.append("")
        body.append("  " + L("None of that was applied. What BeeCode did NOT do:",
                             "Ничего из этого не применено. Что BeeCode НЕ сделал:"))
        body.extend(f"    - {line}" for line in (self.withheld or
                  [L("(nothing was withheld)", "(ничего не удержано)")]))
        body.append("")
        body.append("  " + L("Trust this folder and load what it asks? Answer once; "
                             "the answer is kept in ~/.beecode/trusted.json for this "
                             "folder only.",
                             "Доверить эту папку и загрузить то, что она просит? "
                             "Ответ один раз; он хранится в ~/.beecode/trusted.json "
                             "только для этой папки."))
        return body

    def announce(self) -> str | None:
        """Ask once where a question can be answered, otherwise say what to type.

        Returns the decision, or None when nobody was asked. The rule of this
        project applies here: the answer never defaults to "yes" on the user's
        behalf, and a folder whose plugins were skipped is never described as
        having them active.
        """
        if self.announced or not self.withheld or not self.claims():
            return None
        self.announced = True
        block = self.question()
        if self._can_ask():
            _emit(block)
            _emit([L("  [y/N] — yes loads this folder, no keeps refusing it:",
                     "  [y/N] — да: доверить папку, нет: отказать:")])
            answer = self._read_line()
            if answer is None:
                _emit([L("  (no answer — nothing was loaded. /trust yes or /trust no "
                         "decides this folder later.)",
                         "  (ответа нет — ничего не загружено. /trust yes или /trust no "
                         "решат это позже.)")])
                return None
            yes = answer.strip().lower() in ("y", "yes", "да", "д")
            if yes and self.say_trusted():
                _emit([L("  trusted — see /extensions for what loaded.",
                         "  доверяю — что загрузилось: /extensions.")])
                return TRUSTED
            self.say_declined()
            _emit([L("  refused for this folder. /trust yes changes your mind, "
                     "/trust reset forgets the answer.",
                     "  отказ для этой папки. /trust yes передумать, "
                     "/trust reset забыть ответ.")])
            return DECLINED
        _emit(block)
        _emit([L("  to allow this folder:  /trust yes      to refuse it:  /trust no",
                 "  разрешить папку:  /trust yes      отказать:  /trust no")])
        return None

    def _can_ask(self) -> bool:
        """Only a terminal that can hear a typed answer — and only before an
        interface owns the screen: the REPL banner and the Textual app both start
        after `Agent()`, and a nested prompt inside a running event loop would
        hang the loop that is supposed to draw the question."""
        if os.environ.get("BEECODE_TRUST_PROMPT", "").strip().lower() in ("0", "off", "no"):
            return False
        if self.decision in (TRUSTED, DECLINED):
            return False
        try:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                return False
        except (AttributeError, ValueError, OSError):
            return False
        try:
            import asyncio
            asyncio.get_running_loop()
            return False                # a UI loop is already on this terminal
        except RuntimeError:
            return True

    def _read_line(self) -> str | None:
        try:
            return input()
        except (EOFError, OSError, KeyboardInterrupt, RuntimeError):
            return None

    def say_trusted(self) -> bool:
        """Yes — and whatever was skipped for want of it stops being a complaint."""
        if not store().set_decision(self.key, TRUSTED, "; ".join(self.claims())):
            return False
        self.withheld.clear()
        return True

    def say_declined(self) -> bool:
        return store().set_decision(self.key, DECLINED, "; ".join(self.claims()))

    def reset(self) -> bool:
        """Forget this folder's answer, so the next start asks about it again."""
        self.withheld.clear()
        self.announced = False
        return store().clear(self.key)


def for_folder(workdir=".") -> ProjectTrust:
    """The gate of one folder, one object per process, so a folder is asked about once."""
    key = folder_key(workdir)
    gate = _GATES.get(key)
    if gate is None:
        gate = ProjectTrust(workdir, enforced=True)
        _GATES[key] = gate
    return gate


def unenforced_gate(workdir=".") -> ProjectTrust:
    """A gate that lets everything through: an embedding program vouching for bytes.

    Deliberately not the cached folder gate — a hand-built `PluginLoader` must not
    be able to disarm the guard `Agent()` put up for the same folder.
    """
    return ProjectTrust(workdir, enforced=False)


def record_install(project_root, digest: str, name: str = "",
                   kind: str = "plugin", how: str = "install") -> None:
    """Called when BeeCode writes extension bytes into a folder because the user asked.

    Installing is the act of trust; running it the next time is not a new one. The
    record lives in the user's home beside their decisions, so a folder that
    merely *contains* `.beeagent/plugins/thing/plugin.py` has not installed it.
    """
    try:
        if digest:
            store().grant_bytes(folder_key(project_root), digest,
                                name=name, kind=kind, how=how)
    except Exception:
        pass


def withheld_line(kind: str, name: str, where="") -> str:
    """What to call a piece of the folder that was not applied."""
    if kind == "mcp":
        return L(f"MCP server \"{name}\" from {where}: not registered",
                 f"сервер MCP «{name}» из {where}: не зарегистрирован")
    return L(f"plugin \"{name}\" ({where}): its plugin.py was NOT executed",
             f"плагин «{name}» ({where}): plugin.py НЕ исполнялся")


def apply_grant(agent, gate: ProjectTrust) -> list[str]:
    """A "y" at the question has to do exactly what `/trust yes` does.

    The plugins are re-loaded by the caller; this is the other half: the gate the
    folder's own `beeagent.json` asked for, which was withheld at load time.
    """
    return _restore_config_gate(agent, gate)


def report(text: str) -> None:
    """One line of the result, on the terminal that is already showing the answer."""
    _emit([text])


# --- the reading of the files (used for the question, never for a decision) ---

def _plugins_json(path: Path) -> dict:
    try:
        raw = json.loads((path / ".beeagent" / "plugins.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    installed = raw.get("installed") if isinstance(raw, dict) else None
    return installed if isinstance(installed, dict) else {}


def _project_plugin_files(path: Path) -> list[tuple[str, Path, str]]:
    """(name, entry point, digest) for everything the folder says BeeCode should run."""
    root = path / ".beeagent" / "plugins"
    out = []
    for name, entry in sorted(_plugins_json(path).items()):
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        if entry.get("type") == "mcp":
            continue
        base = root / name
        if not base.is_dir():
            continue
        found = None
        for depth in (0, 1, 2):
            pattern = "plugin.py" if depth == 0 else "/".join(["*"] * depth) + "/plugin.py"
            hits = sorted(base.glob(pattern))
            if hits:
                found = hits[0]
                break
        if found is not None:
            out.append((name, found, digest_file(found)))
    return out


def _config_gate_claims(path: Path) -> list[str]:
    try:
        raw = json.loads((path / "beeagent.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    permissions = raw.get("permissions")
    permissions = permissions if isinstance(permissions, dict) else {}
    lines = []
    mode = str(permissions.get("mode") or "")
    if mode and mode != "ask":
        what = {
            "auto": L("every tool, `bash` and `write` included, would run without asking",
                      "каждый инструмент, включая bash и write, запускался бы без спроса"),
            "readonly": L("reading tools only would run", "запускались бы только инструменты чтения"),
        }.get(mode, L(f"the permission gate would become \"{mode}\"",
                      f"уровень допуска стал бы \"{mode}\""))
        lines.append(L(f"beeagent.json sets permissions.mode to \"{mode}\" — {what}",
                       f"beeagent.json задаёт permissions.mode = \"{mode}\" — {what}"))
    allowed = permissions.get("allowed")
    if isinstance(allowed, list) and allowed:
        names = ", ".join(str(a) for a in allowed[:8]) or ""
        lines.append(L(f"beeagent.json pre-grants {len(allowed)} tool(s) without you being "
                       f"asked: {names}",
                       f"beeagent.json заранее разрешает инструментов: {names} — без вопроса"))
    vpn = str(raw.get("vpn_command") or "").strip()
    if vpn:
        lines.append(L("beeagent.json names a vpn_command, which BeeCode would run through "
                       "a shell after one confirmation",
                       "beeagent.json задаёт vpn_command — BeeCode запустил бы его через "
                       "оболочку после одного подтверждения"))
    return lines


def _project_mcp_claims(path: Path, gate: "ProjectTrust") -> int:
    try:
        raw = json.loads((path / ".beeagent" / "mcp.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0
    servers = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        return 0
    hidden = 0
    for name, cfg in servers.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", True):
            continue
        digest = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode("utf-8")).hexdigest()
        if not gate.allow("mcp", str(name), digest):
            hidden += 1
    return hidden


# --- the one command the user types ------------------------------------------

COMMAND_NAME = "trust"
USAGE = "/trust [yes|no|reset]"
DESCRIPTION = "Let this folder change how BeeCode behaves (plugins, permission gate)"


def register_command() -> bool:
    """Put `/trust` into the shared command machinery.

    Both interfaces go through `commands.dispatch`, so one registration covers
    the classic REPL, the Textual TUI (the sidebar reads COMMANDS at mount) and
    `beecode` one-shot runs. `add_command`/`HANDLERS` are the same pair the
    extension API uses for a contributed command; the category is a core one so
    no plugin can take the name back out.
    """
    from beeagent.ui import commands as core

    existing = next((c for c in core.COMMANDS if c.name == COMMAND_NAME), None)
    if existing is not None and core.HANDLERS.get(COMMAND_NAME) is _cmd_trust:
        return True
    if existing is None:
        core.add_command(COMMAND_NAME, DESCRIPTION, usage=USAGE, category="engine")
    core.HANDLERS[COMMAND_NAME] = _cmd_trust
    return True


def _cmd_trust(ctx, args):
    """`/trust` asks, `/trust yes` allows, `/trust no` refuses, `/trust reset` forgets."""
    from beeagent.ui.commands import CommandResult
    from rich.text import Text

    workdir = getattr(getattr(ctx, "agent", None), "workdir", None) or "."
    gate = for_folder(workdir)
    word = (args[0].lower() if args else "")

    if word in ("yes", "y", "allow", "add", "trust"):
        return CommandResult(output=Text(_grant_and_apply(ctx, gate), style="bold"))
    if word in ("no", "n", "deny", "refuse"):
        gate.say_declined()
        return CommandResult(output=Text(L(
            "Refused for this folder, and the refusal sticks: BeeCode will not ask "
            "again. /trust reset lets it ask once more.",
            "Отказ для этой папки запомнен: BeeCode больше не спросит. "
            "/trust reset — чтобы спросить снова."), style="bold"))
    if word in ("reset", "forget", "remove", "revoke"):
        cleared = gate.reset()
        return CommandResult(output=Text(L(
            f"{gate.path}: your answer is forgotten" + ("" if cleared else " (there was none)"),
            f"{gate.path}: ответ забыт" + ("" if cleared else " (его и не было)")),
            style="bold"))
    if word:
        return CommandResult(output=Text(f"usage: {USAGE}", style="bold"))

    claims = gate.claims()
    lines = [L(f"folder: {gate.path}", f"папка: {gate.path}")]
    decision = gate.decision or L("no answer yet", "ответа ещё нет")
    lines.append(L(f"your answer for it: {decision}", f"твой ответ: {decision}"))
    if claims:
        lines.append(L("what this folder asks for:", "что эта папка просит:"))
        lines.extend(f"  - {c}" for c in claims)
    else:
        lines.append(L("nothing that needs trusting: it ships no plugin BeeCode "
                       "has not already seen",
                       "ничего такого, чего нужно бы доверять: своих незнакомых "
                       "плагинов в ней нет"))
    if gate.withheld:
        lines.append(L("skipped until you allow it:", "не загружено, пока не разрешить:"))
        lines.extend(f"  - {w}" for w in gate.withheld)
    lines.append(L("commands: /trust yes · /trust no · /trust reset",
                   "команды: /trust yes · /trust no · /trust reset"))
    return CommandResult(output=Text("\n".join(lines)))


def _grant_and_apply(ctx, gate: ProjectTrust) -> str:
    """Trust this folder, then say exactly what that turned on."""
    agent = getattr(ctx, "agent", None)
    if not gate.say_trusted():
        return L("I cannot write a decision here — there is no ~/.beecode on this "
                 "machine, so nothing was loaded.",
                 "Я не могу записать решение: на этой машине нет ~/.beecode, "
                 "поэтому ничего не загружено.")
    before = sorted(getattr(getattr(agent, "plugins", None), "tool_names", []) or [])
    report = [L(f"{gate.path} is trusted for this machine.",
                f"{gate.path} доверена этой машине.")]

    restored = _restore_config_gate(agent, gate)
    report.extend(restored)

    if agent is not None and hasattr(agent, "reload_extensions"):
        errors = agent.reload_extensions() or []
        after = sorted(getattr(agent.plugins, "tool_names", []) or [])
        added = [t for t in after if t not in before]
        plugin_names = [name for name, _p, _d in _project_plugin_files(gate.path)]
        if plugin_names:
            report.append(L(f"plugins now loaded from this folder: "
                            f"{', '.join(plugin_names)}",
                            f"плагины загружены из этой папки: "
                            f"{', '.join(plugin_names)}"))
        if added:
            report.append(L(f"tools they added: {', '.join(added)}",
                            f"инструменты: {', '.join(added)}"))
        for line in errors:
            report.append(L(f"a plugin failed: {line}", f"сбой плагина: {line}"))
    else:
        report.append(L("no agent here: the plugins load the next time BeeCode starts.",
                        "агента нет: плагины загрузятся при следующем запуске BeeCode."))
    if not restored:
        report.append(L("beeagent.json left the permission gate at its defaults; "
                        "/permissions shows it, /allow grants one tool at a time.",
                        "beeagent.json не менял уровень допуска; /permissions покажет, "
                        "/allow разрешит по одному инструменту."))
    return "\n".join(report)


def _restore_config_gate(agent, gate: ProjectTrust) -> list[str]:
    """Apply what the folder's `beeagent.json` asked for, now that the user said yes.

    The values were dropped at load time, not lost: re-reading the file through the
    same loader with trust on is the only path that gets them, so a grant cannot be
    talked into *inventing* a permission the file never named — and the folder
    re-read is the one that was just trusted, not whatever the process' cwd says.
    """
    config = getattr(agent, "config", None)
    if config is None or getattr(config, "permissions", None) is None:
        return []
    from beeagent.config.loader import load_config

    try:
        raw = load_config(str(gate.path))
    except Exception:
        return []
    lines = []
    config = agent.config
    if raw.permissions.mode != config.permissions.mode:
        lines.append(L(f"permissions.mode is now \"{raw.permissions.mode}\" "
                       f"(beeagent.json said so)",
                       f"permissions.mode = \"{raw.permissions.mode}\" "
                       f"(так в beeagent.json)"))
        config.permissions.mode = raw.permissions.mode
        permissions = getattr(agent, "permissions", None)
        if permissions is not None:
            permissions.set_mode(raw.permissions.mode)
    extra = [a for a in raw.permissions.allowed if a not in config.permissions.allowed]
    if extra:
        lines.append(L(f"allowed by this folder's config: {', '.join(extra)}",
                       f"разрешено его же config: {', '.join(extra)}"))
    config.permissions.allowed = sorted(set(config.permissions.allowed)
                                        | set(raw.permissions.allowed))
    permissions = getattr(agent, "permissions", None)
    if permissions is not None:
        permissions.granted.update(raw.permissions.allowed)
    if raw.vpn_command and raw.vpn_command != config.vpn_command:
        config.vpn_command = raw.vpn_command
        lines.append(L("vpn_command is set from beeagent.json — it only runs after "
                       "you confirm it on screen",
                       "vpn_command взят из beeagent.json — он запустится только "
                       "после подтверждения на экране"))
    return lines


# --- output ------------------------------------------------------------------

def _emit(lines) -> None:
    """One block of plain text on the terminal the agent was started on.

    Rich's console is what the rest of the UI writes with, and it survives a
    terminal whose code page cannot show a character; a bare print() would raise
    there and take startup down with it.
    """
    text = "\n".join(str(line) for line in lines)
    try:
        from beeagent.ui.components import console

        console.print(text, style="bold", markup=False, highlight=False)
        return
    except Exception:
        pass
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except Exception:
        pass
