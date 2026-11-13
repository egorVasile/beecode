"""Bounded read-only child agents with separate conversations."""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid

from beeagent.tools.base import BaseTool, ToolResult
from beeagent.tools.registry import ToolRegistry


class SubagentTool(BaseTool):
    name = "subagent"
    description = ("Delegate read-only research: start with task, wait by id; up to three. "
                   "Project folder ids come from project.")
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["start", "list", "wait", "stop"]},
        "task": {"type": "string", "description": "Research task"},
        "id": {"type": "string", "description": "Child id"},
        "folder": {"type": "string", "description": "Folder id; omit for current"},
    }, "required": ["action"]}
    READ_TOOLS = frozenset({"read", "grep", "glob", "list_directory"})

    def __init__(self, parent):
        self.parent = parent
        self._jobs = {}
        self._lock = threading.Lock()

    def is_safe(self):
        return True

    def execute(self, action, task="", id="", folder=None):
        if action == "start":
            if folder is not None:
                if not isinstance(folder, str): return ToolResult("Invalid folder id.", True)
                try:
                    from beeagent.core.projects import Projects
                    store = Projects(self.parent.workdir)
                    location = store.location(getattr(self.parent, "project_session_id", ""))
                    if not location: return ToolResult("This chat is not in a project.", True)
                    store.validate_location(location["project"], folder)
                except (ValueError, OSError, AttributeError) as exc:
                    return ToolResult(str(exc), True)
            task = str(task or "").strip()
            if not task or len(task) > 12000:
                return ToolResult("Provide a task of at most 12000 characters.", True)
            with self._lock:
                if sum(job["status"] == "running" for job in self._jobs.values()) >= 3:
                    return ToolResult("Three subagents are already running; wait for one.", True)
                if len(self._jobs) >= 24:
                    return ToolResult("Subagent limit reached for this agent session.", True)
                child_id = uuid.uuid4().hex[:12]
                job = {"status": "running", "task": task, "result": "", "agent": None,
                       "done": threading.Event(), "events": [], "delivered": False,
                       "parent_session": getattr(self.parent, "project_session_id", ""), "project_folder": folder,
                       "last_project_save": 0.0}
                self._jobs[child_id] = job
            try:
                self._save_project_agent(child_id, job)
            except (OSError, ValueError) as exc:
                with self._lock: self._jobs.pop(child_id, None)
                return ToolResult(f"Cannot save subagent in project: {exc}", True)
            threading.Thread(target=self._run, args=(child_id, job), daemon=True,
                             name="beecode-subagent-" + child_id).start()
            return ToolResult(json.dumps({"id": child_id, "status": "running"}), False)
        if action == "list":
            with self._lock:
                rows = [{"id": key, "status": value["status"], "task": value["task"]}
                        for key, value in self._jobs.items()]
            return ToolResult(json.dumps(rows, ensure_ascii=False), False)
        with self._lock:
            job = self._jobs.get(str(id))
        if not job:
            return ToolResult("Unknown subagent id.", True)
        if action == "stop":
            with self._lock:
                job["cancelled"] = True
                child = job["agent"]
            if child:
                child.stop_requested = True
            return ToolResult("Stop requested; a running model request may finish first.", False)
        if action == "wait":
            job["done"].wait(20)
            with self._lock:
                result = {"id": id, "status": job["status"], "result": job["result"]}
                if job["status"] != "running":
                    job["delivered"] = True
            return ToolResult(json.dumps(result, ensure_ascii=False), job["status"] == "error")
        return ToolResult("Unknown action.", True)

    def _save_project_agent(self, child_id, job):
        parent_id = job.get("parent_session")
        if parent_id and getattr(self.parent, "workdir", None):
            from beeagent.core.projects import Projects
            with self._lock:
                snapshot = {"id": child_id, "task": job["task"], "status": job["status"],
                            "result": job["result"], "events": list(job["events"])}
            Projects(self.parent.workdir).save_agent(parent_id, snapshot, job.get("project_folder"))

    def _run(self, child_id, job):
        from beeagent.core.permissions import Permissions
        from beeagent.core.queue import PendingQueue
        from beeagent.core.session import Session
        try:
            child = copy.copy(self.parent)
            child.config = copy.copy(self.parent.config)
            child.config.max_turns = min(12, max(1, int(child.config.max_turns or 12)))
            child.config.mode = "normal"
            child.context = copy.copy(self.parent.context)
            child.context.config = child.config
            child.plugins = type("ChildExtensions", (), {"skills_prompt_section": lambda self: ""})()
            child.tools = ToolRegistry()
            for tool in self.parent.tools.list_tools():
                if tool.name in self.READ_TOOLS and not tool.from_extension:
                    child.tools.register(copy.copy(tool))
            if job.get("parent_session"):
                from beeagent.tools.project import ProjectTool
                child.project_folder_id = job.get("project_folder")
                child.tools.register(ProjectTool(child))
            child.permissions = Permissions(mode="readonly", allowed=[])
            child.pending = PendingQueue()
            child.economy = copy.copy(self.parent.economy)
            child.is_busy = False
            child.running_tool = None
            child.stop_requested = False
            child.route_question = None
            with self._lock:
                job["agent"] = child
                cancelled = job.get("cancelled", False)
            if cancelled:
                result, status = "Stopped before starting.", "stopped"
            else:
                errors = []
                def on_event(event, data):
                    data = data or {}
                    if event == "error":
                        errors.append(str(data.get("message") or "Provider error"))
                    if event not in ("stream_delta", "response", "tool_start", "tool_end", "error"):
                        return
                    row = {"type": event}
                    if event in ("stream_delta", "response"):
                        row["text"] = str(data.get("text") or "")[:4000]
                    elif event in ("tool_start", "tool_end"):
                        row.update(tool=str(data.get("tool") or "")[:100],
                                   args=data.get("args") or {},
                                   output=str(data.get("output") or "")[:4000],
                                   error=bool(data.get("error")))
                    else:
                        row["text"] = str(data.get("message") or "Provider error")[:1000]
                    with self._lock:
                        if len(job["events"]) < 1000:
                            job["events"].append(row)
                        now = time.monotonic()
                        save = now - job["last_project_save"] >= 1.0
                        if save: job["last_project_save"] = now
                    if save:
                        try: self._save_project_agent(child_id, job)
                        except (OSError, ValueError): pass
                result = child.run_sync(job["task"], Session(), on_event)
                status = "stopped" if job.get("cancelled") else "error" if errors else "done"
                if errors:
                    result = errors[-1]
            with self._lock:
                job.update(status=status, result=str(result or "")[:24000], agent=None)
        except Exception as exc:
            with self._lock:
                job.update(status="error", result=f"Subagent failed: {type(exc).__name__}", agent=None)
        finally:
            try:
                self._save_project_agent(child_id, job)
            except (OSError, ValueError):
                pass
            job["done"].set()

    def snapshot(self, child_id):
        with self._lock:
            job = self._jobs.get(str(child_id))
            if not job:
                return None
            return {"id": str(child_id), "task": job["task"], "status": job["status"],
                    "result": job["result"], "events": list(job["events"])}

    def pending_results(self):
        with self._lock:
            return [key for key, job in self._jobs.items() if not job["delivered"]]

    def collect_results(self, ids):
        with self._lock:
            rows = []
            for key in ids:
                job = self._jobs.get(key)
                if job and not job["delivered"] and job["status"] != "running":
                    job["delivered"] = True
                    rows.append({"id": key, "task": job["task"],
                                 "status": job["status"], "result": job["result"]})
            return rows

    def stop_all(self):
        with self._lock:
            jobs = list(self._jobs.values())
            for job in jobs:
                if job["status"] == "running":
                    job["cancelled"] = True
                    if job["agent"]:
                        job["agent"].stop_requested = True
