import json
import pytest

from beeagent.tools.ui import UiTool, read_prefs


def test_components_add_hide_show_remove_and_survive_reload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tool = UiTool()
    assert not tool.execute(action='add', id='tip', kind='notice', slot='sidebar', text='Hello').error
    assert not tool.execute(action='hide', id='new-chat').error
    prefs = read_prefs(str(tmp_path))
    assert prefs['components'][0]['text'] == 'Hello'
    assert prefs['hidden_components'] == ['new-chat']
    assert not tool.execute(action='show', id='new-chat').error
    assert not tool.execute(action='remove', id='tip').error
    assert read_prefs()['components'] == []
    assert read_prefs()['hidden_components'] == []


def test_components_reject_markup_actions_and_bad_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tool = UiTool()
    assert tool.execute(action='hide', id='body > *').error
    assert tool.execute(action='add', id='x', kind='button', slot='main',
                        text='Run', button_action='shell').error
    assert tool.execute(action='add', id='../bad', kind='text', slot='main', text='x').error
    assert not (tmp_path / '.beeagent').exists()


def test_ui_settings_refuse_parent_symlink(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    (tmp_path / '.beeagent').mkdir()
    try:
        (tmp_path / '.beeagent' / 'ui').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('creating symlinks requires privileges')
    with pytest.raises(OSError):
        UiTool().execute(action='add', id='test', kind='text', slot='main', text='x')
    assert list(outside.iterdir()) == []


def test_http_ui_state_and_reset(tmp_path, monkeypatch):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from beeagent import serve

    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(serve._state, 'workdir', str(tmp_path))
    tool = UiTool()
    assert not tool.execute(action='add', id='tip', kind='text', slot='main', text='Hello').error
    assert not tool.execute(action='hide', id='sidebar').error
    server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        with urllib.request.urlopen(base + '/api/ui') as response:
            state = json.load(response)
        assert state['components'][0]['text'] == 'Hello'
        assert state['hiddenComponents'] == ['sidebar']
        request = urllib.request.Request(base + '/api/ui/reset', data=b'{}',
                                         headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request) as response:
            assert json.load(response)['ok']
        with urllib.request.urlopen(base + '/api/ui') as response:
            assert json.load(response)['components'] == []
    finally:
        server.shutdown()
        server.server_close()
