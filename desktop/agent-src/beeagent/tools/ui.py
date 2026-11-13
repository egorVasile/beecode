"""Change the desktop interface and have BeeCode apply it live.

The loop the system prompt teaches: the user asks for a look ("make it green",
"brutal please"), the model calls `ui action=set ...` (or `css` with its own
stylesheet), and the open window picks the change up within seconds — no
reload, no rebuild. What lands answers from the next poll, because the desktop
re-reads `/api/ui` on a beat and applies skin, accent, theme and the custom
stylesheet without restarting.

Safety is the project's own stack, not a new one: everything the model may
touch lives under `.beeagent/ui/` inside the working directory — the same jail
as every other file tool — so the application's own code is never rewritten.
Only validated values are stored (known skins, hex accents, the two themes),
and the custom stylesheet is CSS alone: anything with a `<` in it is refused,
so markup and script cannot ride in through the style link. This tool itself
writes files — granting it is a decision, in `ask` mode a question per session.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .base import BaseTool, ToolResult

UI_DIR = ".beeagent/ui"
PREFS_FILE = ".beeagent/ui/prefs.json"
CSS_FILE = ".beeagent/ui/custom.css"
CSS_LIMIT = 20000

SKINS = ("brutal", "paper")
THEMES = ("paper", "zinc")          # light / dark — the same words the app uses
HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
COMPONENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
SLOTS = ("sidebar", "main", "composer-wrap")


def _base(workdir: str = "") -> Path:
    from pathlib import Path as _Path

    import os

    return _Path(workdir or os.getcwd())


def _ui_path(name: str, workdir: str = "") -> Path:
    root = _base(workdir).resolve()
    path = root / name
    if not path.resolve().is_relative_to(root):
        raise OSError("UI settings path is outside the project")
    return path


def read_prefs(workdir: str = "") -> dict:
    """The interface overrides, or the defaults when nothing was ever set."""
    prefs = {"skin": None, "accent": None, "theme": None,
             "hidden_components": [], "components": [],
             "updated_at": 0.0, "version": 0}
    try:
        raw = json.loads(_ui_path(PREFS_FILE, workdir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return prefs
    if not isinstance(raw, dict):
        return prefs
    if raw.get("skin") in SKINS:
        prefs["skin"] = raw["skin"]
    if isinstance(raw.get("accent"), str) and HEX.match(raw["accent"]):
        prefs["accent"] = raw["accent"]
    if raw.get("theme") in THEMES:
        prefs["theme"] = raw["theme"]
    hidden = raw.get("hidden_components")
    if isinstance(hidden, list):
        prefs["hidden_components"] = [x for x in hidden[:100]
                                      if isinstance(x, str) and COMPONENT_ID.fullmatch(x)]
    components = raw.get("components")
    if isinstance(components, list):
        for item in components[:30]:
            if (isinstance(item, dict) and isinstance(item.get("id"), str)
                    and COMPONENT_ID.fullmatch(item["id"])
                    and item.get("kind") in ("text", "notice", "button")
                    and item.get("slot") in SLOTS
                    and isinstance(item.get("text"), str) and len(item["text"]) <= 1000
                    and item.get("button_action", "") in ("", "settings", "new_chat")):
                prefs["components"].append({k: item.get(k, "") for k in
                                            ("id", "kind", "slot", "text", "button_action")})
    try:
        prefs["updated_at"] = float(raw.get("updated_at") or 0)
    except (TypeError, ValueError):
        prefs["updated_at"] = 0.0
    try:
        prefs["version"] = int(raw.get("version") or 0)
    except (TypeError, ValueError):
        prefs["version"] = 0
    return prefs


def write_prefs(patch: dict, workdir: str = "") -> dict:
    """Store validated fields, bump the version, and hand the stored row back."""
    prefs = read_prefs(workdir)
    for field in ("skin", "accent", "theme", "hidden_components", "components"):
        if patch.get(field) is not None:
            prefs[field] = patch[field]
    prefs["updated_at"] = time.time()
    prefs["version"] = int(prefs.get("version") or 0) + 1
    from .base import write_text_preserving
    target = _ui_path(PREFS_FILE, workdir)
    target.parent.mkdir(parents=True, exist_ok=True)
    _ui_path(PREFS_FILE, workdir)
    write_text_preserving(target, json.dumps(prefs, ensure_ascii=False))
    return prefs


def custom_css_path(workdir: str = "") -> Path:
    return _ui_path(CSS_FILE, workdir)


class UiTool(BaseTool):
    name = "ui"
    description = ("Change the desktop live: theme, skin, accent, CSS and components. "
                   "Use guide for details; changes require permission.")
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["guide", "list", "set", "css", "add", "remove", "hide", "show", "reset"],
                "description": "Interface action.",
            },
            "skin": {
                "type": "string",
                "enum": list(SKINS),
                "description": "Built-in skin to switch to.",
            },
            "theme": {
                "type": "string",
                "enum": list(THEMES),
                "description": "paper is light, zinc is dark.",
            },
            "accent": {
                "type": "string",
                "description": "Accent color as #rgb or #rrggbb.",
            },
            "text": {
                "type": "string",
                "description": "Text for add or full stylesheet for css (empty clears CSS).",
            },
            "id": {"type": "string", "description": "Component ID, without #"},
            "kind": {"type": "string", "enum": ["text", "notice", "button"]},
            "slot": {"type": "string", "enum": list(SLOTS)},
            "button_action": {"type": "string", "enum": ["settings", "new_chat"]},
        },
        "required": ["action"],
    }
    writes_files = True

    def execute(self, action: str = "guide", skin: str = "",
                theme: str = "", accent: str = "", text: str = "", id: str = "",
                kind: str = "", slot: str = "", button_action: str = "") -> ToolResult:
        action = (action or "").strip().lower()
        if action == "guide":
            return self._guide()
        if action == "list":
            return self._list()
        if action == "set":
            return self._set(skin, theme, accent)
        if action == "css":
            return self._css(text)
        if action == "reset":
            return self._reset()
        if action in ("add", "remove", "hide", "show"):
            return self._component(action, id, kind, slot, text, button_action)
        return ToolResult(
            output="ui: unknown action — guide, list, set, css, add, remove, hide, show, reset.",
            error=True)

    def _guide(self) -> ToolResult:
        return ToolResult(output=(
            "The desktop reads `.beeagent/ui/` and applies it live (no reload):\n"
            "- `set` with skin (brutal/paper), theme (paper=light, zinc=dark), "
            "accent (#rgb/#rrggbb). Only given fields change.\n"
            "- `css` with text installs a full custom stylesheet (max "
            f"{CSS_LIMIT} chars, no `<` — CSS alone, never markup or script). "
            "Empty text clears it. Useful variables the skins define: "
            "--amber (the accent), --ink, --paper, --card, --line.\n"
            "- `add` creates a text, notice or button component in sidebar, main or composer-wrap; "
            "buttons may open settings or start a new chat. `remove` removes an added component.\n"
            "- `hide`/`show` toggle an existing DOM id. Useful IDs: sidebar, new-chat, search, "
            "sessions, gear, chat, composer, input, modelpill, permmode, send, ctxbar. "
            "Restore all overrides with Ctrl+Shift+U.\n"
            "- `list` shows skins and what is active; `reset` restores defaults.\n"
            "Tell the user to look at the open window — it picks the change up "
            "within seconds."), error=False)

    def _list(self) -> ToolResult:
        prefs = read_prefs()
        css = custom_css_path()
        lines = [f"skins: {', '.join(SKINS)}",
                 f"active: skin={prefs['skin'] or '—'} "
                 f"theme={prefs['theme'] or '—'} accent={prefs['accent'] or '—'}",
                 f"custom.css: {'yes' if css.exists() else 'no'}"
                 + (f" ({css.stat().st_size} bytes)" if css.exists() else "")]
        lines.append("hidden IDs: " + (", ".join(prefs["hidden_components"]) or "none"))
        lines.append("added components: " + (", ".join(item["id"] for item in prefs["components"]) or "none"))
        return ToolResult(output="\n".join(lines), error=False)

    def _component(self, action, id, kind, slot, text, button_action):
        ident = str(id or "").strip().removeprefix("#")
        if not COMPONENT_ID.fullmatch(ident):
            return ToolResult("ui: provide a valid component ID (letters, digits, _ or -).", True)
        prefs = read_prefs()
        hidden, items = prefs["hidden_components"], prefs["components"]
        if action in ("hide", "show"):
            if action == "hide" and ident not in hidden:
                if len(hidden) >= 100:
                    return ToolResult("ui: hidden component limit reached.", True)
                hidden.append(ident)
            if action == "show":
                hidden = [value for value in hidden if value != ident]
        elif action == "remove":
            if not any(item["id"] == ident for item in items):
                return ToolResult("ui: added component not found; use hide for built-in IDs.", True)
            items = [item for item in items if item["id"] != ident]
        else:
            if kind not in ("text", "notice", "button") or slot not in SLOTS:
                return ToolResult("ui: choose kind text/notice/button and a valid slot.", True)
            if not isinstance(text, str) or not text.strip() or len(text) > 1000:
                return ToolResult("ui: text must contain 1–1000 characters.", True)
            if kind == "button" and button_action not in ("settings", "new_chat"):
                return ToolResult("ui: choose the button action settings or new_chat.", True)
            if kind != "button" and button_action:
                return ToolResult("ui: button_action applies only to buttons.", True)
            if len(items) >= 30 and not any(item["id"] == ident for item in items):
                return ToolResult("ui: component limit reached.", True)
            items = [item for item in items if item["id"] != ident]
            items.append({"id": ident, "kind": kind, "slot": slot, "text": text,
                          "button_action": button_action})
        write_prefs({"hidden_components": hidden, "components": items})
        return ToolResult(f"ui: {action} {ident}; the window updates shortly.", False)

    def _set(self, skin: str, theme: str, accent: str) -> ToolResult:
        patch: dict = {}
        problems: list[str] = []
        skin = (skin or "").strip().lower()
        theme = (theme or "").strip().lower()
        accent = (accent or "").strip()
        if skin:
            if skin in SKINS:
                patch["skin"] = skin
            else:
                problems.append(f"skin is brutal or paper, not {skin!r}")
        if theme:
            if theme in THEMES:
                patch["theme"] = theme
            else:
                problems.append(f"theme is paper or zinc, not {theme!r}")
        if accent:
            if HEX.match(accent):
                patch["accent"] = accent
            else:
                problems.append(f"accent is #rgb or #rrggbb, not {accent!r}")
        if problems:
            return ToolResult(output="ui: " + "; ".join(problems), error=True)
        if not patch:
            return ToolResult(
                output="ui set: nothing to change — give skin, theme or accent.",
                error=True)
        prefs = write_prefs(patch)
        applied = ", ".join(f"{k}={prefs[k]}" for k in ("skin", "theme", "accent")
                            if prefs[k])
        return ToolResult(output=f"ui: applied {applied} — the open window picks "
                                 f"it up within seconds.", error=False)

    def _css(self, text: str) -> ToolResult:
        text = text or ""
        if len(text) > CSS_LIMIT:
            return ToolResult(
                output=f"ui: stylesheet is {len(text)} chars, the cap is "
                       f"{CSS_LIMIT} — shorten it.",
                error=True)
        if "<" in text:
            return ToolResult(
                output="ui: stylesheets are CSS alone — no `<`, no markup, "
                       "no script.",
                error=True)
        path = custom_css_path()
        if not text.strip():
            try:
                path.unlink()
            except OSError:
                pass
            prefs = write_prefs({})
            return ToolResult(output="ui: custom stylesheet cleared "
                                     f"(v{prefs['version']}).", error=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        from .base import write_text_preserving
        write_text_preserving(path, text)
        prefs = write_prefs({})
        return ToolResult(output=f"ui: custom stylesheet installed "
                                 f"({len(text)} chars, v{prefs['version']}) — "
                                 f"the open window picks it up within seconds.",
                          error=False)

    def _reset(self, workdir: str = "") -> ToolResult:
        for name in (PREFS_FILE, CSS_FILE):
            try:
                _ui_path(name, workdir).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return ToolResult(output="ui: could not clear the saved interface changes.", error=True)
        return ToolResult(output="ui: defaults restored — skin, theme, accent "
                                 "and the custom stylesheet are cleared.",
                          error=False)
