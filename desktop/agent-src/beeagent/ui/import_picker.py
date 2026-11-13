"""`/providers import` on the Textual screen: one list, one button per row.

The classic REPL and the fallback path (`_providers_import` in
`beeagent/ui/commands.py`) already print a numbered table and take
`/providers import <number|name>` as a follow-up command. This screen is the
same scan, the same `FoundProvider` objects, the same `agent_import.import_provider`
door — just with a clickable "Add" button beside each importable row instead
of a number to retype. Nothing here decides differently what is safe to
import; it only saves a second command.
"""
from __future__ import annotations

from textual.app import Binding, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from beeagent.i18n import L
from beeagent.ui.components import HONEY, LEAF, bee_title

HIVE_BACKDROP = "#08170a"
HIVE_PANEL = "#0d2410"
ROW_BG = "#0f2a12"


class ImportPicker(ModalScreen):
    """Lists what `agent_import.scan()` found; "Add" saves one right there."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    CSS = f"""
    #ip-backdrop {{
        width: 100%;
        height: 100%;
        align: center middle;
        background: {HIVE_BACKDROP};
    }}
    #ip-box {{
        width: 100;
        max-width: 96%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: {HIVE_PANEL};
        border: heavy {LEAF};
    }}
    #ip-title {{ width: 1fr; color: {HONEY}; text-style: bold; }}
    #ip-note {{ width: 1fr; color: {LEAF}; margin-bottom: 1; }}
    #ip-list {{ height: auto; max-height: 22; }}
    .ip-agent-row {{ width: 1fr; color: {HONEY}; text-style: bold; margin-top: 1; }}
    .ip-row {{ width: 1fr; height: auto; margin-top: 0; align: left middle; }}
    .ip-desc {{ width: 1fr; padding-right: 2; }}
    .ip-add {{ min-width: 10; }}
    #ip-buttons {{ width: 1fr; margin-top: 1; align-horizontal: right; }}
    """

    def __init__(self, findings: list, numbered: list) -> None:
        super().__init__(id="import-picker")
        self.findings = findings
        self.numbered = numbered      # FoundProvider objects, index == button suffix

    def compose(self) -> ComposeResult:
        from beeagent.core import provider_setup

        with Vertical(id="ip-backdrop"):
            with Vertical(id="ip-box"):
                yield Label(bee_title(L("Import providers", "Импорт провайдеров")),
                            id="ip-title")
                yield Label(L("read-only scan of other agents on this machine — "
                              "click Add to save one through /key's own door",
                              "только чтение — Add сохраняет запись через ту же "
                              "дверь, что и /key"), id="ip-note")
                with VerticalScroll(id="ip-list"):
                    for finding in self.findings:
                        if not finding.found and not finding.providers:
                            continue
                        yield Static(f"{finding.label} — {finding.detail or L('found', 'найден')}",
                                     classes="ip-agent-row")
                        for provider in finding.providers:
                            index = next((i for i, p in enumerate(self.numbered)
                                         if p is provider), None)
                            tail = provider_setup.mask_key(provider.key) if provider.key else ""
                            key_bit = L("key " + tail, "ключ " + tail) if tail \
                                else L("no key", "без ключа")
                            what = provider.url or L("built-in endpoint", "встроенный эндпоинт")
                            text = f"{provider.name} — {what} · {key_bit}"
                            if provider.note:
                                text += f" · {provider.note}"
                            with Horizontal(classes="ip-row"):
                                yield Static(text, classes="ip-desc")
                                if index is not None:
                                    yield Button(
                                        L("Add", "Добавить"), id=f"ip-add-{index}",
                                        classes="ip-add", variant="success")
                with Horizontal(id="ip-buttons"):
                    yield Button(L("close", "закрыть"), id="ip-close", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "ip-close":
            self.dismiss("")
            return
        if bid.startswith("ip-add-"):
            index = int(bid[len("ip-add-"):])
            if 0 <= index < len(self.numbered):
                self.dismiss(self.numbered[index])

    def action_cancel(self) -> None:
        self.dismiss("")


def open_picker(ctx, findings: list, numbered: list, workdir: str = ".") -> tuple:
    """Push `ImportPicker` if a Textual app is running. Returns `(message, refusal)`;
    an empty message with no refusal means "the screen is up, nothing saved yet"."""
    from beeagent.core import agent_import
    from beeagent.ui.editor import running_app

    app = running_app()
    if app is None:
        return "", L("no screen to open here — use /providers import <number|name> instead",
                     "экрана нет — используйте /providers import <номер|имя>")

    def _answered(pick) -> None:
        note = getattr(app, "_note", None)
        if not pick:
            if callable(note):
                note("🔑", "nothing was imported", "ничего не импортировано")
            return
        message, refusal = agent_import.import_provider(
            ctx.config, ctx.agent, pick,
            workdir=ctx.agent.workdir if ctx.agent is not None else workdir)
        text = refusal or message
        if callable(note) and text:
            note("🔑", text, text)

    app.push_screen(ImportPicker(findings, numbered), _answered)
    return (L("the import list is on screen — Add saves a row, Esc closes it",
              "список импорта на экране — Add сохраняет строку, Esc закрывает"), "")
