"""`beecode serve` — the agent behind a local HTTP API for BeeCode Desktop.

Stdlib only (`http.server`, like the pool): one process runs the real `Agent`,
and the web UI talks to it as JSON in, SSE out. Nothing is reimplemented —
`agent.run_sync` runs the same loop the terminal does, in the handler thread,
so tools, sessions, permissions, trust and the forge loop behave identically.

Auth: binds 127.0.0.1 only. Any local process can drive it, which on a
single-user desktop is the documented trade, not a boundary: seats, keys and
signatures still live where they always did (the agent's config), and nothing
here listens on the network.

Endpoints (all JSON unless noted):
  GET  /app/<path>            the bundled web UI (needs --ui)
  GET  /api/health            {ok, version}
  GET  /api/models            [{id, window, measured}] for the active provider
  GET  /api/sessions          [{id, messages, age}]
  POST /api/sessions/new      {id}
  GET  /api/session?id=       {id, messages:[{role, content}]}
  POST /api/chat              {session_id?, message, model?} -> text/event-stream
  POST /api/decide            {id, answer}   (answers a mid-turn question)
  POST /api/allow             {tool}          (grant for future turns)
  POST /api/stop              interrupt the running turn
  GET  /api/trust             {trusted, claims}
  POST /api/trust             {answer: yes|no}
  POST /api/config            {model?, provider?, mode?}
  GET  /api/providers         {active, providers, presets} (keys masked, never whole)
  POST /api/providers         {action: use|save|remove, ...} (provider CRUD for settings)
  GET  /api/pool              {url, hasSeat, seatTail, server, seat} (pool status)
  POST /api/pool              {action: enroll|url|google|google-status, ...}
  POST /api/cmd               {command: "/pool status"} (run a CLI command, get text back)
  GET  /api/pair              {code, paired, running, note} (the phone channel)
  POST /api/pair              {action: start|stop} (open/close the phone channel)
  GET  /api/usage             {tokens, limit, pct} for a session (?id=)

Chat streams `text/event-stream` frames `event: <type>\\ndata: <json>\\n\\n`:
  start  {session_id, model} · delta {kind, text} · tool {phase, tool, ...} ·
  denied {tool, message} · ask {id, question} · done {text} · ready {session_id, usage} ·
  error {message} · stopped {} · info {name, data}
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 1 * 1024 * 1024
ASK_TIMEOUT = 300.0

_state: dict = {
    "agent": None,
    "config": None,
    "workdir": ".",
    "ui": None,
    "lock": threading.Lock(),
    "asks": {},            # ask id -> {"event", "question", "answer"}
    "ask_seq": 0,
    "asks_lock": threading.Lock(),
    "port": 0,
    "relay": None,         # the phone channel's record, while /pair is live
}


# --- small helpers ------------------------------------------------------------

def _send_json(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _read_json(handler: BaseHTTPRequestHandler):
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return None
    if length <= 0 or length > MAX_BODY:
        return None
    try:
        raw = handler.rfile.read(length)
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _workdir() -> str:
    return str(_state["workdir"] or ".")


def _agent():
    return _state["agent"]


def _config():
    return _state["config"]


# --- usage numbers (same ruler as /token) -------------------------------------

def _usage(session) -> dict:
    try:
        from beeagent.utils.tokens import count_tokens

        agent, config = _agent(), _config()
        built = agent.context.build_messages(session.to_dicts(),
                                             agent.tools.to_schemas())
        total = sum(count_tokens(str(m.get("content") or ""),
                                 getattr(config, "model", "") or "")
                    for m in built)
        limit = int(getattr(agent.context, "max_tokens", 0) or 0) or 1
        pct = max(0, min(100, int(100 * total / limit)))
        from beeagent.core.context import advertised_window
        from beeagent.core import windows
        model = getattr(agent.context, "model", "") or getattr(config, "model", "") or ""
        configured = getattr(agent.context, "model_window", None)
        context_window = int(configured or advertised_window(model) or 0)
        context_pct = max(0, min(100, int(100 * total / context_window))) if context_window else 0
        return {"tokens": total, "limit": limit, "pct": pct,
                "contextWindow": context_window, "contextPct": context_pct,
                "contextEstimated": not bool(configured or windows.measured(model))}
    except Exception:
        return {"tokens": 0, "limit": 0, "pct": 0}


# --- the blocking question -----------------------------------------------------

def _waiter(callback, question: str) -> str:
    """Executor's `_ask_user` replacement while serving: announce over SSE,
    then wait for POST /api/decide instead of stdin."""
    with _state["asks_lock"]:
        _state["ask_seq"] += 1
        ask_id = f"ask-{_state['ask_seq']}-{int(time.time())}"
        record = {"event": threading.Event(), "question": question,
                  "answer": "(user did not answer)"}
        _state["asks"][ask_id] = record
    callback("serve_ask", {"id": ask_id, "question": question})
    record["event"].wait(ASK_TIMEOUT)
    with _state["asks_lock"]:
        _state["asks"].pop(ask_id, None)
    return record["answer"]


# --- handler -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "beecode-desktop"

    def _request_allowed(self):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "").lower()
        origin = self.headers.get("Origin", "")
        if host not in hosts or (origin and origin not in {f"http://{h}" for h in hosts}):
            _send_json(self, 403, {"error": "untrusted request origin"})
            return False
        if self.command == "POST" and self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            _send_json(self, 415, {"error": "application/json required"})
            return False
        return True

    def log_message(self, *args):
        pass

    def _sse(self, kind: str, payload: dict) -> None:
        data = (f"event: {kind}\n"
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")
        self.wfile.write(data.encode("utf-8"))
        self.wfile.flush()

    def _sse_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    # -- routing ---------------------------------------------------------------

    def do_GET(self):
        if not self._request_allowed():
            return
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/api/health":
            from beeagent import __version__

            return _send_json(self, 200, {"ok": True, "version": __version__})
        if path == "/api/whoami":
            try:
                import getpass

                name = getpass.getuser() or ""
            except Exception:
                name = ""
            return _send_json(self, 200, {"name": name})
        if path == "/api/models":
            return _send_json(self, 200, _models())
        if path == "/api/subagent":
            agent = _agent()
            tool = agent.tools.get("subagent") if agent else None
            child_id = str((query.get("id") or [""])[0])
            child = tool.snapshot(child_id) if tool else None
            if child is None:
                from beeagent.core.projects import Projects
                try:
                    child = Projects(_workdir()).agent(child_id)
                    if (child and child.get("status") == "running" and
                            time.time() - float(child.get("updated_at") or 0) > 4):
                        child = dict(child, status="unavailable")
                except (OSError, ValueError):
                    return _send_json(self, 500, {"error": "Не удалось прочитать проект"})
            if child is None:
                return _send_json(self, 404, {"error": "subagent not found"})
            return _send_json(self, 200, child)
        if path == "/api/projects":
            from beeagent.core.projects import Projects
            try:
                projects = Projects(_workdir()).list()
                for project in projects:
                    project["agents"] = [{k: entry.get(k) for k in ("id", "task", "status", "parent", "folder")}
                                         for entry in project.get("agents", [])]
                return _send_json(self, 200, {"projects": projects})
            except (OSError, ValueError):
                return _send_json(self, 500, {"error": "Не удалось прочитать проекты"})
        if path == "/api/sessions":
            return _send_json(self, 200, {"sessions": _sessions()})
        if path == "/api/session":
            return self._session(str((query.get("id") or [""])[0]))
        if path == "/api/trust":
            return _send_json(self, 200, _trust_status())
        if path == "/api/providers":
            return _send_json(self, 200, _providers())
        if path == "/api/extensions":
            agent = _agent()
            if agent is None:
                return _send_json(self, 503, {"error": "agent not ready"})
            loader = agent.plugins
            builtin_tools = sum(agent.tools.get('mcp_customuze_beecode_' + name) is not None
                                for name in ('ui', 'instructions'))
            servers = [{"name": "customuze_beecode", "enabled": bool(builtin_tools), "withheld": False,
                        "tools": builtin_tools, "discovered": bool(builtin_tools), "builtin": True}]
            for name, cfg in loader.manager.mcp_servers().items():
                if name == "customuze_beecode":
                    continue
                if not isinstance(cfg, dict):
                    continue
                cached = loader.mcp.cached_tools(name)
                servers.append({"name": name, "enabled": bool(cfg.get("enabled", True)),
                                "withheld": loader.is_withheld("mcp", name),
                                "tools": len(cached) if isinstance(cached, list) else 0,
                                "discovered": isinstance(cached, list)})
            return _send_json(self, 200, {
                "skills": [{"name": s.name, "description": "Поиск и установка навыков" if getattr(s, 'builtin', False) and s.name == 'skill-finder' else s.description,
                            "builtin": bool(getattr(s, 'builtin', False))} for s in loader.skills],
                "servers": servers, "errors": list(loader.load_errors)})
        if path == "/api/pool":
            return _send_json(self, 200, _pool_status())
        if path == "/api/pair":
            return _send_json(self, 200, _pair_status())
        if path == "/api/usage":
            return self._usage(str((query.get("id") or [""])[0]))
        if path == "/api/ui":
            return self._ui()
        if path == "/api/ui-css":
            return self._ui_css()
        if path == "/api/config":
            agent, config = _agent(), _config()
            if agent is None:
                return _send_json(self, 503, {"error": "agent not ready"})
            economy = getattr(config, "economy", None)
            return _send_json(self, 200, {
                "model": config.model, "provider": config.provider,
                "mode": config.mode,
                "language": getattr(config, "language", "en"),
                "nativeTools": bool(getattr(config, "native_tools", True)),
                "maxTurns": int(getattr(config, "max_turns", 50) or 50),
                "maxContextTokens": int(getattr(config, "max_context_tokens", 0) or 0),
                "streamIdleTimeout": int(getattr(config, "stream_idle_timeout", 90) or 90),
                "cacheEnabled": bool(getattr(economy, "cache_enabled", True)),
                "cacheTtlMinutes": int(getattr(economy, "cache_ttl_minutes", 30) or 30),
                "customInstructions": getattr(config, "custom_instructions", "") or "",
                "workdir": str(Path(_workdir()).resolve()),
                "permissions": getattr(getattr(config, "permissions", None),
                                       "mode", "?")})
        if path == "/" :
            return self._redirect("/app/")
        if path == "/app/" or path.startswith("/app/"):
            return self._static(path[5:] or "index.html")
        return _send_json(self, 404, {"error": "unknown path"})

    def do_POST(self):
        if not self._request_allowed():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/sessions/new":
            return self._session_new()
        if path == "/api/projects":
            return self._projects()
        if path == "/api/chat":
            return self._chat()
        if path == "/api/decide":
            return self._decide()
        if path == "/api/allow":
            return self._allow()
        if path == "/api/stop":
            agent = _agent()
            stopped = bool(agent.request_stop()) if agent is not None else False
            return _send_json(self, 200, {"stopped": stopped})
        if path == "/api/trust":
            return self._trust()
        if path == "/api/config":
            return self._config()
        if path == "/api/ui/reset":
            from beeagent.tools.ui import UiTool
            result = UiTool()._reset(_workdir())
            return _send_json(self, 500 if result.error else 200,
                              {"ok": not result.error, "message": result.output})
        if path == "/api/providers":
            return self._providers()
        if path == "/api/extensions/connect":
            body = _read_json(self)
            agent = _agent()
            if agent is None:
                return _send_json(self, 503, {"error": "agent not ready"})
            if not isinstance(body, dict) or not isinstance(body.get("name"), str):
                return _send_json(self, 400, {"error": "server name required"})
            try:
                tools = agent.plugins.connect_mcp(body["name"])
            except (ValueError, PermissionError) as exc:
                return _send_json(self, 400, {"error": str(exc)})
            except Exception:
                return _send_json(self, 502, {"error": "MCP server could not be connected"})
            return _send_json(self, 200, {"ok": True, "tools": len(tools)})
        if path == "/api/pool":
            return self._pool()
        if path == "/api/pair":
            return self._pair()
        if path == "/api/cmd":
            return self._cmd()
        return _send_json(self, 404, {"error": "unknown path"})

    def _redirect(self, where: str) -> None:
        self.send_response(302)
        self.send_header("Location", where)
        self.end_headers()

    # -- static UI ---------------------------------------------------------------

    _TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
              ".json": "application/json", ".png": "image/png",
              ".ico": "image/x-icon", ".svg": "image/svg+xml"}

    def _static(self, name: str):
        ui = _state["ui"]
        if not ui:
            return _send_json(self, 404, {"error": "no UI bundled (serve --ui ...)"})
        try:
            target = (Path(ui) / urllib.parse.unquote(name)).resolve()
            root = Path(ui).resolve()
            if target == root:
                target = root / "index.html"
            if root not in target.parents and target != root:
                return _send_json(self, 404, {"error": "unknown path"})
            data = target.read_bytes()
        except OSError:
            return _send_json(self, 404, {"error": "unknown path"})
        self.send_response(200)
        self.send_header("Content-Type", self._TYPES.get(target.suffix.lower(),
                                                         "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- reads ---------------------------------------------------------------------

    def _session(self, sid: str):
        from beeagent.core.session import Session

        if not sid:
            return _send_json(self, 400, {"error": "session id required"})
        try:
            session = Session.load(sid, _workdir())
        except Exception as e:
            return _send_json(self, 404, {"error": f"no such session: {e}"})
        return _send_json(self, 200, {
            "id": session.session_id,
            "messages": [{"role": m.role, "content": m.content or ""}
                         for m in session.messages]})

    def _usage(self, sid: str):
        from beeagent.core.session import Session

        if not sid:
            return _send_json(self, 400, {"error": "session id required"})
        try:
            session = Session.load(sid, _workdir())
        except Exception as e:
            return _send_json(self, 404, {"error": f"no such session: {e}"})
        return _send_json(self, 200, _usage(session))

    # -- interface overrides (the `ui` tool writes, the desktop applies) --------

    def _ui(self):
        """What the agent asked the interface to look like, if anything.

        Nulls mean no override: the desktop keeps its own choice. `version`
        bumps on every change so the window can poll cheaply and re-apply
        only when something actually moved.
        """
        from beeagent.tools.ui import custom_css_path, read_prefs

        prefs = read_prefs(_workdir())
        try:
            custom = custom_css_path(_workdir()).exists()
        except OSError:
            custom = False
        return _send_json(self, 200, {
            "skin": prefs["skin"], "accent": prefs["accent"],
            "theme": prefs["theme"], "customCss": custom,
            "hiddenComponents": prefs["hidden_components"], "components": prefs["components"],
            "updated_at": prefs["updated_at"], "version": prefs["version"]})

    def _ui_css(self):
        """The agent's custom stylesheet, or 404 when there is none."""
        from beeagent.tools.ui import custom_css_path

        try:
            data = custom_css_path(_workdir()).read_bytes()
        except OSError:
            return _send_json(self, 404, {"error": "no custom stylesheet"})
        self.send_response(200)
        self.send_header("Content-Type", "text/css")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _projects(self):
        from beeagent.core.projects import Projects
        body = _read_json(self)
        if body is None: return _send_json(self, 400, {"error": "Некорректный запрос"})
        store = Projects(_workdir())
        try:
            if body.get("action") == "create":
                item = store.create(body.get("name"))
            elif body.get("action") == "folder":
                item = store.add_folder(body.get("project"), body.get("name"), body.get("parent") or "")
            else:
                return _send_json(self, 400, {"error": "Неизвестное действие"})
            return _send_json(self, 200, {"item": item})
        except (ValueError, TypeError) as exc:
            return _send_json(self, 400, {"error": str(exc)})
        except OSError:
            return _send_json(self, 500, {"error": "Не удалось сохранить проект"})

    def _session_new(self):
        from beeagent.core.session import Session
        from beeagent.core.projects import Projects

        body = _read_json(self) or {}
        project = body.get("project") or ""
        folder = body.get("folder") or ""
        store = Projects(_workdir())
        try:
            if not isinstance(project, str) or not isinstance(folder, str) or (folder and not project):
                raise ValueError("Неверное расположение чата")
            if project: store.validate_location(project, folder)
            session = Session()
            session.save(_workdir())
            if project: store.add_chat(project, folder, session.session_id)
            return _send_json(self, 200, {"id": session.session_id})
        except ValueError as exc:
            return _send_json(self, 400, {"error": str(exc)})
        except OSError:
            return _send_json(self, 500, {"error": "Не удалось создать чат"})

    # -- chat ------------------------------------------------------------------------

    def _chat(self):
        from beeagent.core import executor
        from beeagent.core.session import Session

        body = _read_json(self)
        if body is None:
            return _send_json(self, 400, {"error": "JSON body with message required"})
        message = str(body.get("message") or "").strip()
        if not message:
            return _send_json(self, 400, {"error": "message required"})
        agent, config = _agent(), _config()
        if agent is None:
            return _send_json(self, 503, {"error": "agent not ready"})
        if not _state["lock"].acquire(blocking=False):
            return _send_json(self, 429, {"error": "a turn is already running"})
        try:
            sid = str(body.get("session_id") or "")
            try:
                session = Session.load(sid, _workdir()) if sid else Session()
            except Exception as e:
                return _send_json(self, 404, {"error": f"no such session: {e}"})
            agent.project_session_id = session.session_id
            session.request_title = not any(m.role in ("user", "assistant") for m in session.messages)
            wanted = str(body.get("model") or "").strip()
            effort = body.get("reasoning_effort", "")
            if not isinstance(effort, str):
                return _send_json(self, 400, {"error": "Неверный уровень рассуждения"})
            if effort:
                from beeagent.providers.openai_compat import OpenAICompatProvider
                selected_provider = agent.providers.get(config.provider)
                custom = next((p for p in getattr(config, 'custom_providers', [])
                               if str(p.name).lower() == str(config.provider).lower()), None)
                details = next((item for item in (getattr(custom, 'model_details', []) or [])
                                if isinstance(item, dict) and item.get('id') == (wanted or config.model)), None)
                if (not isinstance(selected_provider, OpenAICompatProvider) or not details or
                        not details.get('reasoning') or effort not in (details.get('efforts') or [])):
                    return _send_json(self, 400, {"error": "Уровень рассуждения не настроен для этой модели и провайдера"})
            if wanted and wanted != config.model:
                config.model = wanted
                agent.context.model = wanted
                try:
                    from beeagent.config.loader import save_config

                    save_config(config, _workdir())
                except Exception:
                    pass
            self._sse_headers()
            self._sse("start", {"session_id": session.session_id,
                                "model": config.model})
            events: list = []
            title_pending = bool(session.request_title)
            title_buffer = ""
            title_captured = False

            def title_delta(text, final=False):
                nonlocal title_pending, title_buffer, title_captured
                if not title_pending:
                    return text
                title_buffer += str(text or "")
                candidate = title_buffer.lstrip()
                if not candidate and not final and len(title_buffer) < 128:
                    return ""
                if candidate.startswith("█"):
                    end = candidate.find("█", 1)
                    if end < 0 and not final and len(candidate) <= 100:
                        return ""
                    if 1 < end <= 81:
                        title = " ".join(candidate[1:end].split())
                        if title and not any(ord(ch) < 32 for ch in title):
                            session.title = title[:80]
                            title_captured = True
                            self._sse("title", {"title": session.title})
                            title_pending = False
                            title_buffer = ""
                            return candidate[end + 1:].lstrip()
                title_pending = False
                result, title_buffer = title_buffer, ""
                return result

            def callback(event, data):
                data = data or {}
                if event in ("stream_delta", "reasoning_delta", "response"):
                    text = data.get("text") or ""
                    if event != "reasoning_delta":
                        text = title_delta(text)
                    if text:
                        self._sse("delta", {"kind": event, "text": text})
                elif event == "tool_start":
                    leftover = title_delta("", final=True) if title_buffer else ""
                    if leftover:
                        self._sse("delta", {"kind": "stream_delta", "text": leftover})
                    self._sse("tool", {"phase": "start", "tool": data.get("tool"),
                                       "args": data.get("args") or {},
                                       "call": data.get("call")})
                elif event == "tool_end":
                    output = str(data.get("output") or "")
                    self._sse("tool", {"phase": "end", "tool": data.get("tool"),
                                       "error": bool(data.get("error")),
                                       "output": output[:2000],
                                       "call": data.get("call"),
                                       "metadata": data.get("metadata") or {}})
                elif event in ("tool_renamed", "tool_unknown", "tool_error"):
                    self._sse("tool", {"phase": "note", **{k: data.get(k)
                                                            for k in ("tool", "message",
                                                                      "from", "to")}})
                elif event == "tool_denied":
                    self._sse("denied", {"tool": data.get("tool"),
                                         "message": data.get("message") or ""})
                elif event in ("serve_ask", "ask"):
                    self._sse("ask", {"id": data.get("id"),
                                       "question": data.get("question") or "",
                                       "meta": data.get("meta") or {}})
                elif event == "done":
                    events.append(("done", data.get("text") or ""))
                elif event == "error":
                    events.append(("error", data.get("message") or "error"))
                elif event == "stopped":
                    self._sse("stopped", {})
                elif event in ("status", "waiting", "retry", "context_trimmed",
                               "economy_hit"):
                    self._sse("info", {"name": event, "data": str(data)[:300]})
                # nudged/thinking chatter stays out: the stream is for answers.

            callback.handles_questions = True
            original = executor._ask_user
            executor._ask_user = _waiter
            from beeagent.providers.openai_compat import reasoning_effort_var
            effort_token = reasoning_effort_var.set(effort)
            try:
                answer = agent.run_sync(message, session, callback)
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as e:
                self._sse("error", {"message": f"{e.__class__.__name__}: {e}"})
                return
            finally:
                executor._ask_user = original
                reasoning_effort_var.reset(effort_token)
            leftover = title_delta("", final=True) if title_buffer else ""
            if leftover:
                self._sse("delta", {"kind": "stream_delta", "text": leftover})
            if title_captured:
                import re
                for row in session.messages:
                    if row.role == "assistant":
                        row.content = re.sub(r"^\s*█[^█\r\n]{1,80}█\s*", "", row.content or "", count=1)
                        break
                events = [(kind, re.sub(r"^\s*█[^█\r\n]{1,80}█\s*", "", text, count=1)
                           if kind == "done" else text) for kind, text in events]
                answer = re.sub(r"^\s*█[^█\r\n]{1,80}█\s*", "", answer or "", count=1)
            for kind, text in events:
                self._sse(kind, {"text": text} if kind == "done"
                          else {"message": text})
            if not events:
                self._sse("done", {"text": answer or ""})
            try:
                session.save(_workdir())
            except OSError:
                pass
            self._sse("ready", {"session_id": session.session_id,
                                "usage": _usage(session)})
        finally:
            _state["lock"].release()

    # -- decisions, grants, trust, config ------------------------------------------------

    def _decide(self):
        body = _read_json(self)
        if body is None:
            return _send_json(self, 400, {"error": "JSON body required"})
        ask_id = str(body.get("id") or "")
        from beeagent.core import asks

        if asks.answer(ask_id, str(body.get("answer") or "")):
            return _send_json(self, 200, {"answered": True})
        with _state["asks_lock"]:
            record = _state["asks"].get(ask_id)
        if record is None:
            return _send_json(self, 404, {"error": "no such question (answered or timed out)"})
        record["answer"] = str(body.get("answer") or "(user did not answer)")
        record["event"].set()
        return _send_json(self, 200, {"answered": True})

    def _allow(self):
        body = _read_json(self)
        if body is None:
            return _send_json(self, 400, {"error": "JSON body required"})
        tool = str(body.get("tool") or "").strip()
        if not tool:
            return _send_json(self, 400, {"error": "tool required"})
        agent = _agent()
        if agent is None:
            return _send_json(self, 503, {"error": "agent not ready"})
        agent.permissions.grant(tool)
        return _send_json(self, 200, {"allowed": tool,
                                      "note": "granted for future turns — "
                                              "ask the question again"})

    def _trust(self):
        from beeagent.core import trust

        body = _read_json(self)
        if body is None:
            return _send_json(self, 400, {"error": "JSON body required"})
        gate = trust.for_folder(_workdir())
        word = str(body.get("answer") or "").strip().lower()
        if word in ("yes", "y", "allow", "trust"):
            if gate.say_trusted():
                lines = [str(line) for line in trust.apply_grant(_agent(), gate)]
                return _send_json(self, 200, {"trusted": True, "applied": lines})
            return _send_json(self, 200, {"trusted": True, "applied": []})
        if word in ("no", "n", "deny", "refuse"):
            gate.say_declined()
            return _send_json(self, 200, {"trusted": False})
        return _send_json(self, 400, {"error": "answer yes or no"})

    def _config(self):
        body = _read_json(self)
        if body is None:
            return _send_json(self, 400, {"error": "JSON body required"})
        agent, config = _agent(), _config()
        if agent is None:
            return _send_json(self, 503, {"error": "agent not ready"})
        changed = []
        if body.get("model"):
            config.model = str(body["model"])
            agent.context.model = config.model
            changed.append("model")
        if body.get("provider"):
            config.provider = str(body["provider"])
            changed.append("provider")
        if body.get("mode") in ("normal", "economy"):
            config.mode = str(body["mode"])
            try:
                economy = agent.economy
                economy.mode = config.mode
                if config.mode == "economy" and economy.cache is None:
                    from beeagent.utils.cache import ResponseCache

                    economy.cache = ResponseCache(config.economy.cache_dir)
                elif config.mode == "normal":
                    economy.cache = None
            except Exception:
                pass
            changed.append("mode")
        if body.get("language") in ("en", "ru"):
            config.language = str(body["language"])
            try:
                from beeagent.i18n import set_lang

                set_lang(config.language)
            except Exception:
                pass
            changed.append("language")
        if isinstance(body.get("nativeTools"), bool):
            config.native_tools = bool(body["nativeTools"])
            changed.append("nativeTools")
        for key, field, low, high in (
                ("maxTurns", "max_turns", 1, 500),
                ("maxContextTokens", "max_context_tokens", 0, 1000000),
                ("streamIdleTimeout", "stream_idle_timeout", 10, 3600),
                ("cacheTtlMinutes", None, 1, 1440)):
            if body.get(key) is None:
                continue
            try:
                value = int(body[key])
            except (TypeError, ValueError):
                return _send_json(self, 400, {"error": f"{key}: нужно число"})
            if not low <= value <= high:
                return _send_json(self, 400,
                                  {"error": f"{key}: число от {low} до {high}"})
            if field is not None:
                setattr(config, field, value)
            else:
                config.economy.cache_ttl_minutes = value
            changed.append(key)
        if isinstance(body.get("cacheEnabled"), bool):
            config.economy.cache_enabled = bool(body["cacheEnabled"])
            changed.append("cacheEnabled")
        if body.get("customInstructions") is not None:
            text = str(body["customInstructions"])
            if len(text) > 8000:
                return _send_json(self, 400, {"error": "customInstructions: максимум 8000 символов"})
            config.custom_instructions = text
            changed.append("customInstructions")
        if body.get("permissions") in ("ask", "auto", "readonly"):
            agent.permissions.set_mode(str(body["permissions"]))
            agent.sync_config_permissions()
            changed.append("permissions")
        try:
            from beeagent.config.loader import save_config

            save_config(config, _workdir())
        except Exception:
            return _send_json(self, 500, {"error": "settings changed in memory but could not be saved"})
        return _send_json(self, 200, {"changed": changed,
                                      "model": config.model,
                                      "provider": config.provider,
                                      "mode": config.mode,
                                      "language": getattr(config, "language", "en"),
                                      "nativeTools": bool(getattr(config, "native_tools", True)),
                                      "maxTurns": int(getattr(config, "max_turns", 50) or 50),
                                      "maxContextTokens": int(getattr(config, "max_context_tokens", 0) or 0),
                                      "streamIdleTimeout": int(getattr(config, "stream_idle_timeout", 90) or 90),
                                      "cacheEnabled": bool(getattr(config.economy, "cache_enabled", True)),
                                      "cacheTtlMinutes": int(getattr(config.economy, "cache_ttl_minutes", 30) or 30),
                                      "customInstructions": getattr(config, "custom_instructions", "") or "",
                                      "permissions": getattr(
                                          config.permissions, "mode", "?")})

    def _providers(self):
        """Provider CRUD for the settings screen.

        Actions (all JSON):
          use    {name} — switch to a registered provider
          save   {name, url?, keys?, models?} — store a key for a preset or
                 create/update a custom endpoint (models: "a,b,c" or [..])
          remove {name} — drop a custom endpoint (and its keys)
        Keys travel in the request body the way `/key` takes them on the
        command line; they are never returned whole, only masked.
        """
        from beeagent.config.loader import save_config
        from beeagent.core import provider_setup
        from beeagent.providers.presets import BY_NAME, key_for

        body = _read_json(self)
        if body is None:
            return _send_json(self, 400, {"error": "JSON body required"})
        agent, config = _agent(), _config()
        if agent is None:
            return _send_json(self, 503, {"error": "agent not ready"})
        action = str(body.get("action") or "").strip().lower()
        name = str(body.get("name") or "").strip().lower()

        if action == "use":
            if not name:
                return _send_json(self, 400, {"error": "name required"})
            endpoint = BY_NAME.get(name)
            if endpoint is not None and not key_for(endpoint, config.api_keys) \
                    and not endpoint.keyless:
                return _send_json(self, 400, {
                    "error": f"{endpoint.label} нужен твой ключ "
                             f"(бесплатно: {endpoint.signup}) — вставь его на "
                             f"вкладке «Провайдеры»",
                    "needsKey": True, "signup": endpoint.signup})
            if agent.providers.get(name) is None:
                return _send_json(self, 404, {
                    "error": f"провайдера «{name}» нет — добавь его "
                             f"на вкладке «Провайдеры»"})
            config.provider = name
            provider = agent.providers.get(name)
            models = list(getattr(provider, "models", None) or [])
            if not models and name == "g4f":
                try:
                    from beeagent.providers.g4f_provider import G4fProvider

                    models = G4fProvider.discover_models()
                except Exception:
                    models = []
            if models:
                config.model = models[0]
                agent.context.model = models[0]
            try:
                save_config(config, _workdir())
            except OSError as exc:
                return _send_json(self, 500, {"error": f"не сохранено: {exc}"})
            return _send_json(self, 200, {"ok": True, "provider": name,
                                          "model": config.model,
                                          "providers": _providers()})

        if action == "save":
            if not name:
                return _send_json(self, 400, {"error": "назови провайдера"})
            details = body.get("modelDetails")
            if details is not None:
                if not isinstance(details, list) or len(details) > 100:
                    return _send_json(self, 400, {"error": "неверный список моделей"})
                clean = []
                seen = set()
                previous = next((p for p in getattr(config, 'custom_providers', [])
                                 if str(p.name).lower() == name.lower()), None)
                old_efforts = {m.get('id'): m.get('efforts', []) for m in
                               (getattr(previous, 'model_details', []) or []) if isinstance(m, dict)}
                allowed_efforts = {'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}
                for item in details:
                    if not isinstance(item, dict):
                        return _send_json(self, 400, {"error": "неверные данные модели"})
                    mid = str(item.get("id") or "").strip()
                    label = str(item.get("name") or "").strip()
                    context = item.get("context")
                    reasoning = item.get("reasoning", False)
                    efforts = item.get('efforts', old_efforts.get(mid, []))
                    if not mid or len(mid) > 256 or mid in seen or len(label) > 256:
                        return _send_json(self, 400, {"error": "проверь ID и названия моделей"})
                    if not isinstance(reasoning, bool) or (context is not None and
                            (isinstance(context, bool) or not isinstance(context, int) or not 1024 <= context <= 10000000)):
                        return _send_json(self, 400, {"error": "проверь reasoning и размер контекста"})
                    if (not isinstance(efforts, list) or len(efforts) > len(allowed_efforts) or
                            any(not isinstance(level, str) or level not in allowed_efforts for level in efforts) or
                            len(set(efforts)) != len(efforts)):
                        return _send_json(self, 400, {"error": "проверь уровни рассуждения"})
                    seen.add(mid)
                    clean.append({"id": mid, "name": label or mid,
                                  "reasoning": reasoning, "context": context,
                                  "efforts": efforts if reasoning else []})
                details = clean
            raw_models = body.get("models", "")
            if isinstance(raw_models, str):
                models = provider_setup.split_models(raw_models)
            else:
                models = provider_setup.split_models(list(raw_models or []))
            if details is not None and [item["id"] for item in details] != models:
                return _send_json(self, 400, {"error": "ID моделей не совпадают со списком"})
            supplied_keys = str(body.get("keys") or "")
            if body.get("keepKeys") is True and not supplied_keys:
                supplied_keys = ",".join(provider_setup.stored_keys(config, name))
            fields = provider_setup.Fields(
                name=name,
                url=str(body.get("url") or ""),
                keys=supplied_keys,
                models=models,
                model_details=details,
            )
            message, refusal = provider_setup.apply(
                config, agent, fields, workdir=_workdir())
            if refusal:
                return _send_json(self, 400, {"error": refusal})
            return _send_json(self, 200, {"ok": True, "message": message,
                                          "providers": _providers()})

        if action == "remove":
            if not name:
                return _send_json(self, 400, {"error": "name required"})
            if name in provider_setup.BUILTIN_NAMES or name in BY_NAME:
                return _send_json(self, 400, {
                    "error": f"«{name}» встроен — удалить можно только "
                             f"свой эндпоинт, ключ стирается через пустое поле"})
            parts, refusal = provider_setup.forget(config, agent, name,
                                                   workdir=_workdir())
            if refusal:
                return _send_json(self, 400, {"error": refusal})
            if not parts:
                return _send_json(self, 404, {
                    "error": f"провайдера «{name}» нет"})
            return _send_json(self, 200, {"ok": True,
                                          "message": ", ".join(parts),
                                          "providers": _providers()})

        return _send_json(self, 400, {
            "error": "action: use, save или remove"})

    def _pool(self):
        """Pool enrolment for the settings screen — no terminal needed.

        Actions (all JSON):
          url    {url} — point at a pool box (http(s)://…), saved + live
          enroll {secret?} — take a seat: the install key is made in
                 ~/.beecode if missing, only its public half travels
        After `enroll` switch the provider over with POST /api/providers
        {action: use, name: pool} — the settings screen offers that button.
        """
        from beeagent.config.loader import save_config
        from beeagent.providers import pool as pool_mod

        body = _read_json(self)
        if body is None:
            return _send_json(self, 400, {"error": "JSON body required"})
        agent, config = _agent(), _config()
        if agent is None:
            return _send_json(self, 503, {"error": "agent not ready"})
        action = str(body.get("action") or "").strip().lower()

        if action in ("google", "google-status"):
            url = (config.pool_url or "").strip().rstrip("/")
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return _send_json(self, 400, {"error": "сначала задай полный адрес пула"})
            try:
                if action == "google":
                    return _send_json(self, 200, {
                        "ok": True, "url": pool_mod.google_authorize_url(url)})
                ident = pool_mod.install_id()
                seat = pool_mod.seat_status(url, ident)
            except Exception as exc:
                return _send_json(self, 502, {"error": f"пул не ответил: {exc}"})
            if not seat.get("ok"):
                status = seat.get("status")
                if status == 401 and "prove this install" in seat.get("detail", ""):
                    return _send_json(self, 400, {"error": seat.get("error")})
                if status and status != 401 and status < 500:
                    return _send_json(self, 400, {"error": seat.get("error") or "место отклонено"})
                return _send_json(self, 200, {
                    "complete": False, "message": seat.get("error") or "ожидаю вход"})
            if not seat.get("approved", False):
                return _send_json(self, 200, {
                    "complete": False, "message": "место ожидает одобрения владельца пула"})
            previous = config.pool_token
            if previous != ident:
                config.pool_token = ident
                try:
                    save_config(config, _workdir())
                except Exception as exc:
                    config.pool_token = previous
                    return _send_json(self, 500, {"error": f"не сохранено: {exc}"})
            _refresh_pool_provider(agent, config)
            seat = _seat_budget(seat)
            return _send_json(self, 200, {
                "ok": True, "complete": True,
                "message": "Место получено. Включи провайдер pool кнопкой ниже.",
                "pool": {"url": url, "hasSeat": True, "seatTail": "…" + ident[-4:],
                         "server": {"reachable": True, "detail": "отвечает"}, "seat": seat}})

        if action == "url":
            url = str(body.get("url") or "").strip().rstrip("/")
            if not url.startswith(("http://", "https://")):
                return _send_json(self, 400, {
                    "error": "нужен полный адрес: https://pool.example.com"})
            config.pool_url = url
            try:
                save_config(config, _workdir())
            except OSError as exc:
                return _send_json(self, 500, {"error": f"не сохранено: {exc}"})
            _refresh_pool_provider(agent, config)
            note = "" if url.startswith("https://") else \
                " ⚠ простой http — токен места едет открытым текстом"
            return _send_json(self, 200, {"ok": True, "url": url,
                                          "message": f"адрес пула: {url}.{note} "
                                                     f"Теперь возьми место кнопкой ниже.",
                                          "pool": _pool_status()})

        if action == "enroll":
            url = (config.pool_url or "").rstrip("/")
            if not url:
                return _send_json(self, 400, {
                    "error": "сначала задай адрес пула"})
            try:
                answer = pool_mod.enroll(
                    url, secret=str(body.get("secret") or "").strip())
            except Exception as exc:
                return _send_json(self, 400, {"error": f"пул не ответил: {exc}"})
            token = str(answer.get("id") or answer.get("token") or "")
            if not token:
                return _send_json(self, 400, {"error": "пул не дал id места"})
            config.pool_token = token
            try:
                save_config(config, _workdir())
            except OSError as exc:
                return _send_json(self, 500, {"error": f"не сохранено: {exc}"})
            _refresh_pool_provider(agent, config)
            return _send_json(self, 200, {
                "ok": True,
                "message": f"место получено: …{token[-4:]} — "
                           f"{answer.get('requests_per_day', '?')} запросов и "
                           f"{answer.get('tokens_per_day', '?')} токенов в сутки. "
                           f"Включи провайдер «pool» кнопкой ниже.",
                "pool": _pool_status()})

        return _send_json(self, 400, {
            "error": "action: enroll, url, google или google-status"})

    # Commands the desktop composer accepts after `/`. This is the same
    # `dispatch` the terminal uses — same handlers, same texts — but only the
    # non-interactive doors: nothing here may ask a follow-up question, open a
    # TUI form or clear anyone's screen. Everything else is answered with where
    # it lives instead (settings tabs, or the chat itself for tools).
    _CMD_ALLOW = frozenset({
        "help", "about", "config", "pool", "providers", "provider",
        "key", "model", "models", "mode", "permissions", "allow",
        "lang", "instructions", "stats", "usage", "token", "tools", "window",
    })
    _CMD_ELSEWHERE = {
        "run": "инструменты запускаются из чата — просто попроси словами",
        "read": "чтение файлов — из чата, просто попроси словами",
        "search": "поиск — из чата, просто попроси словами",
        "find": "поиск файлов — из чата, просто попроси словами",
        "status": "git — из чата, просто попроси словами",
        "diff": "git — из чата, просто попроси словами",
        "log": "git — из чата, просто попроси словами",
        "sessions": "чаты — в сайдбаре слева",
        "continue": "чаты — в сайдбаре слева",
        "resume": "чаты — в сайдбаре слева",
        "load": "чаты — в сайдбаре слева",
        "history": "история — открой чат в сайдбаре",
        "session": "этот чат уже открыт",
        "save": "чаты сохраняются сами после каждого ответа",
        "export": "экспорт — попроси в чате, агент выгрузит сам",
        "reset": "новый чат — кнопка слева вверху",
        "new": "новый чат — кнопка слева вверху",
        "stop": "остановка — кнопка ■ под полем ввода",
        "tasks": "задачи — видны прямо в ответах агента",
        "trust": "доверие — жёлтый баннер сверху, когда папка что-то хочет",
        "skin": "оформление терминала — в десктопе не применяется",
        "theme": "тема десктопа — пока одна, Paper",
        "bee": "пчёлка живёт только в терминале",
        "clear": "очистка экрана — в десктопе не нужна",
        "quit": "выход — крестик окна",
        "update": "обновления — вкладка «Папка» подскажет, остальное само",
        "plugins": "плагины — пока только в терминале",
        "plugin": "плагины — пока только в терминале",
        "skills": "навыки — пока только в терминале",
        "skill": "навыки — пока только в терминале",
        "mcp": "MCP-серверы — пока только в терминале",
        "extensions": "расширения — пока только в терминале",
        "thinking": "размышления модели видны в ответах",
        "compact": "сжатие истории — агент делает сам",
        "bug": "багрепорт — напиши словами в чат",
    }

    def _cmd(self):
        from beeagent.ui.commands import ReplContext, dispatch

        body = _read_json(self)
        if body is None:
            return _send_json(self, 400, {"error": "JSON body required"})
        agent, config = _agent(), _config()
        if agent is None:
            return _send_json(self, 503, {"error": "agent not ready"})
        line = str(body.get("command") or "").strip()
        if not line.startswith("/"):
            return _send_json(self, 400, {
                "error": "команда начинается с / — например /pool status"})
        name = line[1:].split()[0].lower() if line[1:].split() else ""
        if name in self._CMD_ELSEWHERE:
            return _send_json(self, 200, {"ok": True,
                                          "output": self._CMD_ELSEWHERE[name]})
        if name not in self._CMD_ALLOW:
            return _send_json(self, 400, {
                "error": f"нет команды /{name} — список: /help"})
        ctx = ReplContext(agent=agent, config=config)
        try:
            result = dispatch(ctx, line)
        except Exception as exc:
            return _send_json(self, 500, {
                "error": f"/{name}: {exc.__class__.__name__}: {exc}"})
        return _send_json(self, 200, {"ok": True,
                                      "output": _render_cmd(result.output)})

    def _pair(self):
        """Open the phone channel, or close it, or say where it stands.

        A second start while a channel is already open answers the live code
        rather than stacking a second poller: the pool would answer the fresh
        start with a new channel nobody is listening on, and one phone plus
        one code is the whole grammar of this feature.
        """
        body = _read_json(self)
        if body is None:
            return _send_json(self, 400, {"error": "JSON body required"})
        action = str(body.get("action") or "start").strip().lower()
        relay = _state.get("relay") or {}
        if action == "stop":
            if relay.get("stop") is not None:
                relay["stop"].set()
            relay["note"] = "закрыт"
            _state["relay"] = {"thread": None, "code": relay.get("code") or "",
                               "paired": False, "stop": None,
                               "note": "закрыт кнопкой"}
            return _send_json(self, 200, {"ok": True})
        if action in ("start", "code"):
            config = _config()
            url = (getattr(config, "pool_url", "") or "").rstrip("/")
            if not url:
                return _send_json(self, 400, {
                    "error": "нет адреса пула — впиши его во вкладке «Пул»"})
            living = (relay.get("thread") is not None
                      and relay.get("code") and relay.get("stop") is not None
                      and not relay["stop"].is_set())
            if living:
                return _send_json(self, 200, {"code": relay["code"],
                                              "paired": bool(relay.get("paired"))})
            from beeagent.providers import pool as pool_mod

            try:
                opened = pool_mod.relay_start(url)
            except Exception as exc:
                return _send_json(self, 502, {"error": str(exc)[:200]})
            stop = threading.Event()
            record = {"code": opened.get("code") or "",
                      "paired": False, "stop": stop, "thread": None,
                      "note": ""}
            thread = threading.Thread(target=_relay_worker,
                                      args=(url, stop, record),
                                      daemon=True, name="relay-channel")
            record["thread"] = thread
            _state["relay"] = record
            thread.start()
            return _send_json(self, 200, {"code": record["code"],
                                          "expires_in": opened.get("expires_in")})
        return _send_json(self, 400, {"error": "action: start | stop"})


def _pair_status() -> dict:
    """Where the phone channel stands, for the settings pane to poll."""
    relay = _state.get("relay") or {}
    stop = relay.get("stop")
    return {"code": relay.get("code") or "",
            "paired": bool(relay.get("paired")),
            "running": bool(relay.get("thread") is not None
                            and stop is not None and not stop.is_set()),
            "note": relay.get("note") or ""}


def _relay_worker(url: str, stop_q: threading.Event, record: dict) -> None:
    """Drive one relay channel from inside serve: instructions in, statuses out.

    Everything the phone says becomes a turn on this very serve, through the
    same `/api/chat` the desktop's window writes into — a self-call, so no
    second agent object and no permission gate that differs from the one the
    person at the keyboard lives with. The stream's frames are translated
    into a few short event kinds the phone renders with emoji: busy, tool,
    done, ask, error.
    """
    import httpx

    from beeagent.providers import pool as pool_mod

    base = f"http://127.0.0.1:{_state.get('port')}"

    def say(kind: str, text: str) -> None:
        try:
            pool_mod.relay_report(url, [{"kind": kind, "text": text[:600]}])
        except Exception:
            pass

    after = -1
    connected = False
    while not stop_q.is_set():
        try:
            body = pool_mod.relay_poll(url, after)
        except Exception as exc:
            if "no live relay channel" in str(exc):
                record["note"] = "канал закрылся"
                say("error", "канал закрылся — сделай новый код на компьютере")
                break
            stop_q.wait(4.0)          # the box asleep / a dropped connection
            continue
        if not connected and body.get("status") == "paired":
            connected = True
            record["paired"] = True
            say("connected", "телефон подключён")
        for message in body.get("messages") or []:
            try:
                after = int(message.get("id") or after)
            except (TypeError, ValueError):
                after += 1
            text = str(message.get("text") or "").strip()
            if not text:
                continue
            if text.startswith("/"):
                try:
                    with httpx.Client(timeout=120.0) as client:
                        r = client.post(base + "/api/cmd", json={"command": text})
                    data = r.json() if r.status_code == 200 else {}
                    out = data.get("output") or data.get("error") or f"{r.status_code}"
                    say("done", str(out)[:600])
                except Exception as exc:
                    say("error", f"{exc.__class__.__name__}: {exc}")
                continue
            _relay_prompt(url, say, text)
    record["note"] = record.get("note") or "канал завершён"


def _relay_prompt(url: str, say, message: str) -> None:
    """One phone prompt through /api/chat, frames translated into events."""
    import httpx

    base = f"http://127.0.0.1:{_state.get('port')}"
    tools = 0
    tail = ""
    try:
        with httpx.Client(timeout=900.0) as client:
            with client.stream("POST", base + "/api/chat",
                               json={"message": message}) as resp:
                if resp.status_code != 200:
                    detail = ""
                    try:
                        detail = resp.read().decode("utf-8", errors="replace")[:200]
                    except Exception:
                        detail = str(resp.status_code)
                    say("error", f"сервер отказал ({resp.status_code}): {detail}")
                    return
                name = ""
                for line in resp.iter_lines():
                    if line.startswith("event:"):
                        name = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if not isinstance(data, dict):
                        data = {}
                    if name == "start":
                        say("busy", "задание принял, работаю")
                    elif name == "tool" and data.get("phase") == "start":
                        tools += 1
                        if tools <= 30:
                            say("tool", str(data.get("tool") or "инструмент"))
                    elif name == "delta" and data.get("kind") == "response":
                        tail = (tail + " " + (data.get("text") or "")).strip()[-600:]
                    elif name == "ask":
                        say("ask", str(data.get("question") or "вопрос агента"))
                    elif name == "done":
                        tail = str(data.get("text") or tail)
                    elif name == "stopped":
                        say("error", "остановлено")
                    elif name == "error":
                        say("error", str(data.get("message") or "ошибка"))
                    name = ""
    except Exception as exc:
        say("error", f"{exc.__class__.__name__}: {exc}"[:300])
        return
    if tail.strip():
        say("done", tail[:600])


def _render_cmd(output) -> str:
    """A CLI command's answer as plain text for the desktop chat."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    try:
        from rich.console import Console

        console = Console(record=True, width=100)
        console.print(output)
        return console.export_text().rstrip() or str(output)
    except Exception:
        return str(output)


def _models() -> dict:
    """Live model list for the active provider, like the terminal's /models.

    Endpoints that can name their own catalogue (the pool asks its box, custom
    OpenAI endpoints answer /models) are asked — no more than once an hour,
    the same cache the terminal keeps. Whatever is stored (file cache, then
    the shipped list) stands in when the box cannot be reached, and `note`
    says so in plain words for the model picker.
    """
    try:
        from beeagent.core import windows
        from beeagent.core.context import advertised_window
    except Exception:
        return {"models": [], "note": ""}
    agent, config = _agent(), _config()
    if agent is None:
        return {"models": [], "note": ""}
    name = getattr(config, "provider", "") or "g4f"
    provider = agent.providers.get(name)
    models = list(getattr(provider, "models", None) or [])
    note = ""
    if name == "g4f":
        if not models:
            try:
                from beeagent.providers.g4f_provider import G4fProvider

                models = G4fProvider.discover_models()
            except Exception:
                models = []
    elif provider is not None and callable(getattr(provider, "discover_models", None)):
        live, note = _live_models(name, provider, models)
        if live:
            models = live
    out = []
    details = {}
    from beeagent.providers.openai_compat import OpenAICompatProvider
    effort_supported = isinstance(provider, OpenAICompatProvider)
    for custom in getattr(config, "custom_providers", []) or []:
        if str(custom.name).lower() == name.lower():
            details = {item["id"]: item for item in getattr(custom, "model_details", []) or []
                       if isinstance(item, dict) and item.get("id")}
            break
    for model in models:
        try:
            size = advertised_window(model)
        except Exception:
            size = 0
        info = details.get(model, {})
        out.append({"id": model, "name": info.get("name") or model,
                    "window": info.get("context") or size,
                    "reasoning": bool(info.get("reasoning")),
                    "efforts": info.get("efforts", []) if info.get("reasoning") else [],
                    "effortSupported": effort_supported,
                    "measured": bool(windows.measured(model))})
    return {"models": out, "note": note, "provider": name}


# An hour-old catalogue is what the terminal serves too: endpoints count
# model lists against per-minute limits, and chat is the only thing that
# leaves without being asked.
_MODELS_CACHE_TTL = 3600.0
_models_cache: dict = {}


def _live_models(name: str, provider, fallback: list) -> tuple:
    """Ask the endpoint, with an hourly cache and a file fallback."""
    now = time.time()
    hit = _models_cache.get(name)
    if hit and now - hit[0] < _MODELS_CACHE_TTL:
        return list(hit[1]), ""
    try:
        found = list(provider.discover_models() or [])
    except Exception as exc:
        cached = _file_models(name) or list(fallback)
        _models_cache[name] = (now, cached)
        return cached, f"живой список недоступен ({exc.__class__.__name__}: {exc}) — показано сохранённое"
    if not found:
        cached = _file_models(name) or list(fallback)
        _models_cache[name] = (now, cached)
        return cached, "пул не назвал моделей — показано сохранённое"
    _models_cache[name] = (now, found)
    return found, ""


def _file_models(name: str) -> list:
    """The catalogue the terminal cached in this workdir, if it is fresh."""
    try:
        from beeagent.core import provider_setup

        return provider_setup.cached_models(name)
    except Exception:
        return []


def _sessions() -> list:
    from beeagent.core.session import Session, read_head

    workdir = _workdir()
    out = []
    try:
        ids = Session.list_sessions(workdir)
    except Exception:
        return []
    for sid in ids:
        try:
            head = read_head(Path(workdir) / ".beeagent" / "sessions" / f"{sid}.json")
        except Exception:
            head = {}
        first = str(head.get("first_user") or head.get("preview") or "")
        out.append({"id": sid,
                    "messages": head.get("message_count", "?"),
                    "age": head.get("saved_at", 0),
                    "preview": first[:80], "title": head.get("title") or ""})
    return out


def _trust_status() -> dict:
    from beeagent.core import trust

    gate = trust.for_folder(_workdir())
    if gate.trusted:
        return {"trusted": True, "claims": []}
    try:
        claims = [str(line) for line in gate.claims()]
    except Exception:
        claims = []
    return {"trusted": False, "claims": claims}


# --- pool status & enrolment (the settings screen's Pool tab) --------------------

# Long enough to survive one cold start of a free instance (measured 22 s
# to wake, platform promises 50 s+), short enough that the settings screen
# does not hang forever against a dead address.
POOL_STATUS_TIMEOUT = 60.0


def _refresh_pool_provider(agent, config) -> None:
    """Hand a fresh address/seat to the already-running pool provider.

    The agent builds its providers once at startup, so a saved token would
    otherwise wait for a restart while the answer claims it works now (the
    same trap `/pool enroll` in the terminal fixed the same way).
    """
    if agent is None or config is None:
        return
    try:
        provider = agent.providers.get("pool")
    except Exception:
        return
    if provider is None:
        return
    try:
        provider.url = (config.pool_url or "").rstrip("/")
        provider.token = config.pool_token or ""
    except Exception:
        pass


def _pool_status() -> dict:
    """Where the pool is, whether this install holds a seat, and if the box answers.

    The seat token is never returned whole — only its tail, like provider keys.
    When the server cannot be reached at all, `server.reachable` is False and
    `server.detail` says why in plain words (asleep box vs wrong address look
    identical on the wire, so the message names both).
    """
    from beeagent.providers import pool as pool_mod

    agent, config = _agent(), _config()
    url = (getattr(config, "pool_url", "") or "").rstrip("/") if config else ""
    token = (getattr(config, "pool_token", "") or "") if config else ""
    out = {"url": url, "hasSeat": bool(token),
           "seatTail": ("…" + token[-4:]) if token else "",
           "server": {"reachable": False, "detail": "адрес пула не задан"},
           "seat": None}
    if not url:
        return out

    _ = agent
    try:
        status = pool_mod.pool_status(url, token, timeout=POOL_STATUS_TIMEOUT)
    except Exception as exc:
        out["server"] = {"reachable": False,
                         "detail": f"пул не ответил ({exc.__class__.__name__}): "
                                   f"{str(exc)[:160]}"}
        return out
    server = status.get("server") or {}
    out["server"] = {"reachable": True,
                     "detail": "отвечает" if status.get("ok", True) else "отвечает с ошибкой",
                     "health": {k: server.get(k) for k in ("ok", "keys", "seats")
                                if k in server}}
    seat = _seat_budget(status.get("seat") or {})
    if seat:
        out["seat"] = {k: seat.get(k) for k in
                       ("approved", "requests_left", "tokens_left",
                        "requests_per_day", "tokens_per_day")
                       if k in seat}
    return out


def _seat_budget(seat: dict) -> dict:
    """Translate the current pool's usage/limit counters for the desktop."""
    out = dict(seat)
    for kind in ("requests", "tokens"):
        limit = seat.get(kind + "_limit")
        used = seat.get(kind)
        if isinstance(limit, (int, float)) and isinstance(used, (int, float)):
            out[kind + "_per_day"] = limit
            out[kind + "_left"] = max(0, limit - used)
    return out


# --- providers listing & editing (the settings screen) ------------------------

def _providers() -> dict:
    """Everything the settings screen needs, with keys masked, never whole.

    Registered endpoints (built-ins, keyed presets, custom ones) carry their
    model lists from the live agent — no network calls here, so the screen
    opens instantly. The `presets` shelf names every free-tier endpoint with
    where its key comes from, whether it is already keyed or answers keyless.
    """
    from beeagent.core import provider_setup
    from beeagent.providers.presets import ENDPOINTS, key_for

    agent, config = _agent(), _config()
    active = (getattr(config, "provider", "") or "g4f").lower() if config else "g4f"
    out = []
    if agent is not None:
        for name in agent.providers.list_names():
            provider = agent.providers.get(name)
            lname = name.lower()
            custom = None
            for entry in getattr(config, "custom_providers", None) or []:
                if str(entry.name).lower() == lname:
                    custom = entry
                    break
            pool = provider_setup.stored_keys(config, name) if config else []
            models = list(getattr(provider, "models", None) or [])
            if not models and config is not None:
                models = provider_setup.cached_models(name)
            out.append({
                "name": name,
                "label": name,
                "kind": "custom" if custom is not None else "builtin",
                "url": getattr(custom, "url", "") or "",
                "hasKey": bool(pool),
                "keyTail": provider_setup.key_tails(pool) if pool else "",
                "models": models,
                "modelDetails": list(getattr(custom, "model_details", []) or []),
                "active": lname == active,
            })
    presets = []
    for endpoint in ENDPOINTS:
        key = key_for(endpoint, getattr(config, "api_keys", None) or {}) if config else ""
        configured = bool(key) or bool(endpoint.keyless and agent is not None
                                       and agent.providers.get(endpoint.name) is not None)
        presets.append({
            "name": endpoint.name,
            "label": endpoint.label,
            "url": endpoint.url,
            "signup": endpoint.signup,
            "free": endpoint.free,
            "env": endpoint.env,
            "keyless": bool(endpoint.keyless),
            "hasKey": bool(key),
            "keyTail": provider_setup.mask_key(key) if key else "",
            "configured": configured,
            "active": endpoint.name.lower() == active,
        })
    return {"active": active, "providers": out, "presets": presets}


def main(port: int = 0, workdir: str = ".", ui: str = "") -> int:
    """Boot the agent and serve it. Prints `BEECODE_SERVE port=N` for the parent."""
    from beeagent import __version__
    from beeagent.config.loader import load_config
    from beeagent.core.agent import Agent

    print("BEECODE_SERVE booting agent...", flush=True)
    _state["workdir"] = workdir
    _state["ui"] = ui
    config = load_config(workdir)
    _state["config"] = config
    _state["agent"] = Agent(config=config, workdir=workdir)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    actual = server.server_address[1]
    _state["port"] = actual
    print(f"BEECODE_SERVE port={actual} version={__version__}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="beeagent.serve")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--ui", default="")
    args = parser.parse_args()
    raise SystemExit(main(port=args.port, workdir=args.workdir, ui=args.ui))
