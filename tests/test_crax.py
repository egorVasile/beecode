"""crax-gpt answers 429 in two meanings; the provider must not mix them up.

A fake endpoint on localhost speaks the documented shape — OpenAI error envelope,
`Retry-After`, `daily_limit_exceeded` — so these tests fail if the provider ever
starts treating "another key" as a cure for an IP limit.
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from beeagent.providers.crax import CraxError, CraxProvider, keys_from


class Fake:
    """A stand-in for gpt.crax.lol that replays a script and remembers requests.

    A script entry is (status, body, headers) for JSON, or
    (status, [chunk, ...], headers, True) for a streamed answer.
    """

    def __init__(self, script, catalogue=None):
        self.script = list(script)
        self.catalogue = catalogue or {}
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append({"path": self.path})
                self._reply(outer.catalogue)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"body": body, "auth": self.headers.get("Authorization")})
                status, payload, headers, streaming = (outer.script.pop(0) if outer.script
                                                       else (200, {"choices": [{"message":
                                                              {"content": "жужж"}}]}, {}, False))
                if streaming:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for piece in payload:
                        self.wfile.write(("data: " + json.dumps(piece) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def make_endpoint(request):
    def make(script, catalogue=None):
        endpoint = Fake(script, catalogue)
        request.addfinalizer(endpoint.close)
        return endpoint

    return make


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """The tools-support cache lives under the working directory — keep the
    suite from filing verdicts into the checkout it runs from."""
    monkeypatch.chdir(tmp_path)


def test_several_keys_are_written_as_one_config_value():
    assert keys_from("a, b,,c") == ["a", "b", "c"]
    assert keys_from("") == []


def test_a_daily_allowance_moves_to_the_next_account(make_endpoint):
    endpoint = make_endpoint([
        (429, {"error": {"message": "daily_limit_exceeded", "type": "daily_limit_exceeded"}},
         {"Retry-After": "3600"}, False),
        (200, {"choices": [{"message": {"content": "со второго ключа"}}]}, {}, False),
    ])
    provider = CraxProvider(api_key="crk_live_one,crk_live_two", base_url=endpoint.url)

    answer = run(provider.chat([{"role": "user", "content": "привет"}], "qwen3.8-max"))

    assert answer == "со второго ключа"
    assert [r["auth"] for r in endpoint.requests] == ["Bearer crk_live_one", "Bearer crk_live_two"]
    assert provider._usable() == [(1, "crk_live_two")], "the spent account is put aside"


def test_an_ip_limit_asks_the_user_and_another_key_is_not_tried(make_endpoint):
    asked = []

    async def ask(error):
        asked.append(error)
        return "wait"

    endpoint = make_endpoint([
        (429, {"error": {"message": "Too many requests", "type": "rate_limit_exceeded"}},
         {"Retry-After": "1"}, False),
        (200, {"choices": [{"message": {"content": "со того же ключа"}}]}, {}, False),
    ])
    provider = CraxProvider(api_key="crk_live_one,crk_live_two", base_url=endpoint.url, ask=ask)

    answer = run(provider.chat([{"role": "user", "content": "привет"}], "m"))

    assert answer == "со того же ключа"
    assert asked and asked[0].kind == "ip" and asked[0].retry_after == 1
    assert [r["auth"] for r in endpoint.requests] == ["Bearer crk_live_one"] * 2, \
        "a per-IP limit is not cured by another account"


def test_declining_the_wait_ends_the_attempt(make_endpoint):
    async def ask(error):
        return None

    endpoint = make_endpoint([
        (429, {"error": {"type": "rate_limit_exceeded"}}, {"Retry-After": "5"}, False)])
    provider = CraxProvider(api_key="crk_live_one", base_url=endpoint.url, ask=ask)

    with pytest.raises(CraxError) as raised:
        run(provider.chat([{"role": "user", "content": "х"}], "m"))
    assert raised.value.kind == "ip"


def test_a_rejected_key_says_where_to_put_the_key(make_endpoint):
    endpoint = make_endpoint([
        (401, {"error": {"message": "Authentication required", "type": "auth_required"}},
         {}, False)])
    provider = CraxProvider(api_key="crk_live_wrong", base_url=endpoint.url)

    with pytest.raises(CraxError) as raised:
        run(provider.chat([{"role": "user", "content": "х"}], "m"))
    assert raised.value.kind == "auth"
    assert "/key crax" in str(raised.value)


def test_no_key_at_all_is_not_reported_as_a_network_problem():
    provider = CraxProvider(api_key="", base_url="http://127.0.0.1:1/v1")
    with pytest.raises(CraxError) as raised:
        run(provider.chat([{"role": "user", "content": "х"}], "m"))
    assert raised.value.kind == "auth"


def test_streaming_keeps_answer_and_reasoning_apart(make_endpoint):
    thinking = {"choices": [{"delta": {"reasoning_content": "думаю"}}]}
    saying = {"choices": [{"delta": {"content": "раз"}}]}
    endpoint = make_endpoint([(200, [thinking, saying], {}, True)])
    provider = CraxProvider(api_key="crk_live_one", base_url=endpoint.url)

    async def collect():
        return [pair async for pair in provider.chat_stream([{"role": "user", "content": "х"}], "m")]

    assert run(collect()) == [("reasoning", "думаю"), ("content", "раз")]
    assert endpoint.requests[0]["body"]["include_reasoning"] is True
    assert endpoint.requests[0]["body"]["stream"] is True


def test_a_generation_model_is_refused_before_the_request_leaves(make_endpoint):
    """The keys also buy image, video and audio. Those are the requests that get
    an account reported, so nothing goes out at all — not even a cheap 400."""
    endpoint = make_endpoint([])
    provider = CraxProvider(api_key="crk_live_one", base_url=endpoint.url)

    for model in ("seedream-5", "qwen-image-2.0-pro", "whisper-1"):
        with pytest.raises(CraxError) as raised:
            run(provider.chat([{"role": "user", "content": "х"}], model))
        assert "chat" in str(raised.value).lower() or "чат" in str(raised.value)
        with pytest.raises(CraxError):
            run(provider.complete([{"role": "user", "content": "х"}], model))
    assert endpoint.requests == [], "a refused model must not cost a request"


def test_the_catalogue_shows_what_this_interface_can_answer(make_endpoint):
    """Filtering the list is what keeps the refusal out of the user's way: a model
    never offered is a model never picked."""
    endpoint = make_endpoint([], {"data": [{"id": "qwen3.8-max"}, {"id": "seedream-5"},
                                           {"id": "gpt-5-6-luna"}, {"id": "text-embedding-3-small"}]})
    provider = CraxProvider(api_key="crk_live_one", base_url=endpoint.url)

    assert provider.discover_models() == ["qwen3.8-max", "gpt-5-6-luna"]


def test_pacing_serializes_bursts_process_wide(monkeypatch):
    """40/min/IP is one start every 1.5 s: immediate asks leave ordered,
    spaced stamps — across instances, since the limit counts the IP."""
    import time

    from beeagent.providers import crax as crax_mod

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("BEECODE_CRAX_MIN_INTERVAL", raising=False)
    crax_mod._next_allowed_at = 0.0
    try:
        assert crax_mod._reserve_slot() == 0.0
        second = crax_mod._reserve_slot()
        assert 1.0 <= second <= 2.0, second
    finally:
        crax_mod._next_allowed_at = 0.0


def test_the_default_model_is_one_that_answered_the_last_time_we_measured():
    """A default that does not answer is the first thing a new person meets, and it
    reads as a broken install rather than a broken model.

    The qwen3.x max/plus family hung ~15 s and 502ed on 2026-09-23; a year of
    measurement says the shape of the failure, not which names survive it — on
    2026-09-26 the endpoint dropped twelve of its thirteen ids, `qwen3-coder-480b`
    among them, and kept `glm-5.3`, `glm-5.3-flash`, `glm-5.2`. So the list is
    checked against the dead names rather than reciting the living ones: a test
    that repeats yesterday's catalogue fails on the day the catalogue is fixed.
    """
    dead = ("qwen3.8-max", "qwen3.7-max", "qwen3.7-plus", "qwen3.6-plus",
            "qwen3.5-plus", "qwen3.5-omni-plus",          # 502 on 2026-09-23
            "qwen3-coder-480b", "gemma-3-12b", "llama-4-maverick", "gpt-5-6-luna",
            "kimi-k2-7-code", "grok-4-3",
            "deepseek-v4-flash", "grok-code-fast-1")      # `Unknown model` 2026-09-26
    # `kimi-k2-6` and `grok-4-6` were on this list and answer again as of
    # 2026-09-30 (one short probe each) — the catalogue is rewritten without
    # a release, in both directions.
    assert CraxProvider.models, "an endpoint with no chat model is not a default"
    assert not set(CraxProvider.models) & set(dead), \
        "a model that does not answer belongs in neither the default nor the picker"
    # what the picker offers is what was measured, in the order it was measured
    # (2026-09-30: all 17 live ids, one short chat probe each)
    assert list(CraxProvider.models) == [
        "glm-5.3", "glm-5.3-flash", "glm-5.2", "instant",
        "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5",
        "claude-fable-5-1", "gpt-5-6-sol", "gpt-5-5", "gpt-5-4",
        "gpt-6-astra", "gemini-3-8-flash", "deepseek-v4-pro",
        "kimi-k3", "kimi-k2-6", "grok-4-6"], \
        list(CraxProvider.models)


# --- native tools dropped upstream: the JSON fallback -------------------------

TOOLS = [{"name": "read", "description": "Open a file.",
          "parameters": {"type": "object",
                         "properties": {"path": {"type": "string"}}}}]
MSGS = [{"role": "user", "content": "открой note.txt"}]


def _cache_of(tmp_path):
    import json as _json
    from pathlib import Path as _Path
    path = _Path(tmp_path) / ".beeagent" / "crax_tools.json"
    return _json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_native_tool_calls_come_back_untouched(make_endpoint, tmp_path):
    """The happy path pays nothing extra: one request, no cache file."""
    endpoint = make_endpoint([
        (200, {"choices": [{"message": {
            "content": "",
            "tool_calls": [{"id": "1", "type": "function",
                            "function": {"name": "read",
                                         "arguments": '{"path": "note.txt"}'}}]}}]},
         {}, False),
    ])
    provider = CraxProvider(api_key="k", base_url=endpoint.url)

    out = run(provider.complete(MSGS, "glm-5.3-flash", TOOLS))

    assert out["tool_calls"] == [{"tool": "read", "args": {"path": "note.txt"}}]
    assert len(endpoint.requests) == 1
    assert "tools" in endpoint.requests[0]["body"]
    assert _cache_of(tmp_path) == {}


def test_a_refusal_falls_back_to_the_json_protocol(make_endpoint, tmp_path):
    """200 OK carrying 'no access' is a dropped `tools` field, not an answer:
    re-ask in prose, parse the block, file the verdict about this model id."""
    endpoint = make_endpoint([
        (200, {"choices": [{"message":
                            {"content": "У меня нет доступа к вашим файлам."}}]},
         {}, False),
        (200, {"choices": [{"message": {"content":
            '```json\n{"tool": "read", "args": {"path": "note.txt"}}\n```' }}]},
         {}, False),
    ])
    provider = CraxProvider(api_key="k", base_url=endpoint.url)

    out = run(provider.complete(MSGS, "gpt-5-5", TOOLS))

    assert out["tool_calls"] == [{"tool": "read", "args": {"path": "note.txt"}}]
    assert len(endpoint.requests) == 2
    assert "tools" in endpoint.requests[0]["body"], "first: native, as always"
    retry = endpoint.requests[1]["body"]
    assert "tools" not in retry, "retry: prose, or it fails the same way"
    assert "Respond with EXACTLY one JSON code block" in retry["messages"][-1]["content"]
    assert "gpt-5-5" in _cache_of(tmp_path), "the verdict is filed"


def test_a_session_denial_falls_back_to_the_json_protocol(make_endpoint, tmp_path):
    """Seen live 2026-10-07: 'в этой сессии у меня нет доступа к файловой
    системе и терминалу, подключены только веб-поиск и генерация картинок'.
    Same denial, new words — the prose retry must still happen."""
    endpoint = make_endpoint([
        (200, {"choices": [{"message":
                            {"content": "В этой сессии у меня нет доступа к файловой "
                                        "системе и терминалу, подключены только "
                                        "веб-поиск и генерация картинок."}}]},
         {}, False),
        (200, {"choices": [{"message": {"content":
            '```json\n{"tool": "read", "args": {"path": "note.txt"}}\n```' }}]},
         {}, False),
    ])
    provider = CraxProvider(api_key="k", base_url=endpoint.url)

    out = run(provider.complete(MSGS, "gpt-5-5", TOOLS))

    assert out["tool_calls"] == [{"tool": "read", "args": {"path": "note.txt"}}]
    assert len(endpoint.requests) == 2
    assert "tools" not in endpoint.requests[1]["body"]


def test_a_cached_model_skips_the_wasted_native_request(make_endpoint, tmp_path):
    """Second session with the same dead route: straight to prose, one request."""
    import json as _json
    import time as _time
    from pathlib import Path as _Path
    cache = _Path(tmp_path) / ".beeagent"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "crax_tools.json").write_text(
        _json.dumps({"gpt-5-5": {"at": _time.time()}}), encoding="utf-8")
    endpoint = make_endpoint([
        (200, {"choices": [{"message": {"content":
            '{"tool": "read", "args": {"path": "a.txt"}}' }}]}, {}, False),
    ])
    provider = CraxProvider(api_key="k", base_url=endpoint.url)

    out = run(provider.complete(MSGS, "gpt-5-5", TOOLS))

    assert out["tool_calls"] == [{"tool": "read", "args": {"path": "a.txt"}}]
    assert len(endpoint.requests) == 1
    assert "tools" not in endpoint.requests[0]["body"]


def test_a_fallback_that_finds_nothing_returns_the_latest_answer(make_endpoint):
    """Prose that also refuses is still the model's latest word: hand it back,
    never an empty reply, never a crash."""
    endpoint = make_endpoint([
        (200, {"choices": [{"message": {"content": "I have no access to data."}}]},
         {}, False),
        (200, {"choices": [{"message": {"content": "Still cannot access files, sorry."}}]},
         {}, False),
    ])
    provider = CraxProvider(api_key="k", base_url=endpoint.url)

    out = run(provider.complete(MSGS, "gpt-5-5", TOOLS))

    assert out["tool_calls"] == []
    assert "Still cannot" in out["text"]
    assert len(endpoint.requests) == 2


def test_no_tools_means_no_fallback_even_on_denial_text(make_endpoint):
    """The fallback repairs tool turns only: a plain answer that happens to say
    'attached the log' must not cost a second request."""
    endpoint = make_endpoint([
        (200, {"choices": [{"message": {"content": "attached the log below, all green"}}]},
         {}, False),
    ])
    provider = CraxProvider(api_key="k", base_url=endpoint.url)

    out = run(provider.complete(MSGS, "gpt-5-5"))

    assert out["tool_calls"] == []
    assert len(endpoint.requests) == 1


def test_every_key_failing_names_the_model_not_the_keys(make_endpoint):
    """A dead model route fails on every key while the accounts are fine —
    'refused every key' used to send the user retyping keys that were never it."""
    async def ask(error):
        return "wait"

    endpoint = make_endpoint([
        (429, {"error": {"message": "Too many requests", "type": "rate_limit_exceeded"}},
         {"Retry-After": "0"}, False),
        (429, {"error": {"message": "Too many requests", "type": "rate_limit_exceeded"}},
         {"Retry-After": "0"}, False),
    ])
    provider = CraxProvider(api_key="k1,k2", base_url=endpoint.url, ask=ask)

    with pytest.raises(CraxError) as raised:
        run(provider.complete(MSGS, "gpt-5-5", TOOLS))

    assert "gpt-5-5" in str(raised.value)
    assert "/models" in str(raised.value)
