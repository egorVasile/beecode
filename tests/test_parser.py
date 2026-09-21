"""The shapes models emit for real, and what BeeCode does with each.

Every case here is a structural defect, not a missing value: a value that never
arrived must never be invented, so those cases assert on the refusal instead.
"""
import asyncio


from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.parser import CommandParser
from beeagent.core.session import Session

FENCE = "```"


def calls(response):
    return [(c.tool, c.args) for c in CommandParser().parse(response).commands]


def test_a_value_written_across_real_newlines_still_runs():
    """The usual death of a file write: Enter inside a quoted value."""
    parsed = CommandParser().parse('{"tool": "write", "args": {"path": "a.txt", '
                                   '"content": "первая\nвторая\n"}}')
    assert parsed.has_commands, parsed.dropped
    assert parsed.commands[0].args["content"] == "первая\nвторая\n"
    assert any("newline" in note for note in parsed.repaired), parsed.repaired


def test_single_quotes_and_bare_keys_are_read_as_json():
    assert calls("{'tool': 'read', 'args': {'path': 'a.py'}}") == [("read", {"path": "a.py"})]
    assert calls('{tool: "read", args: {path: "a.py"}}') == [("read", {"path": "a.py"})]


def test_a_missing_comma_between_two_members_is_not_a_lost_call():
    parsed = CommandParser().parse('{"tool": "read", "args": {"path": "a.py" "page": 2}}')
    assert parsed.commands[0].args == {"path": "a.py", "page": 2}
    assert any("comma" in note for note in parsed.repaired), parsed.repaired


def test_python_literals_become_json_ones():
    parsed = CommandParser().parse('{"tool": "bash", "args": {"command": "ls", "bg": True}}')
    assert parsed.commands[0].args["bg"] is True
    assert any("literal" in note for note in parsed.repaired), parsed.repaired


def test_the_tool_can_be_named_by_any_of_the_keys_models_reach_for():
    assert calls('{"name": "read", "arguments": {"path": "a.py"}}') == [("read", {"path": "a.py"})]
    assert calls('{"function": {"name": "read", "arguments": "{\\"path\\": \\"a.py\\"}"}}') \
        == [("read", {"path": "a.py"})]
    assert calls('{"tool_calls": [{"function": {"name": "bash", '
                 '"arguments": "{\\"command\\": \\"ls\\"}"}}]}') == [("bash", {"command": "ls"})]


def test_arguments_wrapped_in_a_string_are_unwrapped():
    assert calls('{"tool": "read", "args": "{\\"path\\": \\"a.py\\"}"}') \
        == [("read", {"path": "a.py"})]


def test_a_flattened_call_keeps_its_loose_keys_as_arguments():
    assert calls('{"tool": "read", "path": "a.py"}') == [("read", {"path": "a.py"})]


def test_an_array_of_calls_is_a_list_of_calls():
    parsed = CommandParser().parse('[{"tool": "read", "args": {"path": "a.py"}}, '
                                   '{"tool": "read", "args": {"path": "b.py"}}]')
    assert [c.args["path"] for c in parsed.commands] == ["a.py", "b.py"]


def test_a_catalog_entry_echoed_back_is_not_a_call():
    """A schema entry mentions a name and a description; it must not run."""
    parsed = CommandParser().parse('{"name": "read", "description": "Read a file"}')
    assert not parsed.has_commands
    assert not parsed.dropped
    assert "description" in parsed.text


def test_a_cut_off_payload_is_refused_explained_and_kept_off_screen():
    parsed = CommandParser().parse('Сейчас запишу страницу.\n' + FENCE + 'json\n'
                                   + '{"tool": "write", "args": {"path": "a.html", '
                                     '"content": "<html><body><div>и ещё много текста')
    assert not parsed.has_commands
    assert parsed.dropped, "the model has to be told what broke"
    assert "content" in parsed.dropped[0]
    assert "characters" in parsed.dropped[0]
    assert parsed.text == "Сейчас запишу страницу."


def test_a_tag_body_that_is_json_becomes_the_arguments():
    tag = "<tool" + "_call" + " read\n{\"path\": \"a.py\"}\n</tool" + "_call" + ">"
    assert calls(tag) == [("read", {"path": "a.py"})]


def test_the_model_is_asked_again_after_a_cut_off_call(tmp_path, monkeypatch):
    """Ending the turn on "I will create the file" is what reads as being ignored."""
    monkeypatch.chdir(tmp_path)

    class Cutter:
        name = "cutter"

        def __init__(self):
            self.calls = 0
            self.seen = []

        async def chat_stream(self, messages, model=""):
            self.calls += 1
            self.seen.append(" ".join(str(m.get("content")) for m in messages))
            if self.calls == 1:
                yield ("content", '{"tool": "write", "args": {"path": "a.txt", '
                                  '"content": "одна большая строка, которая обрывается')
            elif self.calls == 2:
                yield ("content", '{"tool": "write", "args": {"path": "a.txt", '
                                  '"content": "готово"}}')
            else:
                yield ("content", "записал")

        async def chat(self, messages, model=""):
            return "записал"

    agent = Agent(config=BeeConfig(permissions={"mode": "auto"}), workdir=str(tmp_path))
    endpoint = Cutter()
    agent.providers.register(endpoint)
    agent.providers.select = lambda name: endpoint
    events = []

    asyncio.run(agent.run("запиши файл", session=Session(),
                          callback=lambda kind, data: events.append(kind)))

    assert "tool_dropped" in events
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "готово"
    assert "[BeeCode] Your tool call arrived broken" in endpoint.seen[-1]


def test_a_cut_off_call_is_asked_for_once(tmp_path, monkeypatch):
    """A model that keeps sending broken payloads must not loop forever."""
    monkeypatch.chdir(tmp_path)

    class AlwaysCut:
        name = "always-cut"

        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, model=""):
            self.calls += 1
            # A different payload each time: an identical reply would be caught
            # by the prompt-echo guard and retried, which is not what this tests.
            yield ("content", '{"tool": "write", "args": {"path": "a%d.txt", '
                              '"content": "обрыв' % self.calls)

        async def chat(self, messages, model=""):
            return ""

    agent = Agent(config=BeeConfig(permissions={"mode": "auto"}, max_turns=6),
                  workdir=str(tmp_path))
    endpoint = AlwaysCut()
    agent.providers.register(endpoint)
    agent.providers.select = lambda name: endpoint

    asyncio.run(agent.run("запиши", session=Session()))

    assert endpoint.calls <= 3, f"the re-ask repeated {endpoint.calls} times"
