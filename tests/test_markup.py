"""The model's own highlighter: `beeagent/core/markup.py` and the `table` tool.

`/b/`, `/i/`, `/u/`, `/с.color/` in prose; never in code, never glued to a
word, never half-open. The look of everything else does not move: bold and
italic ride native Markdown, underline and highlight come out as Rich spans.
"""
from beeagent.core.markup import Answer, stylize_prose, table_from_metadata
from beeagent.tools.table import TableTool


def test_bold_italic_ride_markdown():
    assert stylize_prose("/b bold/") == "**bold**"
    assert stylize_prose("/i ital/") == "*ital*"


def test_underline_and_highlight_travel_as_markers():
    out = stylize_prose("/u under/ and /c.red hi/")
    assert "\ue000under\ue001" in out
    assert "\ue002red\ue003hi\ue001" in out


def test_cyrillic_es_spells_highlight_too():
    assert "\ue002green\ue003" in stylize_prose("/с.green да/")


def test_slashes_glued_to_words_stay_text():
    assert stylize_prose("and/or a/b test /bin/sh C:/dir") == \
        "and/or a/b test /bin/sh C:/dir"


def test_unclosed_or_bad_tags_stay_text():
    # An unclosed slash tag runs to the line end (that is the documented
    # meaning now); unknown letters and colours stay literal.
    assert stylize_prose("unclosed /b oops") == "unclosed **oops**"
    assert stylize_prose("/x unknown/") == "/x unknown/"
    assert stylize_prose("/c.notacolor hi/") == "/c.notacolor hi/"
    assert stylize_prose("/b /") == "/b /"


def test_code_is_never_touched():
    fenced = "```\n/b code/ /c.red x/\n```\nafter /b ok/"
    out = stylize_prose(fenced)
    assert "/b code/" in out and "/c.red x/" in out
    assert out.endswith("after **ok**")
    assert stylize_prose("`/b code/` and /b ok/") == "`/b code/` and **ok**"


def test_escapes_survive():
    assert stylize_prose("es\\/b caped") == "es/b caped"


def test_hex_colors_and_case():
    assert "\ue002#ff0000\ue003" in stylize_prose("/c.#FF0000 hi/")
    assert "**B**" in stylize_prose("/B B/")


def test_answer_renders_spans():
    from rich.console import Console

    console = Console(width=80, color_system="truecolor")
    text = console.render_lines(Answer("/b b/ /u u/ /c.red h/ plain"), None)
    flat = " ".join(seg.text for line in text for seg in line)
    assert "b" in flat and "plain" in flat
    assert "\ue000" not in flat and "\ue002" not in flat, "markers must be stripped"


def test_answer_keeps_markdown_structure():
    from rich.console import Console

    console = Console(width=80, color_system="truecolor")
    lines = console.render_lines(Answer("# head\n\n- one\n- two\n\n```\n/x/\n```"), None)
    flat = "\n".join("".join(seg.text for seg in line) for line in lines)
    assert "head" in flat and "one" in flat and "/x/" in flat


def test_table_builds_and_caps():
    tool = TableTool()
    assert tool.silent is True
    assert tool.is_safe() is True
    result = tool.execute(["a", "b"], [["1", "2"], ["3"]])
    assert result.error is False
    assert result.metadata["headers"] == ["a", "b"]
    assert result.metadata["rows"] == [["1", "2"], ["3", ""]]
    assert "2x2" in result.output


def test_table_needs_headers_and_trims_wide_rows():
    tool = TableTool()
    assert tool.execute([], [["1"]]).error is True
    result = tool.execute(["a"], [["1", "2", "3"]])
    assert result.metadata["rows"] == [["1"]]


def test_table_from_metadata_roundtrip():
    tool = TableTool()
    result = tool.execute(["a", "b"], [["1", "2"]])
    drawn = table_from_metadata(result.metadata)
    assert drawn is not None
    assert table_from_metadata({}) is None
    assert table_from_metadata({"render": "table", "headers": []}) is None


def _styles(text):
    return [(span.style or "") for span in text.spans]


def test_render_line_styles_each_tag():
    from beeagent.core.markup import render_line

    text = render_line("a /b bold/ b /i it/ c /u un/ d /c.red h/ e")
    assert text is not None
    plain = text.plain
    assert plain == "a bold b it c un d h e"
    joined = " ".join(_styles(text))
    assert "bold" in joined and "italic" in joined
    assert "underline" in joined and "bold red" in joined


def test_render_line_falls_back_raw():
    from beeagent.core.markup import render_line

    # Slash tags auto-close at the line end; markdown marks do not.
    assert render_line("unclosed /b oops") is not None
    assert render_line("unclosed **oops") is None
    assert render_line("unclosed `oops") is None
    assert render_line("plain text") is not None
    assert render_line("plain text").plain == "plain text"


def test_render_line_nests_and_heads():
    from beeagent.core.markup import render_line

    nested = render_line("/b bold /i both/ end/")
    assert nested is not None and nested.plain == "bold both end"
    head = render_line("# title /b x/")
    assert head is not None and "bold" in " ".join(_styles(head))


def test_alias_colors_parse():
    from beeagent.core.markup import stylize_prose

    for name in ("orange", "purple", "teal", "gray", "grey", "pink",
                 "brown", "lime", "violet"):
        out = stylize_prose(f"/c.{name} x/")
        assert out.startswith("\ue002"), f"{name} did not parse: {out!r}"


def test_unclosed_tag_runs_to_line_end():
    from beeagent.core.markup import render_line, stylize_prose

    assert "Оранжевый курсив" in stylize_prose("/с.orange Оранжевый курсив")
    text = render_line("/с.orange Оранжевый курсив")
    assert text is not None and text.plain == "Оранжевый курсив"
    assert "ffa500" in " ".join(s.style or "" for s in text.spans)


def test_nested_highlight_with_inner_style():
    from beeagent.core.markup import render_line

    text = render_line("/с.red /b Красный жирный/")
    assert text is not None
    assert text.plain == "Красный жирный"
    joined = " ".join(s.style or "" for s in text.spans)
    assert "red" in joined and "bold" in joined


def test_user_keyword_examples():
    """The exact shapes from the request: done-announcements and showcases."""
    from beeagent.core.markup import render_line

    done = render_line("Done! I /b fixed 11 bugs! /")
    assert done is not None and done.plain == "Done! I fixed 11 bugs! "
    assert "bold" in " ".join(s.style or "" for s in done.spans)

    site = render_line("I /u created / a beautiful, /b interactive website! /b ")
    assert site is not None
    assert site.plain == "I created  a beautiful, interactive website! "
    joined = " ".join(s.style or "" for s in site.spans)
    assert "underline" in joined and "bold" in joined

    cool = render_line("I created a cool /bAI!/")
    assert cool is not None and cool.plain == "I created a cool AI!"
    assert "bold" in " ".join(s.style or "" for s in cool.spans)


def test_table_is_registered_and_silent_in_the_loop():
    from beeagent.config.schema import BeeConfig
    from beeagent.core.agent import Agent

    agent = Agent(config=BeeConfig())
    assert "table" in agent.tools.list_names()
    assert agent.tools.get("table").silent is True


def test_table_runs_without_announcement_but_reports_render(tmp_path, monkeypatch):
    """Silent means: no tool_start chalk line; tool_end carries the drawing."""
    import asyncio

    from beeagent.config.schema import BeeConfig
    from beeagent.core.agent import Agent
    from beeagent.core.session import Session

    monkeypatch.chdir(tmp_path)

    class TableThenDone:
        name = "fake"
        models = ["fake-model"]
        supports_tools = False

        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, model=""):
            self.calls += 1
            if self.calls == 1:
                yield ("content", '{"tool": "table", "args": {"headers": ["a", "b"], '
                                  '"rows": [["1", "2"], ["3", "4"]]}}')
            else:
                yield ("content", "done")

        async def chat(self, messages, model=""):
            return "done"

    agent = Agent(config=BeeConfig(permissions={"mode": "auto"}),
                  workdir=str(tmp_path))
    endpoint = TableThenDone()
    agent.providers.register(endpoint)
    agent.providers.select = lambda name: endpoint
    events = []

    answer = asyncio.run(agent.run(
        "show it", session=Session(),
        callback=lambda e, d: events.append((e, dict(d)))))
    assert answer == "done"

    starts = [d for e, d in events if e == "tool_start"]
    assert not starts, "a silent tool announces nothing"
    ends = [(e, d) for e, d in events if e == "tool_end"]
    assert len(ends) == 1
    assert ends[0][1]["render"]["headers"] == ["a", "b"]
    assert ends[0][1]["render"]["rows"] == [["1", "2"], ["3", "4"]]
