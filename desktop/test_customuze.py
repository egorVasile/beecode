import asyncio
import os
import sys
from pathlib import Path

from beeagent.plugins.mcp import McpStdioClient
from beeagent.plugins.customuze import read_instructions
from beeagent.tools.ui import read_prefs


def test_real_builtin_mcp_ui_and_instructions(tmp_path, monkeypatch):
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    client = McpStdioClient('customuze_beecode', sys.executable,
                            ['-m', 'beeagent.plugins.customuze', str(workspace)])
    async def run():
        try:
            await client.start()
            assert {t['name'] for t in await client.list_tools()} == {'ui', 'instructions'}
            assert 'updated' in await client.call_tool('instructions', {'action':'set','text':'Отвечай кратко'})
            assert 'Отвечай кратко' in await client.call_tool('instructions', {'action':'get'})
            assert 'applied' in await client.call_tool('ui', {'action':'set','theme':'zinc'})
            assert '(MCP error)' in await client.call_tool('instructions', {'action':'set','text':''})
        finally:
            await client.stop()
    asyncio.run(run())
    assert read_instructions(str(workspace)) == 'Отвечай кратко'
    assert read_prefs(str(workspace))['theme'] == 'zinc'
    assert read_instructions(str(tmp_path / 'another')) == ''


def test_builtin_tool_registered_and_uses_permissions(tmp_path, monkeypatch):
    from beeagent.core.agent import Agent
    from beeagent.core.permissions import Permissions
    monkeypatch.chdir(tmp_path)
    agent = Agent(workdir=str(tmp_path))
    ui = agent.tools.get('mcp_customuze_beecode_ui')
    instructions = agent.tools.get('mcp_customuze_beecode_instructions')
    assert ui and instructions and agent.tools.get('ui') is ui
    assert 'ui' not in agent.tools.list_names()
    assert not Permissions(mode='readonly').allows(ui)
    assert not Permissions(mode='ask').allows(instructions)
    assert Permissions(mode='auto').allows(instructions)
    agent.reload_extensions()
    assert agent.tools.get('mcp_customuze_beecode_ui')
    agent.plugins.shutdown()


def test_instructions_reach_next_model_request(tmp_path, monkeypatch):
    from beeagent.core.agent import Agent
    from beeagent.config.schema import BeeConfig
    from beeagent.plugins.customuze import write_instructions
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    write_instructions(str(tmp_path), 'Отвечай кратко и по делу')
    class Provider:
        name = 'fake-customuze'
        models = ['test']
        supports_tools = False
        async def chat(self, messages, model='', stream=False):
            assert 'Отвечай кратко и по делу' in messages[0]['content']
            return 'Готово.'
    agent = Agent(config=BeeConfig(provider='fake-customuze', model='test', native_tools=False), workdir=str(tmp_path))
    agent.providers.register(Provider())
    assert 'Готово' in agent.run_sync('Привет')
    agent.plugins.shutdown()
