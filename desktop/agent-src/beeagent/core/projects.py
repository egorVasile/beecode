"""Project and chat groups kept separately from the conversation files."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.RLock()


def _name(value):
    if not isinstance(value, str):
        raise ValueError("Укажите название")
    value = " ".join(value.split())
    if not value or len(value) > 80 or any(ord(c) < 32 for c in value):
        raise ValueError("Название должно содержать от 1 до 80 символов")
    return value


class Projects:
    def __init__(self, workdir):
        base = Path(workdir).resolve()
        self.root = base / ".beeagent"
        if not self.root.resolve().is_relative_to(base):
            raise ValueError("Папка проектов находится вне рабочего каталога")
        self.path = self.root / "projects.json"

    @contextlib.contextmanager
    def locked(self):
        with _LOCK:
            self.root.mkdir(parents=True, exist_ok=True)
            lock_path = self.root / "projects.lock"
            if lock_path.is_symlink() or self.path.is_symlink():
                raise ValueError("Файлы проектов не могут быть ссылками")
            with open(lock_path, "a+b") as lock:
                if os.name == "nt":
                    import msvcrt
                    lock.seek(0, os.SEEK_END)
                    if lock.tell() == 0: lock.write(b"\0"); lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _load(self):
        if not self.path.exists():
            return {"version": 1, "projects": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("projects"), list):
            raise ValueError("Не удалось прочитать проекты")
        return data

    def _save(self, data):
        if self.path.is_symlink(): raise ValueError("Файл проектов не может быть ссылкой")
        handle, tmp = tempfile.mkstemp(prefix=".projects-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

    def list(self):
        with self.locked():
            return self._load()["projects"]

    @staticmethod
    def _project(data, project_id):
        for project in data["projects"]:
            if project.get("id") == project_id: return project
        raise ValueError("Проект не найден")

    @staticmethod
    def _folder(project, folder_id):
        if not folder_id: return ""
        if any(folder.get("id") == folder_id for folder in project.get("folders", [])):
            return folder_id
        raise ValueError("Папка не найдена")

    def create(self, name):
        name = _name(name)
        with self.locked():
            data = self._load()
            if len(data["projects"]) >= 200: raise ValueError("Достигнут предел проектов")
            project = {"id": uuid.uuid4().hex, "name": name, "folders": [], "chats": [], "agents": []}
            data["projects"].append(project); self._save(data)
            return project

    def add_folder(self, project_id, name, parent=""):
        name = _name(name)
        with self.locked():
            data = self._load(); project = self._project(data, project_id)
            self._folder(project, parent)
            if len(project["folders"]) >= 500: raise ValueError("Достигнут предел папок")
            if any(f["name"].casefold() == name.casefold() and f.get("parent", "") == parent for f in project["folders"]):
                raise ValueError("Папка с таким названием уже существует")
            folder = {"id": uuid.uuid4().hex, "name": name, "parent": parent}
            project["folders"].append(folder); self._save(data)
            return folder

    def location(self, sid):
        with self.locked():
            for project in self._load()["projects"]:
                for chat in project.get("chats", []):
                    if chat.get("id") == sid: return {"project": project["id"], "folder": chat.get("folder", "")}
        return None

    def validate_location(self, project_id, folder_id=""):
        with self.locked():
            project = self._project(self._load(), project_id)
            self._folder(project, folder_id)

    def add_chat(self, project_id, folder_id, sid):
        from beeagent.core.session import _safe_sid
        _safe_sid(sid)
        with self.locked():
            data = self._load(); project = self._project(data, project_id)
            self._folder(project, folder_id)
            if any(chat.get("id") == sid for p in data["projects"] for chat in p.get("chats", [])):
                raise ValueError("Чат уже добавлен в проект")
            project["chats"].append({"id": sid, "folder": folder_id}); self._save(data)

    def save_agent(self, parent, snapshot, folder=None):
        with self.locked():
            data = self._load()
            for project in data["projects"]:
                chat = next((c for c in project.get("chats", []) if c.get("id") == parent), None)
                if chat is None: continue
                selected = chat.get("folder", "") if folder is None else self._folder(project, folder)
                entry = dict(snapshot, parent=parent, folder=selected, updated_at=time.time())
                entries = project.setdefault("agents", [])
                for i, old in enumerate(entries):
                    if old.get("id") == entry["id"]:
                        entries[i] = entry; break
                else: entries.append(entry)
                self._save(data)
                return True
        return False

    def agent(self, child_id):
        with self.locked():
            for project in self._load()["projects"]:
                for entry in project.get("agents", []):
                    if entry.get("id") == child_id: return entry
        return None
