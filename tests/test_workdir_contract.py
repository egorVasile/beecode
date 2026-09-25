"""Handed to the source owners: the working directory is two different things.

Both tests below were measured on this box, not reasoned about.  They are marked
``xfail(strict=True)`` so that the day the source is fixed the suite turns red
here and somebody has to come and delete the marker -- which is the only way an
xfail stays honest.

The shared shape: one part of BeeCode decides "the project" from a parameter and
another part decides it from ``os.getcwd()``.  While BeeCode is launched inside
the project those are the same folder, so nothing looks wrong.  The moment they
are not -- an embedding program, a plugin pointing a manager elsewhere, a test
that forgot to ``chdir`` -- the config comes from one folder and the files land
in the other.  That is the same defect that put ``big.svg``, ``diagram.svg``,
``out.svg`` and ``nul`` in the repository root.
"""
import json
import os
from pathlib import Path

import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent


@pytest.fixture()
def two_projects(tmp_path):
    """`launched` is where the process is; `project` is where BeeCode was told to work."""
    launched = tmp_path / "launched"
    project = tmp_path / "project"
    (project / ".beeagent" / "plugins").mkdir(parents=True)
    launched.mkdir()
    original = Path.cwd()
    os.chdir(str(launched))
    try:
        yield launched, project
    finally:
        os.chdir(str(original))


@pytest.mark.xfail(strict=True, reason=(
    "beeagent/tools/_path_policy.py:96 `working_dir()` returns os.getcwd(), but "
    "beeagent/core/agent.py:157 stores a `workdir` that only ever reaches "
    "`load_config`. USER-VISIBLE: Agent(workdir=B) reads B/beeagent.json and then "
    "`write`/`edit`/`bash`/`diagram` land every relative path in the folder "
    "BeeCode happened to be launched from, so `write notes.txt` on project B "
    "creates the file in project A -- and the write tool's own description "
    "('Only inside the working directory') promises the opposite of what "
    "_path_policy enforces."))
def test_a_tool_writes_into_the_folder_the_agent_was_given(two_projects):
    launched, project = two_projects
    agent = Agent(config=BeeConfig(), workdir=str(project))
    assert agent.workdir == str(project)

    result = agent.tools.get("write").execute(path="notes.txt", content="hello")
    assert result.error is False, result.output

    assert (project / "notes.txt").exists(), \
        "the agent was pointed at this folder; the file belongs here"
    assert not (launched / "notes.txt").exists(), \
        "the file landed in the launch folder instead"


@pytest.mark.xfail(strict=True, reason=(
    "beeagent/plugins/manager.py:29 binds STATE_PATH = Path('.beeagent')/"
    "'plugins.json' relative to the process at import time, and manager.py:75 "
    "makes it the default `state_path` even when `plugins_dir` names another "
    "folder. USER-VISIBLE: a manager whose project_root() is B writes its "
    "enable/disable ledger into A -- measured here, `_save_state` on a B-rooted "
    "manager created A/.beeagent/plugins.json. `/plugin enable x` in one project "
    "silently edits another project's plugin list, which is also the file "
    "core/trust.py asks the user about before running anything."))
def test_the_plugin_ledger_belongs_to_the_folder_it_describes(two_projects):
    launched, project = two_projects
    from beeagent.plugins.manager import PluginManager

    manager = PluginManager(plugins_dir=str(project / ".beeagent" / "plugins"))
    assert manager.project_root() == project.resolve(), \
        "the manager already knows which folder it speaks for"

    manager._save_state({"installed": {"demo": {"enabled": True}}})

    assert (project / ".beeagent" / "plugins.json").exists(), \
        "the ledger of project B has to live in project B"
    assert not (launched / ".beeagent" / "plugins.json").exists(), \
        "it was written into the launch folder; see manager.py:29"


# --- the two cases that are *not* bugs, pinned so a fix does not overshoot -----

def test_an_absolute_state_path_is_honoured(tmp_path, monkeypatch):
    """A caller that names the file gets the file: only the *default* is broken."""
    from beeagent.plugins.manager import PluginManager

    state = tmp_path / "somewhere" / "plugins.json"
    manager = PluginManager(plugins_dir=str(tmp_path / "plugins"),
                            state_path=state)
    manager._save_state({"installed": {}})
    assert state.exists()


def test_the_path_policy_does_consult_the_folder_it_calls_the_working_one(tmp_path, monkeypatch):
    """A relative `write` lands in cwd; a path that leaves every trusted root is
    refused rather than quietly redirected.

    The scratch/temp tree IS a trusted root — the suite lives there, and a tool
    that could not write temp could not run at all. So the escape has to aim above
    the temp root itself, or the test proves nothing but the allowance.
    """
    import tempfile
    from pathlib import Path

    from beeagent.tools.write import WriteTool

    monkeypatch.chdir(tmp_path)     # the tools resolve against the process cwd
    inside = WriteTool().execute(path="a.txt", content="x")
    assert inside.error is False
    assert (tmp_path / "a.txt").exists()

    above_temp = Path(tempfile.gettempdir()).parent / "beecode-policy-probe.txt"
    escape = WriteTool().execute(path=str(above_temp), content="x")
    assert escape.error is True, "a path outside every root is refused, not redirected"
    assert not above_temp.exists(), f"the refusal still wrote {above_temp}"
    assert "outside" in escape.output.lower() or "refus" in escape.output.lower() \
        or "work" in escape.output.lower(), escape.output
