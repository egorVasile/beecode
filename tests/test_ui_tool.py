"""The `ui` tool: the model changes the desktop interface, BeeCode applies it.

Everything the tool may touch lives under `.beeagent/ui/` in the working
directory — the application's own code is never rewritten. Only validated
values land in prefs.json, and the stylesheet is CSS alone.
"""
import json

from beeagent.tools.ui import CSS_FILE, PREFS_FILE, custom_css_path, read_prefs


def _prefs(tmp_path):
    return json.loads((tmp_path / PREFS_FILE).read_text(encoding="utf-8"))


def test_guide_names_the_contract(tmp_path, monkeypatch):
    from beeagent.tools.ui import UiTool

    monkeypatch.chdir(tmp_path)
    out = UiTool().execute(action="guide")
    assert not out.error
    assert "brutal" in out.output and "paper" in out.output
    assert "--amber" in out.output


def test_list_shows_defaults_on_empty(tmp_path, monkeypatch):
    from beeagent.tools.ui import UiTool

    monkeypatch.chdir(tmp_path)
    out = UiTool().execute(action="list")
    assert not out.error
    assert "brutal" in out.output
    assert "custom.css: no" in out.output


def test_set_stores_validated_fields_and_bumps_version(tmp_path, monkeypatch):
    from beeagent.tools.ui import UiTool

    monkeypatch.chdir(tmp_path)
    tool = UiTool()
    out = tool.execute(action="set", skin="paper", accent="#00ff00")
    assert not out.error, out.output
    prefs = _prefs(tmp_path)
    assert prefs["skin"] == "paper" and prefs["accent"] == "#00ff00"
    assert prefs["theme"] is None, "untouched fields stay null"
    assert prefs["version"] == 1 and prefs["updated_at"] > 0
    out = tool.execute(action="set", theme="zinc")
    assert _prefs(tmp_path)["version"] == 2
    assert _prefs(tmp_path)["skin"] == "paper", "second set keeps the first"


def test_set_refuses_garbage(tmp_path, monkeypatch):
    from beeagent.tools.ui import UiTool

    monkeypatch.chdir(tmp_path)
    tool = UiTool()
    for bad in ({"skin": "neon"}, {"theme": "dark"}, {"accent": "green"},
                {"accent": "#gggggg"}):
        out = tool.execute(action="set", **bad)
        assert out.error, bad
    assert not (tmp_path / PREFS_FILE).exists(), "nothing stored on refusal"
    out = tool.execute(action="set")
    assert out.error, "bare set changes nothing"


def test_css_installs_and_clears(tmp_path, monkeypatch):
    from beeagent.tools.ui import UiTool

    monkeypatch.chdir(tmp_path)
    tool = UiTool()
    out = tool.execute(action="css", text="#chat { border: 3px solid red; }")
    assert not out.error, out.output
    assert custom_css_path().read_text(encoding="utf-8").startswith("#chat")
    out = tool.execute(action="css", text="  ")
    assert not out.error
    assert not custom_css_path().exists(), "empty text clears"


def test_css_refuses_markup_and_huge_files(tmp_path, monkeypatch):
    from beeagent.tools.ui import UiTool

    monkeypatch.chdir(tmp_path)
    tool = UiTool()
    out = tool.execute(action="css", text="<script>alert(1)</script>")
    assert out.error
    assert not custom_css_path().exists()
    out = tool.execute(action="css", text="x" * 20001)
    assert out.error


def test_reset_restores_defaults(tmp_path, monkeypatch):
    from beeagent.tools.ui import UiTool

    monkeypatch.chdir(tmp_path)
    tool = UiTool()
    tool.execute(action="set", skin="brutal")
    tool.execute(action="css", text="body { color: red; }")
    out = tool.execute(action="reset")
    assert not out.error
    assert not (tmp_path / PREFS_FILE).exists()
    assert not (tmp_path / CSS_FILE).exists()
    assert read_prefs()["version"] == 0


def test_unknown_action_is_an_error(tmp_path, monkeypatch):
    from beeagent.tools.ui import UiTool

    monkeypatch.chdir(tmp_path)
    assert UiTool().execute(action="dance").error
