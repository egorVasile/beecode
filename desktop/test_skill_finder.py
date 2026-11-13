import hashlib
import os
from pathlib import Path
from beeagent.core.agent import Agent
from beeagent.core.permissions import Permissions
from beeagent.tools import skill_finder as finder


def blob(path, data, mode='100644'):
    digest=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
    return {'path':path,'sha':digest,'type':'blob','mode':mode}


def setup_fake(monkeypatch, entries, content):
    def fake_json(url,params=None):
        if url==finder.SEARCH_URL:
            return {'skills':[{'name':'demo','source':'example/repo','skillId':'demo','installs':42}]}
        if '/commits/HEAD' in url: return {'sha':'a'*40}
        return {'tree':entries,'truncated':False}
    monkeypatch.setattr(finder, '_json', fake_json)
    monkeypatch.setattr(finder, '_bytes', lambda url,limit=finder.MAX_FILE: content[url.split('/'+'a'*40+'/')[1]])


def test_builtin_skill_search_and_real_install_flow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files={'skills/one/SKILL.md':b'---\nname: demo\ndescription: Example skill\n---\nUse the reference.\n',
           'skills/one/references/help.md':b'Reference text'}
    setup_fake(monkeypatch,[blob(k,v) for k,v in files.items()],files)
    agent=Agent(workdir=str(tmp_path))
    try:
        assert agent.tools.get('skill_finder') and not Permissions(mode='readonly').allows(agent.tools.get('skill_finder'))
        assert any(s.name=='skill-finder' and s.builtin for s in agent.plugins.skills)
        assert 'skill_finder' in agent.tools.get('skill').execute(action='load',name='skill-finder').output
        tool=agent.tools.get('skill_finder')
        assert 'example/repo' in tool.execute('search',query='demo').output
        result=tool.execute('install',source='example/repo',skill='demo')
        assert not result.error, result.output
        path=tmp_path/'.beeagent/plugins/demo'
        assert (path/'references/help.md').read_text()=='Reference text'
        assert any(s.name=='demo' for s in agent.plugins.skills)
        assert agent.tools.get('skill').execute(action='load',name='demo').output.endswith('Use the reference.\n')
        assert tool.execute('install',source='example/repo',skill='demo').error
        assert (path/'references/help.md').read_text()=='Reference text'
    finally: agent.plugins.shutdown()


def test_installer_rejects_plugin_and_symlinks(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    skill=b'---\nname: demo\n---\nSkill'
    files={'skills/demo/SKILL.md':skill,'skills/demo/plugin.py':b'bad'}
    setup_fake(monkeypatch,[blob(k,v) for k,v in files.items()],files)
    agent=Agent(workdir=str(tmp_path))
    try:
        result=agent.tools.get('skill_finder').execute('install',source='example/repo',skill='demo')
        assert result.error and 'plugin' in result.output
        assert not (tmp_path/'.beeagent/plugins/demo').exists()
        assert finder._safe_relative('references/help.md') == ('references','help.md')
        for path in ('../outside', 'C:/escape', 'a/../escape', 'plugin.py'):
            try: finder._safe_relative(path)
            except ValueError: continue
            raise AssertionError(path)
    finally: agent.plugins.shutdown()


def test_reload_failure_does_not_remove_builtin_tools(tmp_path, monkeypatch):
    from beeagent.core.agent import Agent
    monkeypatch.chdir(tmp_path)
    agent = Agent(workdir=str(tmp_path))
    try:
        before = agent.tools.get('mcp_customuze_beecode_ui')
        assert before and any(s.name == 'skill-finder' for s in agent.plugins.skills)
        def fail(): raise ImportError('test dependency unavailable')
        monkeypatch.setattr(agent.plugins.extensions, 'clear', fail)
        errors = agent.reload_extensions()
        assert errors and 'test dependency unavailable' in errors[-1]
        assert agent.tools.get('mcp_customuze_beecode_ui') is before
        assert any(s.name == 'skill-finder' for s in agent.plugins.skills)
    finally: agent.plugins.shutdown()


def test_reload_does_not_import_cli_commands_without_contributions(tmp_path, monkeypatch):
    import builtins
    from beeagent.core.agent import Agent
    monkeypatch.chdir(tmp_path)
    agent = Agent(workdir=str(tmp_path))
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == 'beeagent.ui' and 'commands' in (kwargs.get('fromlist') or (args[2] if len(args) > 2 else ())):
            raise ImportError('CLI deliberately unavailable')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    try:
        assert not agent.reload_extensions()
        assert agent.tools.get('mcp_customuze_beecode_ui')
        assert any(s.name == 'skill-finder' for s in agent.plugins.skills)
    finally: agent.plugins.shutdown()
