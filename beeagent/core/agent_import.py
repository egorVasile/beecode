"""Borrow providers from the other AI agents on this machine.

OpenCode, Codex, Gemini CLI, Claude Code, Aider, Roo Code, Kilo Code, Cline,
Qoder, Zed and DeepSeek Harness keep their endpoints in their own files. This
module only *reads* those files — nothing is written, moved or sent anywhere
— and hands what it found to `/providers import`, which saves through the
same validated door as `/key`.

Three rules keep this honest:

* a key is never printed whole, anywhere: tables and messages carry tails only;
* a file that does not parse is absence, not an error: the scan never raises;
* every path is tried under the given roots, so tests point it at a sandbox
  instead of the developer's real home.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FoundProvider:
    """One endpoint worth connecting: where it came from and what it needs."""

    agent: str = ""            # detector id, e.g. "opencode"
    name: str = ""             # suggested BeeCode provider name
    url: str = ""              # base URL, "" when unknown
    models: list = field(default_factory=list)
    key: str | None = None     # the value itself — in memory only, never logged
    key_from: str = ""         # "file", "env:VAR" or "" when there is none
    note: str = ""             # why there is no key, when that is the case


@dataclass
class AgentFinding:
    """One agent: found or not, and what it offers."""

    id: str = ""
    label: str = ""
    found: bool = False
    detail: str = ""
    providers: list = field(default_factory=list)


# Environment variables holding a key, and the preset they belong to.
ENV_PRESETS = (
    ("GROQ_API_KEY", "groq"),
    ("GEMINI_API_KEY", "gemini"),
    ("GOOGLE_API_KEY", "gemini"),
    ("MISTRAL_API_KEY", "mistral"),
    ("NVIDIA_API_KEY", "nvidia"),
    ("OPENROUTER_API_KEY", "openrouter"),
    ("TOGETHER_API_KEY", "together"),
    ("CEREBRAS_API_KEY", "cerebras"),
    ("DEEPINFRA_API_KEY", "deepinfra"),
    ("GITHUB_TOKEN", "github"),
)

# A literal key whose name mentions one of these goes to that preset.
KEY_HINTS = ("groq", "mistral", "gemini", "deepinfra", "together", "cerebras",
             "openrouter", "nvidia", "github", "openai", "anthropic", "cohere")


def _read_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _strip_jsonc(text: str) -> str:
    """JSON with // line comments, /* */ spans and trailing commas removed,
    strings intact."""
    out, i, n, quote = [], 0, len(text), ""
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("\"", "'"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(ch)
        i += 1
    cleaned = "".join(out)
    return re.sub(r",\s*([}\]])", r"\1", cleaned)


def _read_jsonc(path: Path):
    try:
        data = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _resolve_key(value) -> tuple:
    """(key or None, where-from): `{env:VAR}` is read from the environment."""
    text = str(value or "").strip()
    if not text:
        return None, ""
    match = re.fullmatch(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", text)
    if match:
        found = (os.environ.get(match.group(1)) or "").strip()
        return (found or None), ("env:" + match.group(1) if found else "")
    return text, "file"


def _slug(*parts: str) -> str:
    """A provider-name-safe spelling of whatever the other agent called it."""
    text = re.sub(r"[^a-z0-9._-]+", "-", "-".join(parts).lower()).strip(".-")
    return text[:48] or "imported"


def _auth_keys(home: Path, appdata: Path | None = None) -> dict:
    """OpenCode keeps `opencode auth login` seats out of the config files.

    `~/.local/share/opencode/auth.json` (or the platform equivalent) maps a
    provider id to `{"type": ..., "key": ...}` — without it every passwordless
    endpoint shows up keyless, which is exactly the complaint that sent us here.
    """
    candidates = [home / ".local" / "share" / "opencode" / "auth.json",
                  home / "Library" / "Application Support" / "opencode" / "auth.json"]
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    if xdg:
        candidates.append(Path(xdg) / "opencode" / "auth.json")
    if appdata is not None:
        candidates.append(appdata / "opencode" / "auth.json")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        data = _read_json(candidate)
        if isinstance(data, dict):
            return data
    return {}


def _detect_opencode(home: Path, appdata: Path | None = None) -> AgentFinding:
    finding = AgentFinding(id="opencode", label="OpenCode")
    base = home / ".config" / "opencode"
    providers: dict = {}
    disabled: set[str] = set()
    parsed_any = False
    for name in ("opencode.json", "opencode.jsonc"):
        candidate = base / name
        if not candidate.is_file():
            continue
        data = _read_json(candidate) if name.endswith(".json") \
            else _read_jsonc(candidate)
        if data is None:
            continue
        parsed_any = True
        file_providers = data.get("provider")
        if isinstance(file_providers, dict):
            providers.update(file_providers)
        for pid in data.get("disabled_providers", []) or []:
            disabled.add(str(pid).lower())
    if not parsed_any:
        return finding
    if not providers:
        finding.found, finding.detail = True, "no providers configured"
        return finding
    finding.found = True
    auth = _auth_keys(home, appdata)
    auth_by_slug = {_slug(str(k)): v for k, v in auth.items()}
    for pid, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        options = entry.get("options") if isinstance(
            entry.get("options"), dict) else {}
        url = str(options.get("baseURL") or options.get("baseUrl") or "").strip()
        key, where = _resolve_key(options.get("apiKey") or options.get("api_key"))
        models = entry.get("models")
        models = [str(m) for m in models] if isinstance(models, dict) else []
        note = ""
        if not key:
            seat = auth_by_slug.get(_slug(str(pid)))
            if isinstance(seat, dict) and str(seat.get("key") or "").strip():
                key, where = str(seat["key"]).strip(), "opencode-auth"
            elif isinstance(seat, str) and seat.strip():
                key, where = seat.strip(), "opencode-auth"
        if str(pid).lower() in disabled:
            note = "disabled in opencode.jsonc"
        elif not key and str(options.get("apiKey") or "").strip():
            note = "key names an env var that is not set here"
        finding.providers.append(FoundProvider(
            agent="opencode", name=_slug(str(pid)), url=url, models=models,
            key=key, key_from=where, note=note))
    # Same endpoint, one account: a keyless entry pointing at the exact URL of
    # a keyed one reuses that seat. No key material is guessed — the user's own
    # stored key, on the identical host, with the provenance in the note.
    keyed = {}
    for p in finding.providers:
        norm = (p.url or "").strip().rstrip("/").lower()
        if p.key and norm and norm not in keyed:
            keyed[norm] = p
    for p in finding.providers:
        if p.key or not (p.url or "").strip():
            continue
        donor = keyed.get(p.url.strip().rstrip("/").lower())
        if donor is not None:
            p.key, p.key_from = donor.key, donor.key_from
            p.note = (f"same endpoint as {donor.name} — key copied from its seat"
                      + (f"; {p.note}" if p.note else ""))
    if not finding.providers:
        finding.detail = "config found, no providers inside"
    return finding


def _parse_toml_sections(text: str) -> dict:
    """[a.b.c] sections with key = value pairs. Enough for provider tables;
    quotes, inline comments and duplicate sections are handled, nesting beyond
    tables is not needed here."""
    sections, current, seen = {}, [], None
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip() if "#" in raw and \
            not raw.lstrip().startswith(("\"", "'")) else raw.strip()
        if not line:
            continue
        header = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if header:
            current = tuple(header.group(1).split("."))
            sections.setdefault(current, {})
            continue
        pair = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(.+)", line)
        if pair and current is not None:
            value = pair.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
                value = value[1:-1]
            sections[current][pair.group(1)] = value
    return sections


def _detect_codex(home: Path) -> AgentFinding:
    finding = AgentFinding(id="codex", label="Codex CLI")
    try:
        text = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return finding
    finding.found = True
    for section, values in _parse_toml_sections(text).items():
        if len(section) != 2 or section[0] != "model_providers":
            continue
        url = str(values.get("base_url") or values.get("base-url") or "").strip()
        name = _slug(section[1])
        model = str(values.get("model") or "").strip()
        models = [model] if model else []
        named = [(f, str(values.get(f) or "").strip())
                 for f in ("env_key", "env-key", "api_key", "api-key", "key")]
        named = [(f, v) for f, v in named if v]
        if not named:
            finding.providers.append(FoundProvider(
                agent="codex", name=name, url=url, models=models,
                note="no key named in the profile"))
            continue
        field, value = named[0]
        if field.startswith("env"):
            env_value = (os.environ.get(value) or "").strip()
            if not env_value:
                finding.providers.append(FoundProvider(
                    agent="codex", name=name, url=url, models=models,
                    note="env %s is not set here" % value))
                continue
            finding.providers.append(FoundProvider(
                agent="codex", name=name, url=url, models=models,
                key=env_value, key_from="env:" + value))
            continue
        finding.providers.append(FoundProvider(
            agent="codex", name=name, url=url, models=models,
            key=value, key_from="file"))
    auth = _read_json(home / ".codex" / "auth.json")
    if isinstance(auth, dict) and str(auth.get("OPENAI_API_KEY") or "").strip():
        finding.providers.append(FoundProvider(
            agent="codex", name="codex-openai", url="https://api.openai.com/v1",
            models=[], key=str(auth["OPENAI_API_KEY"]).strip(), key_from="file",
            note="name its models with /providers models codex-openai <m>"))
    if not finding.providers:
        finding.detail = "config found, no provider profiles inside"
    return finding


def _detect_gemini(home: Path) -> AgentFinding:
    finding = AgentFinding(id="gemini", label="Gemini CLI")
    if not (home / ".gemini" / "settings.json").is_file():
        return finding
    finding.found = True
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            finding.providers.append(FoundProvider(
                agent="gemini", name="gemini", url="", models=[],
                key=value, key_from="env:" + env_name))
            break
    if not finding.providers:
        finding.detail = "installed; keys live in env or Vertex, nothing to take"
    return finding


def _detect_claude(home: Path) -> AgentFinding:
    finding = AgentFinding(id="claude", label="Claude Code")
    if not (home / ".claude.json").is_file() and \
            not (home / ".claude").is_dir():
        return finding
    finding.found = True
    finding.detail = "OAuth login — no exportable key, nothing to take"
    return finding


def _aider_keys(text: str) -> list:
    """`some-api-key: value` lines. Indentation-tolerant, quotes stripped."""
    found = []
    for raw in (text or "").splitlines():
        match = re.match(r"\s*([A-Za-z0-9_.-]*api[-_]key)\s*:\s*(.+?)\s*$",
                         raw, re.IGNORECASE)
        if match:
            value = match.group(2).strip().strip("\"'")
            if value and not value.startswith("$"):
                found.append((match.group(1), value))
    return found


def _preset_for_key(field: str) -> str:
    low = field.lower()
    for hint in KEY_HINTS:
        if hint in low:
            return hint if hint not in ("openai", "anthropic", "cohere") else ""
    return ""


def _detect_aider(home: Path) -> AgentFinding:
    finding = AgentFinding(id="aider", label="Aider")
    try:
        text = (home / ".aider.conf.yml").read_text(encoding="utf-8")
    except OSError:
        return finding
    finding.found = True
    for field_name, value in _aider_keys(text):
        preset = _preset_for_key(field_name)
        if preset:
            finding.providers.append(FoundProvider(
                agent="aider", name=preset, url="", models=[],
                key=value, key_from="file"))
        else:
            finding.providers.append(FoundProvider(
                agent="aider", name=_slug(field_name), url="", models=[],
                note="no matching BeeCode endpoint — save it by hand"))
    if not finding.providers:
        finding.detail = "config found, no literal keys inside"
    return finding


def _vscdb_blob_for(state_db: Path, needle: str) -> str:
    """The `ItemTable.value` column of a VS Code `state.vscdb`, for the row
    whose key contains `needle`. Read-only, one query, closed immediately.

    `state.vscdb` is a plain (unencrypted) SQLite file — the *secrets* an
    extension stores go through VS Code's OS-level secret storage instead,
    which this module does not touch. What lands here is configuration:
    endpoints, model names, and sometimes a key typed straight into a
    settings field rather than through the secret-storage API.
    """
    import sqlite3

    if not state_db.is_file():
        return ""
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key LIKE ? LIMIT 1",
                ("%" + needle + "%",)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ""
    return row[0] if row and isinstance(row[0], str) else ""


def _detect_vscode(home: Path, appdata: Path | None = None) -> AgentFinding:
    """Cline, Roo Code and Kilo Code: all three fork the same VS Code
    extension lineage and store their state the same way — a `state.vscdb`
    per profile under the extension's `globalStorage` folder.

    Their exact JSON shape is not published as a stable schema (it has
    changed across forks and versions), so this reads only what is safe to
    read generically: URLs and `"...ApiKey": "..."` pairs found literally in
    the blob, by pattern rather than by a hard-coded field list. A key that
    is empty, or that VS Code's secret storage holds instead of the blob,
    is reported as absent — never guessed at.
    """
    finding = AgentFinding(id="vscode", label="VS Code agents")
    roots = [home / ".config" / "Code" / "User" / "globalStorage",
              home / ".vscode-server" / "data" / "User" / "globalStorage"]
    if appdata is not None:
        roots.append(appdata / "Code" / "User" / "globalStorage")
    marks = (("cline", ("saoudrizwan.claude-dev", "saoudrizwan.cline")),
             ("roo", ("rooveterinaryinc.roo-cline",)),
             ("kilo", ("kilocode.kilo-code",)))
    seen_any_ext = False
    ext_root = home / ".vscode" / "extensions"
    try:
        installed = [p.name.lower() for p in ext_root.iterdir()]
    except OSError:
        installed = []
    for label, publisher_ids in marks:
        present = any(n.startswith(publisher_ids) for n in installed)
        state_dir = next((root / pid for root in roots for pid in publisher_ids
                          if (root / pid / "state.vscdb").is_file()), None)
        if not present and state_dir is None:
            continue
        seen_any_ext = True
        finding.found = True
        blob = _vscdb_blob_for(state_dir / "state.vscdb", "config") if state_dir else ""
        urls = sorted(set(re.findall(r'"(https?://[^"\s]{6,200})"', blob)))
        key_fields = re.findall(r'"(\w*[Aa]pi[Kk]ey)"\s*:\s*"([^"]{4,200})"', blob)
        if not urls and not key_fields:
            finding.providers.append(FoundProvider(
                agent="vscode", name=label,
                note="installed; no endpoint or key found in its stored settings "
                     "— it may keep both in VS Code's secret storage"))
            continue
        for url in urls[:4]:
            match = next(((f, v) for f, v in key_fields if v), (None, None))
            finding.providers.append(FoundProvider(
                agent="vscode", name=_slug(label, url.split("//", 1)[-1].split("/")[0]),
                url=url, models=[],
                key=match[1], key_from=("file" if match[1] else ""),
                note="" if match[1] else "no literal key alongside this url — it "
                                          "may be in VS Code's secret storage"))
    if not seen_any_ext:
        return finding
    if finding.found and not finding.providers:
        finding.detail = "extension present, nothing readable in its settings"
    return finding


def _detect_qoder(home: Path) -> AgentFinding:
    finding = AgentFinding(id="qoder", label="Qoder")
    if not (home / ".qoder" / "settings.json").is_file():
        return finding
    finding.found = True
    finding.detail = "keys live in its own store — copy one from Qoder's settings"
    return finding


def _detect_zed(home: Path, appdata: Path | None = None) -> AgentFinding:
    """Zed's own OpenAI-compatible endpoints, from `language_models` in its settings.

    Zed never writes a key into `settings.json` — it reads one from an
    environment variable named after the provider, upper-cased, with
    `_API_KEY` appended (that convention is Zed's own, not a guess made here).
    A provider with no such variable set still shows up, without a key.
    """
    finding = AgentFinding(id="zed", label="Zed")
    candidates = [home / ".config" / "zed" / "settings.json",
                  home / ".zed" / "settings.json"]
    if appdata is not None:
        candidates.append(appdata / "Zed" / "settings.json")
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        candidates.append(Path(xdg) / "zed" / "settings.json")
    data = None
    for candidate in candidates:
        if candidate.is_file():
            data = _read_jsonc(candidate)
            if data is not None:
                break
    if data is None:
        return finding
    finding.found = True
    models = data.get("language_models")
    compat = models.get("openai_compatible") if isinstance(models, dict) else None
    if not isinstance(compat, dict) or not compat:
        finding.detail = "installed; no custom openai_compatible endpoint configured"
        return finding
    for pid, entry in compat.items():
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("api_url") or "").strip()
        available = entry.get("available_models")
        model_names = [str(m.get("name")) for m in available
                       if isinstance(m, dict) and m.get("name")] \
            if isinstance(available, list) else []
        env_name = re.sub(r"[^A-Z0-9_]", "_", str(pid).upper()) + "_API_KEY"
        key = (os.environ.get(env_name) or "").strip()
        finding.providers.append(FoundProvider(
            agent="zed", name=_slug(str(pid)), url=url, models=model_names,
            key=(key or None), key_from=("env:" + env_name if key else ""),
            note="" if key else "no %s in this environment — set it or paste "
                                 "the key by hand" % env_name))
    if not finding.providers:
        finding.detail = "config found, no provider profiles inside"
    return finding


def _parse_yaml_subset(text: str) -> dict:
    """A narrow YAML reader: nested string maps, and a list of maps under a key.

    Enough for DeepSeek Harness's `settings.yaml` (`llm-pi-ai.providers.<id>.*`
    plus its `models:` list) and nothing more — no anchors, no flow style, no
    multi-document files. Indentation must be spaces; a tab is treated as the
    line ending early, which surfaces as a missing field, not a crash.
    """
    lines = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0] if "#" in raw and "\"" not in raw.split("#", 1)[0] \
            else raw
        if line.strip():
            lines.append((len(line) - len(line.lstrip(" ")), line.strip()))

    root: dict = {}
    stack = [(-1, root)]

    def is_list_next(after: int) -> bool:
        for indent, stripped in lines[after + 1:]:
            return stripped.startswith("- ") or stripped == "-"
        return False

    for i, (indent, stripped) in enumerate(lines):
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            if not isinstance(parent, list):
                continue
            item: dict = {}
            parent.append(item)
            match = re.match(r"([A-Za-z0-9_.-]+)\s*:\s*(.*)$", item_text)
            if match:
                item[match.group(1)] = _yaml_scalar(match.group(2))
            stack.append((indent, item))
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)\s*:\s*(.*)$", stripped)
        if not match or not isinstance(parent, dict):
            continue
        key, value = match.group(1), match.group(2).strip()
        if value == "":
            child: dict | list = [] if is_list_next(i) else {}
            parent[key] = child
            stack.append((indent, child))
            continue
        parent[key] = _yaml_scalar(value)
    return root


def _yaml_scalar(text: str):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("\"", "'"):
        return text[1:-1]
    return text


def _detect_dsh(home: Path) -> AgentFinding:
    """DeepSeek Harness (`dsh`): endpoints under `llm-pi-ai.providers` in its
    settings file, keys resolved the way `dsh` itself resolves them — from the
    environment variable each provider names as `apiKeyEnv`. The managed
    `.credentials.yaml` store is not read here: its on-disk shape is an
    internal implementation detail `dsh` does not document, and guessing at
    it risks reading the wrong field as a key.
    """
    finding = AgentFinding(id="dsh", label="DeepSeek Harness")
    dsh_home = Path(os.environ.get("DSH_HOME") or (home / ".dsh"))
    try:
        text = (dsh_home / "settings.yaml").read_text(encoding="utf-8")
    except OSError:
        return finding
    finding.found = True
    data = _parse_yaml_subset(text)
    pi_ai = data.get("llm-pi-ai")
    providers = pi_ai.get("providers") if isinstance(pi_ai, dict) else None
    if not isinstance(providers, dict) or not providers:
        finding.detail = "config found, no providers configured"
        return finding
    for pid, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("baseURL") or "").strip()
        models_list = entry.get("models")
        model_ids = [str(m.get("id")) for m in models_list
                     if isinstance(m, dict) and m.get("id")] \
            if isinstance(models_list, list) else []
        env_name = str(entry.get("apiKeyEnv") or "").strip()
        key = (os.environ.get(env_name) or "").strip() if env_name else ""
        note = ""
        if not key:
            note = ("its key lives in %s/.credentials.yaml — set %s "
                    "or paste the key by hand" % (dsh_home, env_name)) if env_name \
                else "no apiKeyEnv named for this provider"
        finding.providers.append(FoundProvider(
            agent="dsh", name=_slug(str(pid)), url=url, models=model_ids,
            key=(key or None), key_from=("env:" + env_name if key else ""),
            note=note))
    if not finding.providers:
        finding.detail = "config found, no providers configured"
    return finding


def _detect_env() -> AgentFinding:
    finding = AgentFinding(id="env", label="Environment")
    seen = False
    for env_name, preset in ENV_PRESETS:
        value = (os.environ.get(env_name) or "").strip()
        if value:
            seen = True
            finding.providers.append(FoundProvider(
                agent="env", name=preset, url="", models=[],
                key=value, key_from="env:" + env_name))
    finding.found = seen
    if not seen:
        finding.detail = "no known provider keys exported"
    return finding


def scan(home=None, appdata=None) -> list:
    """Every known agent, in a stable order. Never raises: one bad file must
    not hide the rest of the machine."""
    home = Path(home) if home is not None else Path.home()
    appdata = Path(appdata) if appdata is not None else None
    detectors = (
        lambda: _detect_opencode(home, appdata),
        lambda: _detect_codex(home),
        lambda: _detect_gemini(home),
        lambda: _detect_claude(home),
        lambda: _detect_aider(home),
        lambda: _detect_vscode(home, appdata),
        lambda: _detect_qoder(home),
        lambda: _detect_zed(home, appdata),
        lambda: _detect_dsh(home),
        _detect_env,
    )
    findings = []
    for detect in detectors:
        try:
            findings.append(detect())
        except Exception:
            findings.append(AgentFinding(id="?", label="?", detail="unreadable"))
    return findings


def import_provider(config, agent, found: FoundProvider, workdir: str = ".") -> tuple:
    """Save one finding through the validated door. Returns (message, refusal).

    A preset name (groq, mistral, …) becomes a key on the preset; anything else
    becomes a custom endpoint. The key travels only into the config and the
    live provider — never into a message, a log line or an error.
    """
    from beeagent.core import provider_setup
    from beeagent.providers.presets import BY_NAME

    name = (found.name or "").strip().lower()
    if found.url:
        if name in BY_NAME or not provider_setup.NAME_SHAPE.match(name):
            name = _slug(found.agent, found.name or "endpoint")
    fields = provider_setup.Fields(
        name=name, url=found.url or "",
        keys=found.key or "", models=list(found.models or []))
    if not found.url and name in BY_NAME:
        # A bare key for a built-in endpoint: no address to validate.
        from beeagent.config.loader import save_config
        from beeagent.i18n import L

        config.api_keys[name] = found.key
        if agent is not None:
            agent.attach_preset(name, found.key)
            agent.ready_presets = list(dict.fromkeys(
                list(agent.ready_presets) + [name]))
        try:
            save_config(config, workdir)
        except OSError as exc:
            return "", L(f"nothing was saved: {exc}", f"ничего не сохранено: {exc}")
        tail = provider_setup.mask_key(found.key)
        return L(f"🔑 {BY_NAME[name].label}: key from {found.agent} saved ({tail})",
                 f"🔑 {BY_NAME[name].label}: ключ от {found.agent} сохранён ({tail})"), ""
    message, refusal = provider_setup.apply(
        config, agent, fields, workdir=workdir)
    if refusal or agent is None:
        return message, refusal
    agent.sync_context()
    return message, refusal
