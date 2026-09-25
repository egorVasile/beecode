"""`/edit-ui <file>` — open a file in a screen the *human* can type into.

The agent's own `edit` tool is untouched by this pack and is not advertised
here: this is the other half of the loop. The model changed something, the
person wants to look at it and fix one line without leaving BeeCode, losing
their place or handing the file to another program. So this plugin contributes
exactly one slash command and no tool, and the command contributes one screen.

Everything about the file itself — where it may be opened from, how it is read,
whether the previous bytes are recoverable, how it lands back on disk — lives in
`beeagent/ui/editor.py`, which nothing imports unless this command is used.
"""
from beeagent.i18n import L

USAGE = "/edit-ui <file>"


def setup(api) -> None:
    # Two settings, both overridable in beeagent.json under
    # {"extensions": {"edit-ui": {...}}}: a 4 GB log and a phone screen want
    # different windows into the same file.
    max_lines = api.setting("max_lines", 5000,
                            L("lines the editor loads at most",
                              "строк редактор загружает максимум"))
    max_bytes = api.setting("max_bytes", 512 * 1024,
                            L("bytes the editor loads at most",
                              "байт редактор загружает максимум"))
    api.command("edit-ui", "Open a file in the editor screen (human mode)",
                _make_command(api, max_lines, max_bytes), usage=USAGE)


def _workdir(api, ctx) -> str:
    """The folder the undo journal belongs to — the agent's, wherever it came from.

    `api.agent` is what the loader handed this plugin; `ctx.agent` is what the
    command line was dispatched against. They are the same object in a running
    BeeCode and absent in a test that builds a context by hand, so both are
    asked and the working directory defaults to the process' own.
    """
    for owner in (getattr(api, "agent", None), getattr(ctx, "agent", None)):
        workdir = str(getattr(owner, "workdir", "") or "")
        if workdir:
            return workdir
    return "."


def _make_command(api, max_lines, max_bytes):
    """Bind the limits this instance settled on, so the handler stays one call."""
    def edit_ui(ctx, args):
        from beeagent.ui.commands import CommandResult
        from rich.text import Text

        target = " ".join(args or []).strip().strip('"').strip("'")
        if not target:
            return CommandResult(output=Text(L(
                "name the file to open: /edit-ui <path> — one file at a time, and it "
                "has to be inside the folder BeeCode was started in",
                "назовите файл: /edit-ui <путь> — по одному, внутри папки, откуда "
                "запущен BeeCode"), style="bold red"))
        try:
            from beeagent.ui import editor
        except Exception as e:                        # noqa: BLE001 - Textual is optional here
            return CommandResult(output=Text(L(
                f"the editor could not load ({e.__class__.__name__}: {e}); it needs "
                f"Textual, which comes with BeeCode",
                f"редактор не загрузился ({e.__class__.__name__}: {e}); ему нужен "
                f"Textual, который идёт в комплекте с BeeCode"), style="bold red"))
        workdir = _workdir(api, ctx)
        message = editor.open_editor(target, workdir=workdir,
                                     max_lines=int(max_lines), max_bytes=int(max_bytes))
        if message:
            return CommandResult(output=Text(message, style="bold red"))
        return CommandResult(output=Text(L(
            "the editor is on screen — Ctrl+S saves, Ctrl+Q or Esc closes it",
            "редактор на экране — Ctrl+S сохраняет, Ctrl+Q или Esc закрывает"),
            style="dim"))

    return edit_ui
