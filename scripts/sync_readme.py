"""Regenerate the slash-command reference in README.md from the live registry.

The table is generated, not hand-written, so a new command cannot be documented
wrongly or forgotten. Run it after touching `beeagent/ui/commands.py`:

    python scripts/sync_readme.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beeagent.ui.commands import COMMANDS  # noqa: E402

BEGIN = "<!-- COMMANDS:BEGIN -->"
END = "<!-- COMMANDS:END -->"


def build_table() -> str:
    rows = ["| Command | What it does | Usage |", "| --- | --- | --- |"]
    by_category: dict[str, list] = {}
    for command in COMMANDS:
        by_category.setdefault(command.category, []).append(command)
    labels = {
        "info": "Info and status", "engine": "Model, provider, mode", "session": "Sessions",
        "git": "Git", "tools": "Run tools directly", "files": "Files",
        "extensions": "Skills, plugins, MCP", "general": "General",
    }
    for category in sorted(by_category, key=lambda c: (c != "general", c)):
        rows += ["", f"### {labels.get(category, category).title()}", ""]
        rows.append("| Command | What it does | Usage |")
        rows.append("| --- | --- | --- |")
        for command in sorted(by_category[category], key=lambda c: c.name):
            usage = command.usage or f"/{command.name}"
            rows.append(f"| `/{command.name}` | {command.description} | `{usage}` |")
    return "\n".join(rows)


def main() -> None:
    path = ROOT / "README.md"
    text = io.open(path, encoding="utf-8").read()
    table = f"{BEGIN}\n{build_table()}\n{END}"
    if BEGIN in text and END in text:
        head = text.split(BEGIN)[0]
        tail = text.split(END, 1)[1]
        text = head + table + tail
    else:
        text = text.replace("<!-- COMMANDS_TABLE -->", table)
    io.open(path, "w", encoding="utf-8").write(text)
    print(f"README.md: {len(COMMANDS)} commands documented")


if __name__ == "__main__":
    main()
