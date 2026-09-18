import json
from pathlib import Path
from .schema import BeeConfig

CONFIG_FILE = "beeagent.json"

def load_config(workdir: str = ".") -> BeeConfig:
    config_path = Path(workdir) / CONFIG_FILE
    if config_path.exists():
        with open(config_path) as f:
            data = json.load(f)
        return BeeConfig(**data)
    return BeeConfig()

def save_config(config: BeeConfig, workdir: str = "."):
    config_path = Path(workdir) / CONFIG_FILE
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config.model_dump(), f, indent=2)
