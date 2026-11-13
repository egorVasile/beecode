"""`beecode serve` answers the desktop UI: health, models, sessions, trust.

The chat turn itself runs the real agent loop and needs a model, so it is
covered by walking the validation doors (bad bodies, unknown sessions, busy)
and by the permission/trust/config state the UI reads. Nothing here reaches
the network.
"""
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from beeagent import serve
from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.session import Session


@pytest.fixture()
def serving(tmp_path, monkeypatch):
    """The real HTTP server on a random port, agent pointed at a tmp workdir."""
    monkeypatch.chdir(tmp_path)
    config = BeeConfig()
    serve._state["agent"] = Agent(config=config, workdir=str(tmp_path))
    serve._state["config"] = config
    serve._state["workdir"] = str(tmp_path)
    serve._state["ui"] = ""
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _read(response) -> dict:
    try:
        return json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}


def _get(base, path):
    import urllib.request

    try:
        with urllib.request.urlopen(base + path, timeout=30) as response:
            return response.status, _read(response)
    except Exception as e:
        return getattr(e, "code", 0), _read(e)


def _post(base, path, body):
    import urllib.request

    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, _read(response)
    except Exception as e:
        return getattr(e, "code", 0), _read(e)


def test_health_names_the_version(serving):
    status, body = _get(serving, "/api/health")
    assert status == 200 and body["ok"] is True
    assert body["version"]


def test_cross_origin_cannot_grant_tools(serving):
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        serving + "/api/allow", data=b'{"tool":"bash"}', method="POST",
        headers={"Origin": "https://attacker.invalid", "Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 403
    assert "bash" not in serve._state["agent"].permissions.granted


def test_config_save_failure_is_reported(serving, monkeypatch):
    from beeagent.config import loader

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(loader, "save_config", fail)
    status, body = _post(serving, "/api/config", {"model": "test-model"})
    assert status == 500
    assert "could not be saved" in body["error"]


def test_whoami_is_a_string(serving):
    status, body = _get(serving, "/api/whoami")
    assert status == 200 and isinstance(body["name"], str)


def test_models_carry_windows(serving):
    status, body = _get(serving, "/api/models")
    assert status == 200 and body["models"]
    assert all("id" in m and "window" in m for m in body["models"])


def test_models_live_discover_and_fallback(serving):
    from beeagent import serve

    agent = serve._state["agent"]
    config = serve._state["config"]
    old_provider = config.provider

    class FakePool:
        name = "pool"
        models = ["shipped-a", "shipped-b", "shipped-c"]
        calls = 0

        def discover_models(self):
            type(self).calls += 1
            return ["live-1", "live-2", "live-3", "live-4", "live-5"]

    fake = FakePool()
    agent.providers.register(fake, replace=True)
    config.provider = "pool"
    try:
        serve._models_cache.clear()
        status, body = _get(serving, "/api/models")
        assert status == 200
        assert [m["id"] for m in body["models"]] == [
            "live-1", "live-2", "live-3", "live-4", "live-5"]
        assert not body["note"]
        # Second read comes from the hourly cache — no second network call.
        status, body = _get(serving, "/api/models")
        assert status == 200 and len(body["models"]) == 5
        assert FakePool.calls == 1

        def boom():
            raise RuntimeError("asleep")

        fake.discover_models = boom
        serve._models_cache.clear()
        status, body = _get(serving, "/api/models")
        assert status == 200
        assert [m["id"] for m in body["models"]] == [
            "shipped-a", "shipped-b", "shipped-c"]
        assert "недоступен" in body["note"]
        assert "sk-" not in body["note"]
    finally:
        config.provider = old_provider
        agent.providers.unregister("pool")
        serve._models_cache.clear()


def test_sessions_round_trip(serving):
    status, created = _post(serving, "/api/sessions/new", {})
    assert status == 200 and created["id"]
    status, listed = _get(serving, "/api/sessions")
    assert status == 200
    assert any(s["id"] == created["id"] for s in listed["sessions"])
    status, missing = _get(serving, "/api/session?id=nope")
    assert status == 404


def test_chat_refuses_garbage_before_touching_a_model(serving):
    status, _ = _post(serving, "/api/chat", {})
    assert status == 400
    status, _ = _post(serving, "/api/chat", {"message": "hi",
                                             "session_id": "nope"})
    assert status == 404


def test_config_reads_and_writes_without_secrets(serving):
    status, body = _get(serving, "/api/config")
    assert status == 200
    assert body["model"] and body["provider"] and body["mode"]
    assert "api_keys" not in body and "pool_token" not in body
    for key in ("language", "nativeTools", "maxTurns", "maxContextTokens",
                "streamIdleTimeout", "cacheEnabled", "cacheTtlMinutes",
                "customInstructions"):
        assert key in body, key
    status, body = _post(serving, "/api/config",
                         {"permissions": "readonly"})
    assert status == 200 and body["changed"] == ["permissions"]
    assert body["permissions"] == "readonly"
    status, body = _post(serving, "/api/config", {"permissions": "ask"})
    assert body["permissions"] == "ask"


def test_config_writes_agent_options(serving):
    status, body = _post(serving, "/api/config", {
        "mode": "economy", "language": "ru", "nativeTools": False,
        "maxTurns": 25, "maxContextTokens": 60000,
        "streamIdleTimeout": 60, "cacheEnabled": True,
        "cacheTtlMinutes": 45})
    assert status == 200
    for key in ("mode", "language", "nativeTools", "maxTurns",
                "maxContextTokens", "streamIdleTimeout", "cacheEnabled",
                "cacheTtlMinutes"):
        assert key in body["changed"], key
    assert body["mode"] == "economy" and body["language"] == "ru"
    assert body["maxTurns"] == 25 and body["cacheTtlMinutes"] == 45
    status, body = _get(serving, "/api/config")
    assert status == 200
    assert body["mode"] == "economy" and body["language"] == "ru"
    assert body["nativeTools"] is False and body["maxTurns"] == 25
    status, _ = _post(serving, "/api/config", {"maxTurns": "много"})
    assert status == 400
    status, _ = _post(serving, "/api/config", {"maxTurns": 0})
    assert status == 400
    status, _ = _post(serving, "/api/config", {"streamIdleTimeout": 5})
    assert status == 400


def test_custom_instructions_round_trip(serving):
    status, body = _post(serving, "/api/config",
                         {"customInstructions": "Отвечай кратко."})
    assert status == 200 and "customInstructions" in body["changed"]
    status, body = _get(serving, "/api/config")
    assert status == 200
    assert body["customInstructions"] == "Отвечай кратко."
    status, body = _post(serving, "/api/config", {"customInstructions": ""})
    assert status == 200
    status, body = _get(serving, "/api/config")
    assert body["customInstructions"] == ""
    status, _ = _post(serving, "/api/config",
                      {"customInstructions": "x" * 8001})
    assert status == 400
    status, body = _post(serving, "/api/config",
                          {"mode": "normal", "language": "en"})
    assert status == 200 and body["mode"] == "normal"


def test_ui_prefs_round_trip(serving, tmp_path):
    from beeagent.tools.ui import write_prefs
    status, body = _get(serving, "/api/ui")
    assert status == 200
    assert body["skin"] is None and body["version"] == 0
    assert body["customCss"] is False
    write_prefs({"skin": "paper", "accent": "#00ff00"})
    status, body = _get(serving, "/api/ui")
    assert status == 200
    assert body["skin"] == "paper" and body["accent"] == "#00ff00"
    assert body["version"] == 1 and body["updated_at"] > 0
    status, _ = _get(serving, "/api/ui-css")
    assert status == 404, "no stylesheet yet"
    (tmp_path / ".beeagent" / "ui").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".beeagent" / "ui" / "custom.css").write_text(
        "#chat { color: red; }", encoding="utf-8")
    status, body = _get(serving, "/api/ui")
    assert body["customCss"] is True
    import urllib.request
    with urllib.request.urlopen(serving + "/api/ui-css", timeout=30) as r:
        assert r.status == 200
        assert r.headers.get_content_type() == "text/css"
        assert b"#chat" in r.read()


def test_allow_grants_a_tool(serving):
    status, body = _post(serving, "/api/allow", {"tool": "bash"})
    assert status == 200 and body["allowed"] == "bash"
    status, _ = _post(serving, "/api/allow", {})
    assert status == 400


def test_decide_without_a_question_is_404(serving):
    status, _ = _post(serving, "/api/decide", {"id": "ask-0", "answer": "x"})
    assert status == 404


def test_unknown_paths_are_json(serving):
    status, body = _get(serving, "/nope")
    assert status == 404 and body["error"] == "unknown path"


def test_session_history_round_trip(serving, tmp_path):
    session = Session()
    session.add_user_message("hello")
    session.add_assistant_message("hi")
    session.save(str(tmp_path))
    status, body = _get(serving, f"/api/session?id={session.session_id}")
    assert status == 200
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant"]


def test_providers_list_masks_keys(serving):
    status, body = _get(serving, "/api/providers")
    assert status == 200
    assert body["active"]
    assert body["providers"], "the keyless built-ins must be listed"
    assert body["presets"], "the free-tier shelf must be listed"
    dump = json.dumps(body)
    assert "sk-secret" not in dump
    for preset in body["presets"]:
        assert preset["name"] and preset["signup"] and preset["free"]


def test_providers_use_switches_builtin(serving):
    status, body = _post(serving, "/api/providers",
                         {"action": "use", "name": "g4f"})
    assert status == 200 and body["ok"] is True
    assert body["provider"] == "g4f" and body["model"]
    status, body = _post(serving, "/api/providers",
                         {"action": "use", "name": "nope"})
    assert status == 404


def test_providers_use_needs_key_for_preset(serving):
    status, body = _post(serving, "/api/providers",
                         {"action": "use", "name": "groq"})
    assert status == 400
    assert body.get("needsKey") is True and "signup" in body


def test_providers_save_and_remove_custom(serving):
    status, body = _post(serving, "/api/providers", {
        "action": "save", "name": "mybox",
        "url": "https://api.example.com/v1",
        "keys": "sk-secret-1234", "models": "my-model-a, my-model-b"})
    assert status == 200 and body["ok"] is True
    names = [p["name"] for p in body["providers"]["providers"]]
    assert "mybox" in names
    entry = next(p for p in body["providers"]["providers"]
                 if p["name"] == "mybox")
    assert entry["hasKey"] is True
    assert "sk-secret-1234" not in json.dumps(body)
    assert "1234" in entry["keyTail"]
    status, switched = _post(serving, "/api/providers",
                             {"action": "use", "name": "mybox"})
    assert status == 200 and switched["model"] == "my-model-a"
    status, body = _post(serving, "/api/providers",
                         {"action": "remove", "name": "mybox"})
    assert status == 200 and body["ok"] is True
    status, body = _post(serving, "/api/providers",
                         {"action": "use", "name": "mybox"})
    assert status == 404


def test_providers_refuses_garbage(serving):
    status, _ = _post(serving, "/api/providers", {"action": "save"})
    assert status == 400
    status, _ = _post(serving, "/api/providers",
                      {"action": "save", "name": "add",
                       "url": "https://api.example.com/v1",
                       "models": "m"})
    assert status == 400
    status, _ = _post(serving, "/api/providers",
                      {"action": "save", "name": "badbox",
                       "url": "not-a-url", "models": "m"})
    assert status == 400
    status, _ = _post(serving, "/api/providers",
                      {"action": "remove", "name": "g4f"})
    assert status == 400
    status, _ = _post(serving, "/api/providers", {"action": "dance"})
    assert status == 400


def test_pool_status_without_seat(serving, monkeypatch):
    import beeagent.providers.pool as pool_mod

    seen = {}

    def fake_status(url, token, timeout=None):
        seen["url"] = url
        return {"ok": True, "server": {"ok": True},
                "seat": {"approved": True, "requests_left": 20,
                         "tokens_left": 50000}}

    monkeypatch.setattr(pool_mod, "pool_status", fake_status)
    status, body = _get(serving, "/api/pool")
    assert status == 200
    assert body["url"] and body["hasSeat"] is False
    assert body["server"]["reachable"] is True
    assert seen["url"] == body["url"]


def test_pool_status_unreachable(serving, monkeypatch):
    import beeagent.providers.pool as pool_mod

    def boom(url, token, timeout=None):
        raise RuntimeError("asleep")

    monkeypatch.setattr(pool_mod, "pool_status", boom)
    status, body = _get(serving, "/api/pool")
    assert status == 200
    assert body["server"]["reachable"] is False
    assert body["seat"] is None


def test_pool_enroll_round_trip(serving, monkeypatch):
    import beeagent.providers.pool as pool_mod

    def fake_enroll(url, secret=""):
        assert url.startswith("http")
        return {"id": "seat-abc-1234", "requests_per_day": 50,
                "tokens_per_day": 100000}

    monkeypatch.setattr(pool_mod, "enroll", fake_enroll)
    monkeypatch.setattr(pool_mod, "pool_status",
                        lambda url, token, timeout=None: {
                            "ok": True, "server": {"ok": True}, "seat": {}})
    status, body = _post(serving, "/api/pool", {"action": "enroll"})
    assert status == 200 and body["ok"] is True
    assert "seat-abc-1234" not in json.dumps(body)
    status, body = _get(serving, "/api/pool")
    assert body["hasSeat"] is True
    assert body["seatTail"] == "…1234"


def test_pool_enroll_failure_is_400(serving, monkeypatch):
    import beeagent.providers.pool as pool_mod

    def refuse(url, secret=""):
        raise pool_mod.PoolError("the pool answered 503")

    monkeypatch.setattr(pool_mod, "enroll", refuse)
    status, body = _post(serving, "/api/pool", {"action": "enroll"})
    assert status == 400 and "503" in body["error"]


def test_pool_url_validation(serving):
    status, _ = _post(serving, "/api/pool",
                      {"action": "url", "url": "not-a-url"})
    assert status == 400
    status, body = _post(serving, "/api/pool",
                         {"action": "url",
                          "url": "https://pool.example.com"})
    assert status == 200 and body["url"] == "https://pool.example.com"
    status, _ = _post(serving, "/api/pool", {"action": "dance"})
    assert status == 400


def _cmd(base, command):
    return _post(base, "/api/cmd", {"command": command})


def test_cmd_help_and_config(serving):
    status, body = _cmd(serving, "/help")
    assert status == 200 and body["ok"] is True
    assert "pool" in body["output"]
    status, body = _cmd(serving, "/config")
    assert status == 200 and "provider" in body["output"]


def test_cmd_rejects_non_commands(serving):
    status, _ = _cmd(serving, "just talking")
    assert status == 400
    status, _ = _cmd(serving, "/nope-not-a-command")
    assert status == 400
    status, _ = _cmd(serving, "/")
    assert status == 400


def test_cmd_redirects_terminal_only(serving):
    status, body = _cmd(serving, "/quit")
    assert status == 200 and body["output"]
    status, body = _cmd(serving, "/run echo hi")
    assert status == 200 and "чат" in body["output"]


def test_cmd_lang_and_allow(serving):
    from beeagent.i18n import get_lang, set_lang

    before = get_lang()
    try:
        status, body = _cmd(serving, "/lang ru")
        assert status == 200
        assert "ru" in body["output"]
        status, body = _cmd(serving, "/allow bash")
        assert status == 200 and "bash" in body["output"]
    finally:
        set_lang(before)


def test_cmd_key_and_pool_status(serving, monkeypatch):
    import beeagent.providers.pool as pool_mod

    status, body = _cmd(serving, "/key groq gsk-test-abc-9999")
    assert status == 200
    assert "9999" not in body["output"] or "…" in body["output"]
    assert "gsk-test-abc-9999" not in body["output"]
    monkeypatch.setattr(pool_mod, "pool_status",
                        lambda url, token, timeout=None: {
                            "ok": True, "server": {"ok": True}, "seat": {}})
    status, body = _cmd(serving, "/pool status")
    assert status == 200 and body["output"]
