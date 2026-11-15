"""Local, fixed-command bridge to the optional BeeCode desktop pet."""
import ctypes
import json
import os
import subprocess
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_ROOT = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "BeeCodePet"
_PATH = _ROOT / "beecode-state.json"
_STATES = {"idle", "working", "thinking", "done", "error", "stopped", "waiting"}

def read():
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return {"enabled": data.get("enabled") is True, "variant": data.get("variant") if data.get("variant") in ("black", "yellow") else "black", "state": data.get("state", "idle"), "scene": data.get("scene", "desktop"), "scene_time": data.get("scene_time", 0)}
    except (OSError, ValueError, AttributeError):
        return {"enabled": False, "variant": "black", "state": "idle"}

def update(body):
    with _LOCK:
        data = read()
        if "enabled" in body:
            if not isinstance(body["enabled"], bool): raise ValueError("invalid enabled")
            data["enabled"] = body["enabled"]
        if "variant" in body:
            if body["variant"] not in ("black", "yellow"): raise ValueError("invalid variant")
            data["variant"] = body["variant"]
        if "state" in body:
            if body["state"] not in _STATES: raise ValueError("invalid state")
            data["state"] = body["state"]
        if "scene" in body:
            if body["scene"] not in ("settings", "desktop"): raise ValueError("invalid scene")
            data["scene"] = body["scene"]
            data["scene_time"] = time.time()
        if "landing" in body:
            landing = body["landing"]
            if not isinstance(landing, dict) or not all(isinstance(landing.get(k), (int, float)) and abs(landing[k]) < 100000 for k in ("x", "y")):
                raise ValueError("invalid landing")
            data["landing"] = {k: int(landing[k]) for k in ("x", "y")}
        if body.get("enabled") is True:
            bundled = Path(__file__).resolve().parents[2] / "BeeCodePet.exe"
            legacy = Path.home() / "Desktop/BeeCodePet.exe"
            exe = bundled if bundled.is_file() else legacy
            if not exe.is_file(): raise FileNotFoundError("Не найден BeeCode Pet. Переустановите BeeCode.")
        _ROOT.mkdir(parents=True, exist_ok=True)
        data["revision"] = time.time_ns()
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _PATH)
        if body.get("enabled") is True and not ctypes.windll.user32.FindWindowW(None, "BeeCode Pet"):
            subprocess.Popen([str(exe)], close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return data
