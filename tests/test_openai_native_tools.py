import json

import httpx
import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.providers.openai_compat import OpenAICompatProvider


def test_custom_provider_uses_native_tools_and_sends_results(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'sample.txt').write_text('known test content', encoding='utf-8')
    requests = []

    def reply(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body['messages'][0]['role'] == 'system'
        assert body['tools'] and body['tool_choice'] == 'auto'
        if len(requests) == 1:
            message = {'content': None, 'tool_calls': [{'id': 'call-1', 'type': 'function',
                       'function': {'name': 'read', 'arguments': '{"path":"sample.txt"}'}}]}
        else:
            assert any(row['role'] == 'tool' and 'known test content' in row['content']
                       for row in body['messages'])
            message = {'content': 'The file contains the expected test content.'}
        return httpx.Response(200, json={'choices': [{'message': message}]})

    config = BeeConfig(provider='fake', model='model', native_tools=True)
    agent = Agent(config=config, workdir=str(tmp_path))
    provider = OpenAICompatProvider('https://example.com/v1', name='fake', model='model')
    provider._client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(reply))
    agent.providers.register(provider)
    assert 'expected test content' in agent.run_sync('Read sample.txt and report its contents.')
    assert len(requests) == 2


def test_malformed_tool_arguments_are_not_silently_replaced():
    import asyncio
    from beeagent.providers.base import ProviderAPIError

    message = {'tool_calls': [{'type': 'function', 'function': {
        'name': 'read', 'arguments': '{broken'}}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={'choices': [{'message': message}]}))
    provider = OpenAICompatProvider('https://example.com/v1', name='fake')
    provider._client = lambda: httpx.AsyncClient(transport=transport)
    with pytest.raises(ProviderAPIError, match='invalid tool arguments'):
        asyncio.run(provider.complete([{'role': 'user', 'content': 'hello'}], 'model', []))
