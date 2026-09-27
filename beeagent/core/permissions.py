"""Which tools may run without asking.

`BaseTool.is_safe()` marks the tools that only read. Everything else — writing
files, running a shell, plugin and MCP tools — needs a grant: a wrong guess by
a free model costs the user real work, and no answer from the model counts as
permission to touch the disk.
"""
from __future__ import annotations

from beeagent.i18n import L

ASK = "ask"
AUTO = "auto"
READONLY = "readonly"
MODES = (ASK, AUTO, READONLY)

MODE_HELP = {
    ASK: L("unsafe tools run only after /allow <tool>",
           "опасные инструменты — только после /allow <инструмент>"),
    AUTO: L("every tool runs without asking (trust the model)",
            "все инструменты исполняются без спроса (доверие модели)"),
    READONLY: L("reading tools only; nothing is written and nothing leaves the machine",
                "только чтение; ничего не записывается и не уходит наружу"),
}


def _norm(name: str) -> str:
    """One spelling for tool names: JSON grants, /allow and the registry."""
    return (name or "").strip().lower()


class Permissions:
    def __init__(self, mode: str = ASK, allowed: list[str] | None = None):
        self.mode = mode if mode in MODES else ASK
        # "Bash" / " bash " from beeagent.json never matched tool.name == "bash",
        # so the grant silently did nothing. Normalise on the way in.
        self.granted: set[str] = {_norm(n) for n in (allowed or ()) if _norm(n)}
        # Tools the user was asked about and refused this session, so the model
        # gets one clear no instead of asking the user over and over.
        self.denied_this_run: set[str] = set()

    def set_mode(self, mode: str) -> bool:
        if mode not in MODES:
            return False
        self.mode = mode
        return True

    def grant(self, name: str) -> None:
        key = _norm(name)
        if not key:
            return
        self.granted.add(key)
        self.denied_this_run.discard(key)

    def revoke(self, name: str) -> bool:
        key = _norm(name)
        if key not in self.granted:
            return False
        self.granted.discard(key)
        return True

    def allows(self, tool) -> bool:
        """Can this tool run right now?

        `readonly` is a ceiling, not a queue: an earlier grant does not unlock a
        tool that changes the machine there. And a plugin or MCP server vouches
        for itself, which is a claim made by code the user installed from
        somewhere else — such a tool needs an explicit grant even when it says
        it is safe.
        """
        if self.mode == AUTO:
            return True
        # A plugin's is_safe() is attacker-controlled code: an exception in it
        # must deny, never crash the loop.
        try:
            claimed_safe = bool(tool.is_safe()) if hasattr(tool, "is_safe") else False
        except Exception:
            claimed_safe = False
        try:
            writes = bool(getattr(tool, "writes_files", False))
        except Exception:
            writes = True
        core_safe = not bool(getattr(tool, "from_extension", False)) and claimed_safe
        if self.mode == READONLY:
            # `is_safe()` means "only reads"; a tool can be safe to look with and
            # still write its own state file. Read-only means nothing changes.
            # A missing writes_files attribute defaults to writable (deny).
            if not hasattr(tool, "writes_files"):
                return False
            return core_safe and not writes
        return core_safe or _norm(getattr(tool, "name", "")) in self.granted

    def refusal(self, tool) -> str:
        """The text handed back to the model — and shown to the user."""
        name = getattr(tool, "name", "?")
        if self.mode == READONLY:
            base = L(f"Permission denied: BeeCode runs in read-only mode, "
                     f"`{name}` would change the machine. "
                     f"Ask the user to run /permissions ask or /permissions auto.",
                     f"Разрешения нет: BeeCode в режиме только чтения, `{name}` меняет систему. "
                     f"Попроси пользователя выполнить /permissions ask или /permissions auto.")
        else:
            base = L(f"Permission denied: the user has not allowed `{name}` this session. "
                     f"Do not retry it. Tell the user they can run /allow {name} "
                     f"(or /permissions auto) and then ask you to continue.",
                     f"Разрешения нет: пользователь не разрешил `{name}` на эту сессию. "
                     f"Не повторяй вызов. Скажи пользователю, что можно выполнить /allow {name} "
                     f"(или /permissions auto) и попросить тебя продолжить.")
        if name in self.denied_this_run:
            # The model asked again anyway: end the run instead of burning the
            # remaining turns on a call that will never be granted.
            base += L(" This was already refused earlier in this run — answer in "
                      "plain text and stop calling tools.",
                      " Это уже отклонено в этом запуске — отвечай текстом и "
                      "перестань звать инструменты.")
        return base

    def describe(self) -> str:
        granted = ", ".join(sorted(self.granted)) or L("none", "ни одного")
        return L(f"mode {self.mode} · allowed by hand: {granted}",
                 f"режим {self.mode} · разрешено вручную: {granted}")

    def prompt_section(self, tools) -> str:
        """What the model is told about the gate, so it stops fighting it."""
        def _unsafe(tool) -> bool:
            try:
                claimed = bool(tool.is_safe())
            except Exception:
                return True
            # An extension's "safe" still needs a grant — do not advertise it
            # as "reading tools always work".
            return (not claimed) or bool(getattr(tool, "from_extension", False))
        unsafe = [t.name for t in tools.list_tools() if _unsafe(t)]
        if not unsafe or self.mode == AUTO:
            return ""
        blocked = [n for n in unsafe if n not in self.granted]
        lines = ["# PERMISSIONS"]
        if self.mode == READONLY:
            lines.append(
                "This session is read-only: " + ", ".join(unsafe) + " are refused by the user. "
                "Never claim you changed a file or ran a command — show the patch or the "
                "command as text and let the user run it.")
        elif blocked:
            lines.append(
                "Tools that change the machine run only after the user granted them. "
                "Already granted: " + (", ".join(sorted(self.granted)) or "none") + ". "
                "Not granted (they will be refused): " + ", ".join(blocked) + ". "
                "If a tool is refused, do not retry it: tell the user what you wanted to run "
                "and that /allow <tool> unlocks it. Reading tools always work.")
        else:
            return ""
        return "\n".join(lines)
