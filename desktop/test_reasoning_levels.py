import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from beeagent import serve
from beeagent.config.schema import BeeConfig
from beeagent.config.loader import load_config
from beeagent.core.agent import Agent


def test_efforts_persist_validate_and_old_clients_preserve_them(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = BeeConfig()
    agent = Agent(config=config, workdir=str(tmp_path))
    monkeypatch.setitem(serve._state, 'config', config)
    monkeypatch.setitem(serve._state, 'agent', agent)
    monkeypatch.setitem(serve._state, 'workdir', str(tmp_path))
    server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    def save(item):
        data={'action':'save','name':'test-provider','url':'https://api.example.com/v1',
              'models':['model-a'],'modelDetails':[item]}
        request=urllib.request.Request(base+'/api/providers', json.dumps(data).encode(),
                                      {'Content-Type':'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request) as response: return response.status
        except urllib.error.HTTPError as error: return error.code
    item={'id':'model-a','name':'A','context':128000,'reasoning':True,'efforts':['low','high']}
    try:
        assert save(item) == 200
        assert load_config(str(tmp_path)).custom_providers[0].model_details[0]['efforts'] == ['low','high']
        with urllib.request.urlopen(base+'/api/providers') as response:
            providers = json.load(response)['providers']
        assert next(p for p in providers if p['name']=='test-provider')['modelDetails'][0]['efforts'] == ['low','high']
        old = dict(item); old.pop('efforts')
        assert save(old) == 200
        assert config.custom_providers[0].model_details[0]['efforts'] == ['low','high']
        assert save(dict(item, efforts=['hugh'])) == 400
        assert config.custom_providers[0].model_details[0]['efforts'] == ['low','high']
        assert save(dict(item, reasoning=False)) == 200
        assert config.custom_providers[0].model_details[0]['efforts'] == []
    finally:
        server.shutdown(); server.server_close(); agent.plugins.shutdown()


def test_selected_effort_is_sent_in_native_streaming_and_plain_requests():
    import asyncio
    import httpx
    from beeagent.providers.openai_compat import OpenAICompatProvider, reasoning_effort_var
    bodies=[]
    def answer(request):
        body=json.loads(request.content); bodies.append(body)
        if body['stream']:
            return httpx.Response(200, headers={'content-type':'text/event-stream'},
                                  text='data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={'choices':[{'message':{'content':'OK'}}]})
    provider=OpenAICompatProvider('https://example.com/v1',model='model')
    provider._client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(answer))
    async def run():
        token=reasoning_effort_var.set('high')
        try:
            await provider.chat([{'role':'user','content':'hello'}])
            await provider.complete([{'role':'user','content':'hello'}],tools=[])
            assert [part async for part in provider.chat_stream([{'role':'user','content':'hello'}])]
        finally:
            reasoning_effort_var.reset(token)
        await provider.chat([{'role':'user','content':'hello'}])
    asyncio.run(run())
    assert all(body['reasoning_effort']=='high' for body in bodies[:3])
    assert 'reasoning_effort' not in bodies[3]


def test_chat_validates_effort_and_sends_it_to_provider(tmp_path, monkeypatch):
    import httpx
    from beeagent.config.schema import CustomProvider
    from beeagent.core.session import Session
    from beeagent.providers.openai_compat import OpenAICompatProvider
    monkeypatch.chdir(tmp_path)
    config=BeeConfig(provider='sample', model='model-a', native_tools=True)
    config.custom_providers=[CustomProvider(name='sample',type='openai_compat',url='https://api.example.com/v1',
        model='model-a',model_details=[{'id':'model-a','reasoning':True,'efforts':['low','high']}])]
    agent=Agent(config=config,workdir=str(tmp_path))
    captured=[]
    def answer(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'Done.'}}]})
    provider=OpenAICompatProvider('https://api.example.com/v1',name='sample',model='model-a')
    provider._client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(answer))
    agent.providers.register(provider,replace=True)
    monkeypatch.setitem(serve._state,'config',config); monkeypatch.setitem(serve._state,'agent',agent)
    monkeypatch.setitem(serve._state,'workdir',str(tmp_path))
    session=Session(); session.save(str(tmp_path))
    server=ThreadingHTTPServer(('127.0.0.1',0),serve.Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base=f'http://127.0.0.1:{server.server_address[1]}'
    def send(effort):
        data={'session_id':session.session_id,'message':'Check this','model':'model-a','reasoning_effort':effort}
        request=urllib.request.Request(base+'/api/chat',json.dumps(data).encode(),{'Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.urlopen(request) as response: return response.status,response.read()
        except urllib.error.HTTPError as error: return error.code,error.read()
    try:
        assert send('ultra')[0]==400 and not captured
        assert send('high')[0]==200 and captured[-1]['reasoning_effort']=='high'
        assert send('')[0]==200 and 'reasoning_effort' not in captured[-1]
    finally:
        server.shutdown();server.server_close();agent.plugins.shutdown()


def test_provider_rejection_is_not_retried_without_effort():
    import asyncio
    import httpx
    import pytest
    from beeagent.providers.base import ProviderAPIError
    from beeagent.providers.openai_compat import OpenAICompatProvider, reasoning_effort_var
    requests=[]
    def reject(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400,json={'error':{'message':'unsupported reasoning_effort'}})
    provider=OpenAICompatProvider('https://example.com/v1',model='m')
    provider._client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(reject))
    async def go():
        token=reasoning_effort_var.set('ultra')
        try:
            with pytest.raises(ProviderAPIError,match='unsupported reasoning_effort'):
                await provider.complete([{'role':'user','content':'hello'}])
        finally: reasoning_effort_var.reset(token)
    asyncio.run(go())
    assert len(requests)==1 and requests[0]['reasoning_effort']=='ultra'
