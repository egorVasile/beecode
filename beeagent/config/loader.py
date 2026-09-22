import json
import os
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
    """
    try:
        broken = config_path.with_name(config_path.name + ".broken")
        if broken.exists():
            broken.unlink()
        config_path.replace(broken)
    except OSError:
        pass


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
        return BeeConfig(**data)
    except ValidationError as e:
        _warn(L(f"⚠ {config_path} has invalid values — starting with defaults\n{e}",
                f"⚠ в {config_path} неверные значения — беру настройки по умолчанию\n{e}"))
        _set_aside(config_path)
        return BeeConfig()


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
