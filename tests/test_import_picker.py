"""`/providers import` inside the real Textual app: the picker opens, "Add"
saves the clicked row through the validated door, and the config file on disk
gets the endpoint — the same assertions `test_agent_import.py::test_import_by_number_and_name`
makes for the text path, but driven by an actual button click.
"""
import asyncio
import json
from pathlib import Path

from beeagent.config.schema import BeeConfig
from beeagent.core.session import Session

LITERAL_KEY = "sk-test-opencode-aaaabbbbccccdddd"


def _machine(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM7_API_KEY", "sk-test-env-1111222233334444")
    conf = home / ".config" / "opencode"
    conf.mkdir(parents=True)
    (conf / "opencode.json").write_text(json.dumps({
        "provider": {
            "mine": {"options": {"baseURL": "https://mine.test/v1",
                                 "apiKey": LITERAL_KEY},
                     "models": {"m1": {}}},
        },
    }), encoding="utf-8")


def app_running(tmp_path):
    from beeagent.ui.tui import BeeCodeApp

    app = BeeCodeApp(config=BeeConfig(), session=Session())
    app.agent.workdir = str(tmp_path)
    return app


def test_add_button_saves_the_clicked_provider(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _machine(Path.home(), monkeypatch)
    app = app_running(tmp_path)

    seen = {}

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app._handle_command("/providers import")
            await pilot.pause(0.3)
            from beeagent.ui.import_picker import ImportPicker

            screen = app.screen
            assert isinstance(screen, ImportPicker)
            add_buttons = [w for w in screen.query("Button")
                           if str(w.id or "").startswith("ip-add-")]
            assert add_buttons, "no Add button was drawn for an importable provider"
            await pilot.click(add_buttons[0])
            await pilot.pause(0.3)
            # The app is closed below; read the log while it is still mounted.
            seen["log"] = "\n".join(str(line) for line in app.chatlog.lines)
    asyncio.run(scenario())

    saved = json.loads((tmp_path / "beeagent.json").read_text(encoding="utf-8"))
    custom = {c["name"]: c for c in saved.get("custom_providers", [])}
    assert custom, "the Add button did not save anything"
    assert any(c["url"] == "https://mine.test/v1" for c in custom.values())
    for c in custom.values():
        assert c["key"] == LITERAL_KEY                # the real key is on disk …
    assert LITERAL_KEY not in seen["log"]  # … but never echoed to the screen


def test_escape_closes_the_picker_without_saving(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _machine(Path.home(), monkeypatch)
    app = app_running(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app._handle_command("/providers import")
            await pilot.pause(0.3)
            from beeagent.ui.import_picker import ImportPicker

            assert isinstance(app.screen, ImportPicker)
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ImportPicker)
    asyncio.run(scenario())

    assert not (tmp_path / "beeagent.json").exists()
