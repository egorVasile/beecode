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
                "the permission gate stays at its defaults",
                "⚠ BeeCode не может определить, доверяешь ли ты этой папке — "
                "уровень допуска остаётся по умолчанию"))
        defaults = BeeConfig()
        defaults.economy = config.economy
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
    if dropped:
        _note_gate(gate, dropped)
    return config


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
    if config.api_keys or any(p.key for p in config.custom_providers):
        _restrict(config_path)
