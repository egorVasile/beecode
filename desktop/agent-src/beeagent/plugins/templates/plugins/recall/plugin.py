"""Find the conversation where a decision was made.

Sessions are one JSON file each, and `/sessions` lists them by date — which is
no help at all when you remember the sentence and not the day. `/recall` searches
the text of every saved transcript and names the one to reopen with `/continue`.
(``/find`` is BeeCode's own file lookup, so this reaches for a different word.)
"""
import json
import re
import time
from pathlib import Path

MAX_FILES = 300
SNIPPET = 90


def _root(api) -> Path:
    agent = getattr(api, "agent", None)
    return Path(getattr(agent, "workdir", ".") or ".")


def _content(message: dict) -> str:
    body = message.get("content")
    if isinstance(body, str):
        return body
    if isinstance(body, list):        # some providers return parts
        return " ".join(str(part.get("text", part)) if isinstance(part, dict) else str(part)
                        for part in body)
    return ""


def _snippet(text: str, needle: str) -> str:
    at = text.lower().find(needle)
    if at < 0:
        return ""
    start = max(0, at - SNIPPET // 2)
    fragment = re.sub(r"\s+", " ", text[start:at + len(needle) + SNIPPET // 2]).strip()
    return ("…" if start else "") + fragment + "…"


def _scan(api, needle: str) -> list[dict]:
    directory = _root(api) / ".beeagent" / "sessions"
    if not directory.is_dir():
        return []
    hits = []
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:MAX_FILES]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue        # a torn transcript is not a result
        matched = []
        for message in data.get("messages", []):
            text = _content(message)
            if needle in text.lower():
                matched.append((message.get("role", "?"), _snippet(text, needle)))
        if matched:
            hits.append({
                "id": data.get("id") or path.stem,
                "when": path.stat().st_mtime,
                "count": len(matched),
                "first": matched[0],
            })
    return hits


def setup(api) -> None:
    def find(ctx, args):
        from beeagent.ui.commands import CommandResult
        from rich.text import Text

        needle = " ".join(args).strip().lower()
        if not needle:
            return CommandResult(output=Text("usage: /recall <words from an old answer>",
                                             style="dim"))
        hits = _scan(api, needle)
        if not hits:
            total = len(list((_root(api) / ".beeagent" / "sessions").glob("*.json"))) \
                if (_root(api) / ".beeagent" / "sessions").is_dir() else 0
            return CommandResult(output=Text(
                f"nothing in {total} saved sessions matches “{needle}”", style="dim"))
        return CommandResult(output=_table(api, hits, needle))

    api.command("recall", "Search every saved conversation for a phrase", find,
                usage="/recall <текст>")


def _table(api, hits: list[dict], needle: str):
    from rich.table import Table

    from beeagent.ui import skin

    kwargs = skin.frame_kwargs("#ffcc00")
    table = Table(title=f"🐝 /recall “{needle}” — {len(hits)} sessions", show_header=True,
                  header_style="bold #ffcc00", **kwargs)
    table.add_column("when", style="dim")
    table.add_column("session")
    table.add_column("hits", justify="right", style="dim")
    table.add_column("first match")
    for hit in hits[:15]:
        when = time.strftime("%m-%d %H:%M", time.localtime(hit["when"]))
        role, text = hit["first"]
        table.add_row(when, hit["id"], str(hit["count"]), f"[{role}] {text}"[:110])
    if len(hits) > 15:
        table.add_row("", "", "", f"… {len(hits) - 15} more")
    return table
