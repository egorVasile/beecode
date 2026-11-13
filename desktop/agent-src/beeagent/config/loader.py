import json
import os
import shutil
import tempfile
import stat
from pathlib import Path

from beeagent.i18n import L
from .model import ValidationError
from .schema import BeeConfig

CONFIG_FILE = "beeagent.json"


def _restrict(path: Path):
    """Owner-only permissions on a file that holds API keys (POSIX only)."""
    if os.name != "posix":
        return
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _warn(text: str):
    print(text)


def _set_aside(config_path) -> None:
    """Move a config BeeCode cannot read out of the way before returning defaults.

    Every failure path hands the caller a fresh default object, and the next
    /model, /key or /permissions writes that object over the file — so one typo
    in beeagent.json used to mean the stored API keys and custom providers were
    erased the first time the user changed anything. A file we could not read is
    nobody's defaults: it goes to one side, where it can still be repaired.

    Two rules keep that promise: a rescue already there is never overwritten (a
    second bad config used to delete the first one's keys), and if the move is
    refused the file is copied instead — because BeeCode is about to run on
    defaults, and the only thing standing between the user's keys and the next
    save is this copy.
    """
    broken = config_path.with_name(config_path.name + ".broken")
    target = broken
    step = 1
    while target.exists():
        target = broken.with_name(f"{broken.name}.{step}")
        step += 1
    try:
        config_path.replace(target)
    except OSError:
        try:
            shutil.copyfile(str(config_path), str(target))
        except OSError:
            _warn(L(f"⚠ {config_path} could not be set aside — copy it before "
                    "the next /key, /model or /permissions writes defaults over it",
                    f"⚠ {config_path} не удалось отложить — скопируй его, пока "
                    "/key, /model или /permissions не записали поверх него defaults"))


def load_config(workdir: str = ".") -> BeeConfig:
    """Read beeagent.json, or start on defaults and say what was wrong.

    This used to raise straight out of `cli.py` and `Agent.__init__`: one torn
    file — and `save_config` truncated in place, so a crash, a full disk or
    Ctrl+C wrote one — left the agent unable to start, with a traceback that did
    not name the file to fix.
    """
    config_path = Path(workdir) / CONFIG_FILE
    if not config_path.exists():
        return BeeConfig()

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        _warn(L(f"⚠ {config_path} is unreadable ({e.__class__.__name__}) — starting with defaults",
                f"⚠ {config_path} не читается ({e.__class__.__name__}) — беру настройки по умолчанию"))
        _set_aside(config_path)
        return BeeConfig()
    if not isinstance(data, dict):
        _warn(L(f"⚠ {config_path} should hold a JSON object — starting with defaults",
                f"⚠ в {config_path} должен быть JSON-объект — беру настройки по умолчанию"))
        _set_aside(config_path)
        return BeeConfig()

    unknown = [key for key in data if key not in BeeConfig.model_fields]
    if unknown:
        # An unknown name is ignored by the config model, and the next save drops
        # it from disk — so it is announced before that happens, not after.
        _warn(L(f"⚠ {config_path} holds keys BeeCode does not know: {', '.join(sorted(unknown))} "
                f"— they will be dropped on the next save",
                f"⚠ в {config_path} есть незнакомые ключи: {', '.join(sorted(unknown))} "
                "— при следующем сохранении они исчезнут"))

    try:
        config = BeeConfig(**data)
    except ValidationError as e:
        _warn(L(f"⚠ {config_path} has invalid values — starting with defaults\n{e}",
                f"⚠ в {config_path} неверные значения — беру настройки по умолчанию\n{e}"))
        _set_aside(config_path)
        return BeeConfig()
    return _respect_project_trust(config, workdir)


def _respect_project_trust(config: BeeConfig, workdir) -> BeeConfig:
    """Take back what an unagreed-to folder wrote into the permission gate.

    `{"permissions":{"mode":"auto"}}` in a repository — and the default-looking
    `{"mode":"ask","allowed":["bash","write"]}` that names the two most damaging
    tools — both decide this from files the folder shipped, before the first model
    call, before `/allow`, before the user sees anything. The answer belongs to the
    user and lives in their home directory, so a clone cannot grant it to itself
    (see core/trust.py). What is dropped here is only *not applied*: it is recorded
    with the folder's gate, so the question and `/trust yes` can put it back.

    A failure of the trust layer is not a licence to trust the folder: on any
    exception the gate stays at the defaults the program ships with.
    """
    from beeagent.core import trust

    try:
        gate = trust.for_folder(workdir)
        if gate.trusted:
            return config
    except Exception:
        _warn(L("⚠ BeeCode cannot tell whether this folder is one you agreed to — "
                "the permission gate stays at its defaults, and the folder's pool, "
                "seat, keys and endpoints are not applied",
                "⚠ BeeCode не может определить, доверяешь ли ты этой папке — "
                "уровень допуска остаётся по умолчанию, а пул, место, ключи и "
                "эндпоинты папки не применяются"))
        # A trust-layer failure is not a licence to trust the folder: drop the
        # folder-controlled gate fields exactly as for an untrusted folder.
        # (The old code built `defaults` and returned `config` untouched.)
        defaults = BeeConfig()
        config.permissions.mode = defaults.permissions.mode
        config.permissions.allowed = []
        config.vpn_command = ""
        config.pool_url = defaults.pool_url
        config.pool_token = ""
        config.api_keys = {}
        config.custom_providers = []
        return config

    dropped = []
    defaults = BeeConfig()
    if config.permissions.mode != defaults.permissions.mode:
        dropped.append(L(f"beeagent.json set permissions.mode to "
                         f"\"{config.permissions.mode}\" — it stayed "
                         f"\"{defaults.permissions.mode}\", the default",
                         f"beeagent.json задавал permissions.mode = "
                         f"\"{config.permissions.mode}\" — оставлен "
                         f"\"{defaults.permissions.mode}\" по умолчанию"))
        config.permissions.mode = defaults.permissions.mode
    if config.permissions.allowed:
        dropped.append(L(f"beeagent.json pre-granted {len(config.permissions.allowed)} "
                         f"tool(s) ({', '.join(config.permissions.allowed[:8])}) — none "
                         f"of them is granted",
                         f"beeagent.json заранее разрешал инструментов: "
                         f"{', '.join(config.permissions.allowed[:8])} — ничего не разрешено"))
        config.permissions.allowed = []
    if config.vpn_command:
        dropped.append(L("beeagent.json set vpn_command (it runs through a shell) — "
                         "it is not set",
                         "beeagent.json задавал vpn_command (запуск через оболочку) — "
                         "он снят"))
        config.vpn_command = ""
    # Where the prompts go is the same class of claim as who may run a tool. A
    # folder that names a pool, a seat, a key or an endpoint decides that this
    # machine's work leaves it — and it decides that before the first question,
    # silently, for a user who cloned an unrelated project.
    #
    # Only a file this install never wrote: `save_config` vouches for the bytes it
    # put here, and a key the user typed is not an attack.
    if not _written_here(workdir):
        if config.pool_url != defaults.pool_url:
            dropped.append(L(f"beeagent.json pointed the pool at \"{config.pool_url}\" — the "
                             "address that ships with BeeCode is used until you trust the folder",
                             f"beeagent.json направлял в пул \"{config.pool_url}\" — "
                             "используется адрес из поставки BeeCode, пока папке не доверяешь"))
            config.pool_url = defaults.pool_url
        if config.pool_token:
            dropped.append(L("beeagent.json carried a pool seat token — this install answers as "
                             "its own seat (/pool enroll), not as the one the folder brought",
                             "beeagent.json нёс токен места в пуле — эта установка отвечает как "
                             "своё место (/pool enroll), а не как то, что принесла папка"))
            config.pool_token = ""
        if config.api_keys:
            dropped.append(L(f"beeagent.json supplied {len(config.api_keys)} API key(s) "
                             f"({', '.join(sorted(config.api_keys)[:8])}) — none is used",
                             f"beeagent.json приносил ключей: {len(config.api_keys)} "
                             f"({', '.join(sorted(config.api_keys)[:8])}) — ни один не используется"))
            config.api_keys = {}
        if config.custom_providers:
            where = ", ".join(str(getattr(p, "url", "") or getattr(p, "name", ""))
                              for p in config.custom_providers[:4])
            dropped.append(L(f"beeagent.json defined {len(config.custom_providers)} provider "
                             f"endpoint(s) ({where}) — they are not registered",
                             f"beeagent.json определял провайдеров(а): "
                             f"{len(config.custom_providers)} ({where}) — они не зарегистрированы"))
            config.custom_providers = []
    if dropped:
        _note_gate(gate, dropped)
    return config


def _written_here(workdir) -> bool:
    """Did this install write the config in that folder, at the bytes it has now?"""
    from beeagent.core import trust

    try:
        digest = trust.digest_file(Path(workdir or ".") / CONFIG_FILE)
        return bool(digest) and trust.store().granted_bytes(trust.folder_key(workdir), digest)
    except Exception:
        return False


def _note_gate(gate, dropped) -> None:
    """Hand the dropped grants to the folder's gate, which is what asks about them."""
    try:
        for line in dropped:
            gate.withhold(line)
    except Exception:
        pass


def save_config(config: BeeConfig, workdir: str = "."):
    config_path = Path(workdir) / CONFIG_FILE
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the file and move it in: a save interrupted halfway used to
    # leave a truncated config that `load_config` then refused to start on. The
    # name is unique because two BeeCode processes in one tree used to share one
    # `beeagent.json.tmp`, where the loser of the race got a PermissionError.
    descriptor, tmp_name = tempfile.mkstemp(dir=str(config_path.parent),
                                            prefix=CONFIG_FILE + ".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(config.model_dump(), handle, indent=2, ensure_ascii=False)
        os.replace(tmp_name, config_path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
    # pool_token is a credential exactly like api_keys; the old condition left
    # a file holding only pool_token world-readable, even on POSIX.
    if config.api_keys or config.pool_token or any(p.key for p in config.custom_providers):
        _restrict(config_path)
    _vouch_for_saved_config(config_path, workdir)


def _vouch_for_saved_config(config_path, workdir) -> None:
    """Record the bytes this install just wrote, so a later start can tell them apart.

    `beeagent.json` is where a user's own `/key`, `/providers` and `/pool url` land,
    and it is also what a cloned repository arrives with. To a reader the two files
    look the same; a hash of what this program wrote is the only thing that says
    which one it is. Without it the gate that stops a clone from choosing where
    your prompts go also throws away the key you saved five minutes ago.
    """
    from beeagent.core import trust

    try:
        digest = trust.digest_file(config_path)
        if digest:
            trust.store().grant_bytes(trust.folder_key(workdir), digest,
                                      name=CONFIG_FILE, kind="config", how="saved")
    except Exception:
        pass                            # a refused note must not lose the save
