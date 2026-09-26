"""One door for an endpoint: the `/key` line, the guided REPL form and the modal
all write through this file, so the three doors cannot promise different things.

Ported from the `keep-9.0.0` attempt (`beeagent/core/provider_setup.py`) and
adapted to what main has: the branch discovered both the OpenAI shape and an
Anthropic one and registered both provider classes; main's providers package
ships only the OpenAI shape (plus ollama), so the Anthropic half is cut and the
discovery asks `OpenAICompatProvider.list_models` per key. The branch imported
`keys_of` from `providers/presets.py`, which main does not export — it lives
here instead, because `beeagent/providers/*` is not this command's to change.

A provider in this project is four facts: a name, where it lives, who may ask it
(the keys, any number, tried in turn), and what it answers for (the models).
Those are exactly the four fields `ui/provider_form.py` shows.

Keys are secrets: nothing here ever returns a whole key in a message — `mask_key`
and `key_tails` are the only way a key leaves this file towards a screen.
"""
import json
import re
import time
from dataclasses import dataclass, field

from beeagent.i18n import L
from beeagent.providers.presets import BY_NAME

#: `http://` or `https://` and nothing else: a base URL that reaches the machine
#: as a file path or a shell word is not an endpoint.
URL_SHAPE = re.compile(r"^https?://[^\s/][^\s]*$", re.IGNORECASE)
NAME_SHAPE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")

#: Words `/providers` itself answers to. An endpoint named `add` could never be
#: reached through its own door, so the name is refused where it is typed.
RESERVED_NAMES = ("add", "use", "edit", "set", "change", "key", "models",
                  "remove", "rm", "delete")

#: Names the program owns and a form must not overwrite: the keyless route and
#: the operator's pool are built by code, not by an endpoint entry.
BUILTIN_NAMES = ("g4f", "pool")


def keys_of(raw) -> list[str]:
    """A stored pool, however it was written, as the ordered list it means.

    Main's `providers/presets.py` does not export this helper (the old form
    imported it from there), so the split lives here: a pool is one comma
    separated string in `beeagent.json` and a list everywhere else.
    """
    if raw is None:
        return []
    parts = [str(item) for item in raw] if isinstance(raw, (list, tuple)) \
        else str(raw).split(",")
    out: list[str] = []
    for part in parts:
        key = part.strip()
        if key and key not in out:
            out.append(key)
    return out


#: What the UI prints, and accepts back, for "this endpoint needs no key".
NO_KEY = ("-", "none", "nokey")


def keys_text(raw) -> str:
    """The key pool as saved, with "no keys" spelled several ways coming out empty.

    A line that lists an endpoint without a key writes `-` for it, and a person
    pasting that line back must not end up holding a key called `-`.
    """
    parts = [part for part in keys_of(raw) if part.strip().lower() not in NO_KEY]
    return ",".join(parts)


def mask_key(key: str) -> str:
    """A key for the screen: everything but the last four characters, hidden.

    A key of four characters or fewer is hidden whole — its tail is the key.
    """
    key = (key or "").strip()
    if not key:
        return "—"
    if len(key) <= 4:
        return "…" + "•" * max(3, len(key))
    return "…" + key[-4:]


def key_tails(keys) -> str:
    """A whole pool, named only by its tails: enough to tell two keys apart."""
    pool = keys_of(keys)
    return ", ".join(mask_key(key) for key in pool) if pool else L("none", "нет")


@dataclass
class Fields:
    """The four facts, as typed, plus how the models were found out."""
    name: str = ""
    url: str = ""
    keys: str = ""                              # comma separated, in the typed order
    models: list[str] = field(default_factory=list)
    dialect: str = ""                           # main speaks one shape: "openai_compat"
    was: str = ""                               # the name being edited, if any

    @property
    def key_list(self) -> list[str]:
        return keys_of(keys_text(self.keys))

    @property
    def model(self) -> str:
        return self.models[0] if self.models else ""


def looks_like_url(value: str) -> bool:
    return bool(URL_SHAPE.match((value or "").strip()))


def _echo(value: str) -> str:
    """Show a rejected value back to whoever typed it — unless it could be a secret.

    An address has slashes and dots; a pasted key has neither. A rejected word
    that is long and pathless is shown by its tail, so the refusal sentence
    cannot put a secret on the screen by complaining about it.
    """
    value = (value or "").strip()
    if value and "/" not in value and len(value) > 8:
        return mask_key(value)
    return repr(value)


def url_refusal(url: str) -> str:
    """Why an address cannot be saved, in the words the person can act on."""
    value = (url or "").strip()
    if not value:
        return L("an endpoint needs an address: /providers add asks for it, "
                 "or /key <name> <base-url> <keys> [models ...]",
                 "нужен адрес эндпоинта: /providers add спросит его, "
                 "или /key <имя> <base-url> <ключи> [модели ...]")
    if not looks_like_url(value):
        return L(f"{_echo(value)} is not an http(s) address — the base URL is the "
                 f"part before /chat/completions, for example "
                 f"https://api.example.com/v1",
                 f"{_echo(value)} — не http(s)-адрес; base URL — это часть до "
                 f"/chat/completions, например https://api.example.com/v1")
    return ""


def name_refusal(name: str) -> str:
    value = (name or "").strip()
    if not value:
        return L("name the provider: /providers add asks for it, "
                 "or /key <name> <base-url> <keys> [models ...]",
                 "назовите провайдер: /providers add спросит его, "
                 "или /key <имя> <base-url> <ключи> [модели ...]")
    if not NAME_SHAPE.match(value.lower()):
        return L(f"{value!r} cannot be a provider name — one word, letters, digits, "
                 f"“.”, “-” and “_”",
                 f"{value!r} не может быть именем провайдера: одно слово из букв, "
                 f"цифр, «.», «-» и «_»")
    if value.lower() in RESERVED_NAMES:
        return L(f"“{value}” is a word /providers answers to itself — a provider "
                 f"under that name could never be reached; choose another",
                 f"«{value}» — служебное слово /providers; провайдер с таким именем "
                 f"бы было не достать; выберите другое")
    return ""


def split_models(values) -> list[str]:
    """The model names as given: several per argument or one per line, deduplicated.

    Spaces inside a name are kept, because the g4f catalogue has ids with spaces
    in them and cutting at the first one would make such a model untypeable.
    """
    raw = values if isinstance(values, (list, tuple)) else str(values).splitlines()
    out: list[str] = []
    for item in raw:
        for part in str(item).split(","):
            name = " ".join(part.split())
            if name and name not in out:
                out.append(name)
    return out


def dialect_of(url: str, discovered: str = "") -> str:
    """Which chat protocol an endpoint is saved as.

    Main's providers package speaks the OpenAI shape (ollama has its own door and
    its own command); the Anthropic class the old attempt registered does not
    exist here, so every endpoint this writes is saved as `openai_compat`.
    """
    return "openai_compat"


def models_cache_path(name: str):
    """The cache file `commands._cached_models` reads — same path, same shape.

    `CustomProvider` on main persists one `model`, not a list, and that schema
    file is not this command's to change; the full list lives beside it in the
    cache the completion already consults.
    """
    from pathlib import Path

    return Path(".beeagent") / f"models_{str(name).strip().lower()}.json"


def write_models_cache(name: str, models: list[str]) -> None:
    from beeagent import __version__
    from pathlib import Path

    if not models:
        return
    try:
        path = models_cache_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"saved_at": time.time(), "version": __version__,
                                    "models": list(models)}), encoding="utf-8")
    except OSError:
        pass                                        # the config stays the truth


def cached_models(name: str) -> list[str]:
    """The models written for this endpoint before, or []."""
    try:
        data = json.loads(models_cache_path(name).read_text(encoding="utf-8"))
        return [str(m) for m in data.get("models", []) if str(m).strip()]
    except (OSError, ValueError):
        return []


def stored_keys(config, name: str) -> list[str]:
    """The pool this provider holds right now, from the config the user owns."""
    name = (name or "").strip().lower()
    stored = keys_of((getattr(config, "api_keys", None) or {}).get(name))
    if stored:
        return stored
    for custom in getattr(config, "custom_providers", None) or []:
        if str(custom.name).lower() == name:
            return keys_of(custom.key)
    return []


def exists(config, name: str) -> bool:
    """Whether this name already means an endpoint — the form says "edit" then."""
    name = (name or "").strip().lower()
    if not name:
        return False
    if name in BY_NAME or name in BUILTIN_NAMES:
        return True
    if any(str(custom.name).lower() == name
           for custom in (getattr(config, "custom_providers", None) or [])):
        return True
    return bool(stored_keys(config, name))


def current(config, name: str) -> Fields:
    """What is configured for this provider right now, so a form can open filled in."""
    name = (name or "").strip().lower()
    endpoint = BY_NAME.get(name)
    fields = Fields(name=name,
                    url=getattr(endpoint, "url", "") if endpoint else "",
                    keys=",".join(stored_keys(config, name)),
                    models=list(getattr(endpoint, "models", ()) or ()) if endpoint else [])
    for custom in getattr(config, "custom_providers", None) or []:
        if str(custom.name).lower() != name:
            continue
        fields.url = custom.url or fields.url
        if not fields.keys:
            fields.keys = ",".join(keys_of(custom.key))
        if custom.model:
            fields.models = [custom.model]
        break
    cached = cached_models(name)
    if cached:
        fields.models = cached
    if fields.url and endpoint is None:
        fields.dialect = "openai_compat"
    return fields


def discover(url: str, keys, timeout: float = 12.0) -> tuple:
    """Ask the endpoint which models it has. Returns `(models, dialect, refusal)`.

    The three empty-handed outcomes are kept apart because they need different
    sentences: an endpoint that answered with an empty list, an endpoint that
    refused the key, and an endpoint that never answered at all are three
    different things to tell a person. Several keys are asked in turn, because
    one of them may be the one that is accepted.
    """
    refusal = url_refusal(url)
    if refusal:
        return [], "", refusal
    from beeagent.plugins.mcp import run_coro_blocking

    pool = keys_of(keys_text(keys)) or [""]
    try:
        # The command's own thread, so a second here is not a second the terminal
        # cannot draw, answer or cancel: the wait has a ceiling, and a timeout is
        # an answer of its own rather than a hung command.
        models, failure = run_coro_blocking(
            lambda: _ask_pairs(url, pool), timeout=timeout)
    except Exception as exc:                             # noqa: BLE001 - reported, not raised
        return [], "", L(f"the model list could not be asked of {url}: {exc}",
                         f"список моделей не получен от {url}: {exc}")
    if models:
        return models, "openai", ""
    return [], "", str(failure) if failure is not None else ""


async def _ask_pairs(url: str, pool: list[str]):
    """The first key an endpoint answers for, or the last refusal it gave."""
    from beeagent.providers.openai_compat import OpenAICompatProvider

    last = None
    for key in pool:
        probe = OpenAICompatProvider(base_url=url, api_key=key,
                                     name="discovery", model="")
        try:
            found = await probe.list_models()
        except Exception as exc:                         # noqa: BLE001 - reported
            last = exc
            continue
        if found:
            return list(found), None
        last = last or L("the endpoint answered but listed no models",
                         "эндпоинт ответил, но не назвал ни одной модели")
    return [], last


def apply(config, agent, fields: Fields, workdir: str = ".") -> tuple:
    """Save one provider. Returns `(message, refusal)`; a refusal changes nothing.

    The live agent is re-registered in the same call, so what was just typed
    works in this session and does not wait for a restart — and the message says
    whether it is the provider being used right now, because `/providers` marks
    the active one with an arrow and would look wrong if the answer did not say
    where the person still is.
    """
    from beeagent.config.loader import save_config

    refusal = name_refusal(fields.name)
    if refusal:
        return "", refusal
    name = fields.name.strip().lower()
    if name in BUILTIN_NAMES:
        return "", L(f"{name} is built into BeeCode, not stored as an endpoint — "
                     f"switch to it with /providers {name}",
                     f"{name} встроен в BeeCode, это не эндпоинт в конфиге — "
                     f"переключение: /providers {name}")
    keys = keys_text(fields.keys)
    models = split_models(fields.models)

    if name in BY_NAME:
        return _apply_preset(config, agent, name, fields, keys, models,
                             save_config, workdir)

    url = (fields.url or "").strip()
    refusal = url_refusal(url)
    if refusal:
        return "", refusal
    already = next((custom for custom in (getattr(config, "custom_providers", None) or [])
                    if str(custom.name).lower() == name), None)

    if not models and not keys and already is None and not cached_models(name):
        # Saving an endpoint with neither would put a name in /providers that
        # cannot answer anything: `model` is required by the schema, and filling
        # it with "gpt-4" would make the provider look real to whoever reads the
        # file next month.
        return "", L(f"{name} has neither keys nor models — nothing to save; "
                     f"paste a key, or name the models",
                     f"у {name} нет ни ключей, ни моделей — сохранять нечего; "
                     f"вставьте ключ или назовите модели")

    if keys:
        config.api_keys[name] = keys
    else:
        # An empty pool means "none": what was stored under this name goes.
        # The interview keeps stored keys in the box unless told otherwise, so
        # arriving here empty is a decision, not an oversight.
        config.api_keys.pop(name, None)

    was = (fields.was or "").strip().lower()
    if was and was != name:
        # Renamed in the form: the old entry and the keys stored under the old
        # name move with it. Left alone, the edit would leave a second provider
        # pointing at the same address with the same keys.
        _carry_over(config, was, name)

    kept = [custom for custom in (getattr(config, "custom_providers", None) or [])
            if str(custom.name).lower() not in (name, was)]
    entry = _entry(name=name, url=url, keys=keys, models=models,
                   keep_model=already.model if already is not None else "")
    kept.append(entry)
    config.custom_providers = kept

    if models:
        write_models_cache(name, models)
    _register(config, agent, name, url, keys, models)
    if was and was != name and agent is not None:
        agent.providers.unregister(was)
        agent.ready_presets = [p for p in agent.ready_presets if p != was]
    try:
        save_config(config, workdir)
    except OSError as exc:
        return "", L(f"nothing was saved: {exc}", f"ничего не сохранено: {exc}")

    return _saved_message(name, url, fields, models, config, agent), ""


def _apply_preset(config, agent, name, fields: Fields, keys: str, models,
                  save_config, workdir) -> tuple:
    """A built-in preset keeps its own URL and class; here only keys and models move.

    A preset typed with a foreign address is refused rather than shadowed: two
    endpoints under one name is exactly the drift the shared door exists to stop.
    """
    from beeagent.providers.presets import BY_NAME as PRESETS

    endpoint = PRESETS[name]
    url = (fields.url or "").strip() or endpoint.url
    if url != endpoint.url:
        return "", L(f"{name} is a built-in endpoint at {endpoint.url} — its address "
                     f"is not editable here; to point at {url}, save it under a new "
                     f"name (/providers add)",
                     f"{name} — встроенный эндпоинт по адресу {endpoint.url}; его адрес "
                     f"здесь не меняется; для {url} сохраните под новым именем "
                     f"(/providers add)")
    if keys:
        config.api_keys[name] = keys
    else:
        config.api_keys.pop(name, None)
    if models:
        write_models_cache(name, list(models))
    if agent is not None and keys:
        # Through the agent's factory, so an endpoint with its own provider class
        # (crax splits a pool; the generic ones carry one key) keeps that class.
        agent.attach_preset(name, keys)
        agent.ready_presets = list(dict.fromkeys(list(agent.ready_presets) + [name]))
    try:
        save_config(config, workdir)
    except OSError as exc:
        return "", L(f"nothing was saved: {exc}", f"ничего не сохранено: {exc}")
    fields = Fields(name=name, url=endpoint.url, keys=keys, models=list(models))
    return _saved_message(name, endpoint.url, fields, list(models), config, agent), ""


def _saved_message(name, url, fields: Fields, models, config, agent) -> str:
    active = (getattr(config, "provider", "") or "").lower() == name
    switched = ""
    if active and models and agent is not None:
        switched = L(f" · active, model → {models[0]}", f" · активен, модель → {models[0]}")
    elif active:
        switched = L(" · active", " · активен")
    hint = "" if active else L(f" · switch: /providers {name}",
                               f" · включить: /providers {name}")
    return (f"{name}: {url} · {L('keys', 'ключей')}: "
            f"{key_tails(fields.keys)} · {L('models', 'моделей')}: {len(models) or '—'}"
            + switched + hint)


def _carry_over(config, was: str, name: str) -> None:
    """Move stored keys from an old provider name to the new one."""
    stored = (getattr(config, "api_keys", None) or {}).pop(was, None)
    if stored and not (getattr(config, "api_keys", None) or {}).get(name):
        config.api_keys[name] = stored


def _entry(name: str, url: str, keys: str, models: list[str], keep_model: str = ""):
    from beeagent.config.schema import CustomProvider

    # main's schema persists one `model` per custom entry — no list — so the
    # first name is the entry and the whole list goes to the models cache.
    return CustomProvider(name=name, type="openai_compat", url=url,
                          key=keys or None,
                          model=(models[0] if models else (keep_model or "gpt-4")))


def _register(config, agent, name: str, url: str, keys: str,
              models: list[str]) -> None:
    """Put the saved endpoint in front of the running agent, not only on disk.

    A pool on a plain OpenAI endpoint is carried by its first key here: key
    rotation belongs to `providers/` (only crax splits a pool on main), which is
    not this command's to change — the whole pool is still what gets saved.
    """
    if agent is None:
        return
    from beeagent.providers.openai_compat import OpenAICompatProvider

    idle = max(10, int(getattr(config, "stream_idle_timeout", 90) or 90))
    pool = keys_of(keys)
    first = pool[0] if pool else ""
    provider = OpenAICompatProvider(base_url=url, api_key=first, name=name,
                                    model=models[0] if models else "gpt-4",
                                    models=models or (["gpt-4"] if not models else models),
                                    idle_timeout=idle)
    agent.providers.register(provider, replace=True)
    if models and (getattr(config, "provider", "") or "").lower() == name:
        config.model = models[0]
        agent.context.model = models[0]


def forget(config, agent, name: str, workdir: str = ".") -> tuple:
    """Drop one custom endpoint: its entry, its keys, its live registration.

    Returns `(what_went_away, refusal)`; the caller decides which built-in names
    are refused before ever reaching here.
    """
    from beeagent.config.loader import save_config

    name = (name or "").strip().lower()
    had_key = (getattr(config, "api_keys", None) or {}).pop(name, None) is not None
    before = len(getattr(config, "custom_providers", None) or [])
    config.custom_providers = [custom for custom in (config.custom_providers or [])
                               if str(custom.name).lower() != name]
    dropped = before - len(config.custom_providers)
    if not had_key and not dropped:
        return [], ""
    if agent is not None:
        agent.providers.unregister(name)
        agent.ready_presets = [p for p in agent.ready_presets if p != name]
    try:
        path = models_cache_path(name)
        if path.exists():
            path.unlink()
    except OSError:
        pass
    try:
        save_config(config, workdir)
    except OSError as exc:
        return [], L(f"nothing was saved: {exc}", f"ничего не сохранено: {exc}")
    parts = []
    if had_key:
        parts.append(L("key removed", "ключ удалён"))
    if dropped:
        parts.append(L("endpoint removed", "эндпоинт удалён"))
    return parts, ""
