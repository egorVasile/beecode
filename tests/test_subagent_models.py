import json
from types import SimpleNamespace

from beeagent.config.schema import BeeConfig, CustomProvider
from beeagent.core.context import ContextManager
from beeagent.core.provider_setup import Fields, apply, sync_context
from beeagent.tools.read import ReadTool
from beeagent.tools.bash import BashTool
from beeagent.tools.registry import ToolRegistry
from beeagent.tools.subagent import SubagentTool


def test_custom_model_metadata_roundtrips_and_sets_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = BeeConfig()
    fields = Fields(name='custom', url='https://example.com/v1',
                    models=['model-a', 'model-b'], model_details=[
                        {'id': 'model-a', 'name': 'Alpha', 'context': 128000, 'reasoning': True},
                        {'id': 'model-b', 'name': 'Beta', 'context': 32000, 'reasoning': False}])
    _, error = apply(config, None, fields, workdir=str(tmp_path))
    assert not error
    from beeagent.config.loader import load_config
    restored = load_config(str(tmp_path))
    assert restored.custom_providers[0].model_details == fields.model_details
    restored.provider, restored.model = 'custom', 'model-b'
    agent = SimpleNamespace(config=restored, context=ContextManager())
    sync_context(agent)
    assert agent.context.model_window == 32000
    assert agent.context.window == 32000


def test_provider_api_preserves_keys_and_model_metadata(tmp_path, monkeypatch):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from beeagent import serve
    from beeagent.core.agent import Agent

    monkeypatch.chdir(tmp_path)
    config = BeeConfig()
    agent = Agent(config=config, workdir=str(tmp_path))
    monkeypatch.setitem(serve._state, 'agent', agent)
    monkeypatch.setitem(serve._state, 'config', config)
    monkeypatch.setitem(serve._state, 'workdir', str(tmp_path))
    server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_address[1]}/api/providers'

    def send(payload):
        req = urllib.request.Request(url, json.dumps(payload).encode(),
                                     {'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)

    try:
        details = [{'id': 'alpha', 'name': 'Alpha', 'context': 128000, 'reasoning': True}]
        payload = {'action': 'save', 'name': 'local', 'url': 'https://example.com/v1',
                   'keys': 'private-test-key', 'models': ['alpha'], 'modelDetails': details}
        first = send(payload)
        assert 'private-test-key' not in json.dumps(first)
        payload.update(keys='', keepKeys=True)
        second = send(payload)
        provider = next(item for item in second['providers']['providers'] if item['name'] == 'local')
        assert provider['hasKey'] and provider['modelDetails'] == details
        assert config.api_keys['local'] == 'private-test-key'
    finally:
        server.shutdown()
        server.server_close()


class FakeAgent:
    def __init__(self):
        self.config = BeeConfig(max_turns=50)
        self.context = ContextManager()
        self.tools = ToolRegistry()
        self.tools.register(ReadTool())
        self.tools.register(BashTool())
        self.economy = SimpleNamespace()

    def run_sync(self, prompt, session, callback=None):
        assert self.tools.get('read') is not None
        assert self.tools.get('bash') is None
        assert self.tools.get('subagent') is None
        assert self.permissions.mode == 'readonly'
        assert self.config.max_turns == 12
        assert session.messages == []
        return 'result: ' + prompt


def test_subagent_runs_with_read_only_tools_and_separate_session():
    parent = FakeAgent()
    manager = SubagentTool(parent)
    started = manager.execute('start', task='inspect code')
    assert not started.error
    child_id = json.loads(started.output)['id']
    finished = manager.execute('wait', id=child_id)
    assert not finished.error
    data = json.loads(finished.output)
    assert data['status'] == 'done'
    assert data['result'] == 'result: inspect code'
    assert parent.tools.get('bash') is not None
    assert parent.config.max_turns == 50


def test_subagent_concurrency_is_bounded():
    import threading

    gate = threading.Event()
    class BlockedAgent(FakeAgent):
        def run_sync(self, prompt, session, callback=None):
            gate.wait(5)
            return 'finished'

    manager = SubagentTool(BlockedAgent())
    ids = [json.loads(manager.execute('start', task='read').output)['id'] for _ in range(3)]
    assert manager.execute('start', task='fourth').error
    gate.set()
    for child_id in ids:
        assert json.loads(manager.execute('wait', id=child_id).output)['status'] == 'done'


def test_real_agent_child_reads_file_through_normal_agent_loop(tmp_path, monkeypatch):
    from beeagent.core.agent import Agent
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'sample.txt').write_text('child can see this', encoding='utf-8')

    class Provider:
        name = 'fake'
        models = ['model']
        supports_tools = False

        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, model=''):
            self.calls += 1
            if self.calls == 1:
                yield ('content', '```json\n{"tool":"read","args":{"path":"sample.txt"}}\n```')
            else:
                assert any('child can see this' in str(row.get('content')) for row in messages)
                yield ('content', 'The file contains the expected child-visible text.')

    parent = Agent(config=BeeConfig(provider='fake', model='model', native_tools=False),
                   workdir=str(tmp_path))
    provider = Provider()
    parent.providers.register(provider)
    manager = parent.tools.get('subagent')
    started = manager.execute('start', task='Read sample.txt and report its contents.')
    result = manager.execute('wait', id=json.loads(started.output)['id'])
    answer = json.loads(result.output)
    assert answer['status'] == 'done', answer
    assert 'expected child-visible text' in answer['result']
    assert provider.calls == 2


def test_parent_waits_for_child_and_uses_result(tmp_path, monkeypatch):
    import threading
    import time
    from beeagent.core.agent import Agent
    from beeagent.core.session import Session

    monkeypatch.chdir(tmp_path)
    gate = threading.Event()
    child_started = threading.Event()
    class Provider:
        name = 'fake'
        models = ['model']
        supports_tools = False
        async def chat_stream(self, messages, model=''):
            content = str(messages)
            if 'child research task' in content and 'Subagent results, supplied' not in content:
                child_started.set()
                gate.wait(8)
                yield ('content', 'child discovered the answer')
            elif 'Subagent results, supplied' in content:
                assert 'child discovered the answer' in content
                yield ('content', 'final answer uses child result')
            elif '"tool": "subagent"' not in content and 'tool=subagent' not in content:
                yield ('content', '```json\n{"tool":"subagent","args":{"action":"start","task":"child research task"}}\n```')
            else:
                yield ('content', 'I will answer now')

    parent = Agent(config=BeeConfig(provider='fake', model='model', native_tools=False, max_turns=2), workdir=str(tmp_path))
    parent.providers.register(Provider())
    result = []
    thread = threading.Thread(target=lambda: result.append(parent.run_sync('Research this request thoroughly.', Session())), daemon=True)
    thread.start()
    assert child_started.wait(5)
    time.sleep(0.4)
    assert thread.is_alive(), 'orchestrator must wait for its child'
    gate.set()
    thread.join(10)
    assert not thread.is_alive()
    assert result == ['final answer uses child result']


def test_usage_exposes_context_window_and_actual_input_limit(monkeypatch):
    from beeagent import serve
    from beeagent.core import windows
    from beeagent.core.session import Session

    class Context:
        model = 'glm-5.3-flash'
        model_window = None
        max_tokens = 28672
        def build_messages(self, messages, schemas):
            return [{'content': 'hello world'}]

    agent = SimpleNamespace(context=Context(), tools=SimpleNamespace(to_schemas=lambda: []))
    monkeypatch.setitem(serve._state, 'agent', agent)
    monkeypatch.setitem(serve._state, 'config', SimpleNamespace(model='glm-5.3-flash'))
    monkeypatch.setattr(windows, 'measured', lambda model: None)
    result = serve._usage(Session())
    assert result['contextWindow'] == 1048576
    assert result['contextEstimated'] is True
    assert result['limit'] == 28672
    assert result['contextPct'] == int(100 * result['tokens'] / result['contextWindow'])


def test_new_chat_title_is_streamed_cleanly_and_persisted_once(tmp_path, monkeypatch):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from beeagent import serve
    from beeagent.core.agent import Agent
    from beeagent.core.session import Session

    monkeypatch.chdir(tmp_path)
    requests = []
    class Provider:
        name = 'fake'
        models = ['model']
        supports_tools = False
        async def chat_stream(self, messages, model=''):
            requests.append(messages)
            if len(requests) == 1:
                for part in ['█При', 'ветствие█', ' Привет!']:
                    yield ('content', part)
            else:
                yield ('content', 'Как дела?')
    config = BeeConfig(provider='fake', model='model', native_tools=False)
    agent = Agent(config=config, workdir=str(tmp_path))
    agent.providers.register(Provider())
    monkeypatch.setitem(serve._state, 'agent', agent)
    monkeypatch.setitem(serve._state, 'config', config)
    monkeypatch.setitem(serve._state, 'workdir', str(tmp_path))
    session = Session(); session.save(str(tmp_path))
    server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    def send(message):
        payload = json.dumps({'session_id':session.session_id, 'message':message}).encode()
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/api/chat', payload,
                                     {'Content-Type':'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.read().decode()
    try:
        first = send('Привет')
        assert 'event: title' in first and 'Приветствие' in first
        assert '█' not in first
        saved = Session.load(session.session_id, str(tmp_path))
        assert saved.title == 'Приветствие'
        assert saved.messages[-1].content == 'Привет!'
        assert serve._sessions()[0]['title'] == 'Приветствие'
        send('Что дальше?')
        assert 'first response only' in str(requests[0])
        assert 'one or two words' in str(requests[0])
        assert 'first response only' not in str(requests[1])
    finally:
        server.shutdown(); server.server_close()
