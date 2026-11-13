import json
import threading
from beeagent.core.projects import Projects
from beeagent.tools.project import ProjectTool
from beeagent.tools.subagent import SubagentTool
from test_subagent_models import FakeAgent


def test_project_groups_are_logical_persistent_and_isolated(tmp_path):
    store = Projects(tmp_path)
    project = store.create('Сайт')
    folder = store.add_folder(project['id'], 'Дизайн')
    nested = store.add_folder(project['id'], 'Макеты', folder['id'])
    store.add_chat(project['id'], nested['id'], '20261113_120000_abcdef')
    assert store.location('20261113_120000_abcdef') == {'project': project['id'], 'folder': nested['id']}
    assert Projects(tmp_path).list()[0]['folders'] == [folder, nested]
    assert not (tmp_path / 'Сайт').exists()
    assert not (tmp_path / 'Дизайн').exists()
    try:
        store.add_chat(project['id'], 'not-a-folder', '20261113_120001_abcdef')
    except ValueError:
        pass
    else:
        raise AssertionError('invalid folder must not be accepted')


def test_subagent_is_saved_in_its_parent_project(tmp_path):
    store = Projects(tmp_path)
    project = store.create('Проект')
    folder = store.add_folder(project['id'], 'Исследование')
    sid = '20261113_120000_abcdef'
    store.add_chat(project['id'], folder['id'], sid)
    parent = FakeAgent(); parent.workdir = str(tmp_path); parent.project_session_id = sid
    manager = SubagentTool(parent)
    started = json.loads(manager.execute('start', task='inspect code').output)
    answer = json.loads(manager.execute('wait', id=started['id']).output)
    assert answer['status'] == 'done'
    saved = Projects(tmp_path).agent(started['id'])
    assert saved['status'] == 'done' and saved['parent'] == sid and saved['folder'] == folder['id']
    assert 'inspect code' in saved['result']
    project_tool = ProjectTool(parent)
    created = project_tool.execute('create_folder', name='Результаты', parent=folder['id'])
    assert not created.error
    assert json.loads(created.output)['parent'] == folder['id']


def test_project_updates_from_threads_do_not_overwrite_each_other(tmp_path):
    store = Projects(tmp_path)
    project = store.create('Проект')
    threads = [threading.Thread(target=store.add_folder, args=(project['id'], str(i))) for i in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(Projects(tmp_path).list()[0]['folders']) == 20


def test_agent_can_choose_existing_folder_and_folder_cannot_cross_projects(tmp_path):
    store = Projects(tmp_path)
    one, two = store.create('Один'), store.create('Два')
    folder = store.add_folder(one['id'], 'Работа')
    other = store.add_folder(two['id'], 'Чужая')
    sid = '20261113_120000_abcdef'
    store.add_chat(one['id'], '', sid)
    parent = FakeAgent(); parent.workdir = str(tmp_path); parent.project_session_id = sid
    manager = SubagentTool(parent)
    assert manager.execute('start', task='inspect', folder=other['id']).error
    child = json.loads(manager.execute('start', task='inspect', folder=folder['id']).output)
    manager.execute('wait', id=child['id'])
    assert Projects(tmp_path).agent(child['id'])['folder'] == folder['id']


def test_project_http_creates_isolated_project_and_chat(tmp_path, monkeypatch):
    import urllib.request
    from http.server import ThreadingHTTPServer
    from beeagent import serve
    from beeagent.core.session import Session
    monkeypatch.setitem(serve._state, 'workdir', str(tmp_path))
    server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    def post(path, value):
        request = urllib.request.Request(base+path, json.dumps(value).encode(),
            {'Content-Type':'application/json'}, method='POST')
        with urllib.request.urlopen(request) as response: return json.load(response)
    try:
        project = post('/api/projects', {'action':'create', 'name':'Проект'})['item']
        folder = post('/api/projects', {'action':'folder', 'project':project['id'], 'name':'Папка'})['item']
        sid = post('/api/sessions/new', {'project':project['id'], 'folder':folder['id']})['id']
        assert Session.load(sid, str(tmp_path)).messages == []
        with urllib.request.urlopen(base+'/api/projects') as response: data=json.load(response)
        assert data['projects'][0]['chats'] == [{'id':sid, 'folder':folder['id']}]
        with urllib.request.urlopen(base+'/api/sessions') as response: sessions=json.load(response)['sessions']
        assert any(item['id'] == sid for item in sessions)
    finally:
        server.shutdown(); server.server_close()


def test_subagent_can_create_project_group_but_cannot_run_shell(tmp_path):
    store = Projects(tmp_path)
    project = store.create('Проект')
    sid = '20261113_120000_abcdef'
    store.add_chat(project['id'], '', sid)
    class FolderAgent(FakeAgent):
        def run_sync(self, prompt, session, callback=None):
            assert self.tools.get('bash') is None
            tool = self.tools.get('project')
            assert tool and tool.is_safe()
            result = tool.execute('create_folder', name='От субагента')
            assert not result.error
            return 'done'
    parent = FolderAgent(); parent.workdir = str(tmp_path); parent.project_session_id = sid
    manager = SubagentTool(parent)
    child = json.loads(manager.execute('start', task='создай группу').output)
    assert json.loads(manager.execute('wait', id=child['id']).output)['status'] == 'done'
    assert Projects(tmp_path).list()[0]['folders'][0]['name'] == 'От субагента'


def test_saved_subagent_is_readable_when_live_manager_is_gone(tmp_path, monkeypatch):
    import urllib.request
    from http.server import ThreadingHTTPServer
    from beeagent import serve
    store = Projects(tmp_path)
    project = store.create('Проект')
    sid = '20261113_120000_abcdef'
    store.add_chat(project['id'], '', sid)
    parent = FakeAgent(); parent.workdir = str(tmp_path); parent.project_session_id = sid
    manager = SubagentTool(parent)
    child = json.loads(manager.execute('start', task='Проверь проект').output)
    manager.execute('wait', id=child['id'])
    monkeypatch.setitem(serve._state, 'workdir', str(tmp_path))
    monkeypatch.setitem(serve._state, 'agent', None)
    server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f'http://127.0.0.1:{server.server_address[1]}'
        with urllib.request.urlopen(base+'/api/subagent?id='+child['id']) as response:
            restored = json.load(response)
        assert restored['task'] == 'Проверь проект' and restored['status'] == 'done'
        with urllib.request.urlopen(base+'/api/projects') as response:
            listed = json.load(response)['projects'][0]['agents'][0]
        assert listed['id'] == child['id'] and 'events' not in listed and 'result' not in listed
    finally:
        server.shutdown(); server.server_close()


def test_real_orchestrator_creates_group_and_subagent_in_project(tmp_path, monkeypatch):
    from beeagent.core.agent import Agent
    from beeagent.core.session import Session
    from beeagent.config.schema import BeeConfig
    monkeypatch.chdir(tmp_path)
    store = Projects(tmp_path)
    project = store.create('Проект')
    session = Session()
    store.add_chat(project['id'], '', session.session_id)
    seen = []
    class Provider:
        name = 'project-fake'
        models = ['model']
        supports_tools = False
        def __init__(self): self.parent_turns = 0
        async def chat(self, messages, model='', stream=False):
            system = next(m['content'] for m in messages if m['role'] == 'system')
            seen.append((threading.current_thread() is threading.main_thread(), system))
            if threading.current_thread() is not threading.main_thread():
                return 'Дочерняя задача выполнена.'
            self.parent_turns += 1
            if self.parent_turns == 1:
                call = {'tool':'project', 'args':{'action':'create_folder', 'name':'Исследование'}}
                return '```json\n'+json.dumps(call, ensure_ascii=False)+'\n```'
            elif self.parent_turns == 2:
                folder = store.list()[0]['folders'][0]['id']
                call = {'tool':'subagent', 'args':{'action':'start','task':'INDEPENDENT_CHILD','folder':folder}}
                return '```json\n'+json.dumps(call)+'\n```'
            elif self.parent_turns == 3:
                return 'Подожду результат.'
            else:
                assert 'Дочерняя задача выполнена.' in str(messages)
                return 'Результат учтён.'
    config = BeeConfig(provider='project-fake', model='model', native_tools=False)
    parent = Agent(config=config, workdir=str(tmp_path))
    provider = Provider(); parent.providers.register(provider)
    parent.project_session_id = session.session_id
    result = parent.run_sync('Создай группу и исследуй проект.', session)
    assert 'Результат учтён' in result
    saved = store.list()[0]
    assert len(saved['agents']) == 1 and saved['agents'][0]['folder'] == saved['folders'][0]['id']
    assert saved['agents'][0]['status'] == 'done', saved['agents'][0]
    assert any(not parent and 'Исследование' in prompt and 'INDEPENDENT_CHILD' in prompt for parent, prompt in seen)
    assert all('CURRENT PROJECT' in prompt and 'Проект' in prompt for _, prompt in seen)


def test_project_snapshot_is_current_isolated_and_excludes_transcripts(tmp_path):
    from beeagent.tools.project import project_prompt
    from beeagent.core.session import Session
    store = Projects(tmp_path)
    project = store.create('Мой проект')
    other = store.create('Секретный другой проект')
    session = Session(); session.title = 'Обсуждение'; session.add_user_message('ЧАСТНАЯ ПЕРЕПИСКА'); session.save(str(tmp_path))
    store.add_chat(project['id'], '', session.session_id)
    store.add_folder(other['id'], 'Чужая папка')
    parent = FakeAgent(); parent.workdir = str(tmp_path); parent.project_session_id = session.session_id
    store.add_folder(project['id'], 'Дизайн')
    store.save_agent(session.session_id, {'id':'child-id', 'task':'Проверить макет', 'status':'running', 'result':'СЕКРЕТНЫЙ РЕЗУЛЬТАТ', 'events':[]})
    view = json.loads(ProjectTool(parent).execute('list').output)
    assert view['name'] == 'Мой проект' and view['folders'][0]['name'] == 'Дизайн'
    assert view['chats'][0]['title'] == 'Обсуждение' and view['agents'][0]['task'] == 'Проверить макет'
    prompt = project_prompt(parent)
    assert 'Дизайн' in prompt and 'Проверить макет' in prompt
    assert all(secret not in prompt for secret in ('Чужая папка', 'Секретный другой проект', 'ЧАСТНАЯ ПЕРЕПИСКА', 'СЕКРЕТНЫЙ РЕЗУЛЬТАТ'))
    store.add_folder(project['id'], 'Новая группа')
    assert 'Новая группа' in project_prompt(parent)
    parent.project_folder_id = view['folders'][0]['id']
    assert json.loads(ProjectTool(parent).execute('list').output)['current_folder'] == parent.project_folder_id
