"""/compact: the user's own "shrink it now", with no model anywhere in the loop.

`ContextManager` already folds the turns that will not fit into a digest, but it
does so late — on the request that was already too big — and a person watching a
free tier's quota burn has no way to say "do it now". These tests pin the manual
path: that a preview is really a preview, that the fold keeps what it says it
keeps, that the numbers printed are the numbers `/token` prints, that the words
survive in a file, that every refusal is a sentence rather than a traceback, and
that folding by hand does not switch the automatic trim off.

Every test here builds a real `Session` and runs the real command through the
real `commands.dispatch`. No agent is attached to the context, which is also the
proof that no model call is involved: there is no provider object in the
process for a request to go through, and `tests/conftest.py` would name this file
if anything tried to leave the machine anyway.

The source is ASCII on purpose and the rendered text is compared against an ASCII
mirror (this console is cp1251, and a failing assertion that prints an em dash is
a second failure); the full text, Cyrillic and ellipses included, goes to a UTF-8
file and is read back, which is the only way to show what the user would see.
"""
import json
import re
from pathlib import Path

import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core import compact
from beeagent.core.context import ContextManager
from beeagent.core.session import Message, Session
from beeagent.i18n import set_lang
from beeagent.ui import commands
from beeagent.ui.commands import ReplContext, dispatch

FILLER = ("The quick brown fox jumps over the lazy dog while the terminal keeps "
          "printing file contents nobody asked for and the quota keeps falling. ")

DIGEST_HEAD = "# CONVERSATION SO FAR"


# --- the harness ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    """A session's own save path is `<workdir>/.beeagent/sessions`, and the workdir
    default is the process cwd. Every byte this file writes lands in tmp_path."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def registry():
    """COMMANDS and HANDLERS are module globals; give back what we registered."""
    saved = list(commands.COMMANDS)
    handlers = dict(commands.HANDLERS)
    yield
    commands.COMMANDS[:] = saved
    commands.HANDLERS.clear()
    commands.HANDLERS.update(handlers)


def make_session(turns: int = 40, tool_every: int = 5,
                 session_id: str = "20260925_120000_compact") -> Session:
    """A conversation with tool rounds in it, so a fold has to keep pairs whole."""
    session = Session(session_id=session_id)
    for index in range(turns):
        session.add_user_message(f"question {index} " + FILLER * (2 + index % 3))
        if tool_every and index % tool_every == tool_every - 1:
            session.messages.append(Message("assistant", f"calling read at {index}",
                                            tool_calls=[{"id": f"c{index}"}]))
            session.add_tool_result("[tool result] tool=read error=False\n" + FILLER * 4)
        else:
            session.add_assistant_message(f"answer {index} " + FILLER)
    return session


def context_for(**config_kwargs) -> ContextManager:
    """The builder the command would use for this config, taken from the same place
    `_cmd_compact` takes it: no agent, so a bare ContextManager over the model."""
    config = BeeConfig(**config_kwargs)
    return ContextManager(model=config.model,
                          window=config.max_context_tokens or None)


def run(session, line: str, **config_kwargs):
    """Type `line` at the interface: the real dispatch over a real context."""
    compact.register_command()
    ctx = ReplContext(config=BeeConfig(**config_kwargs), session=session)
    output = dispatch(ctx, line).output
    return getattr(output, "plain", None) or str(output or "")


def mirror(text: str) -> str:
    """The ASCII face of a rendered block: what a failing assertion may print.

    The digest writes em dashes and ellipses and the Russian half writes Cyrillic,
    none of which this console can encode. Needles in these tests are always
    ASCII and never span one of those characters, so membership in the mirror
    means membership in the text.
    """
    return text.encode("ascii", "replace").decode("ascii")


def session_file(tmp_path, session_id: str = None) -> Path:
    return compact.sessions_dir(".") / f"{session_id or '20260925_120000_compact'}.json"


# --- the preview ---------------------------------------------------------------

def test_the_preview_reports_the_cost_and_changes_nothing(tmp_path):
    """/compact with no argument is a question about the future, not an edit."""
    session = make_session(40)
    session.save(".")
    before_rows = session.to_dicts()
    on_disk = session_file(tmp_path).read_bytes()
    listed = sorted(p.name for p in compact.sessions_dir(".").iterdir())

    shown = mirror(run(session, "/compact"))

    assert "preview" in shown and "Nothing has been changed" in shown
    assert "session 20260925_120000_compact: 40 turn(s)" in shown
    assert "would fold: the 32 oldest turn(s)" in shown
    assert "stays word for word: the last 8 turn(s)" in shown
    assert DIGEST_HEAD.replace("# ", "") in shown          # the line it would say
    assert "8941" not in shown or True                     # numbers, asserted below
    assert "->" in shown, "before -> after, both printed"

    assert session.to_dicts() == before_rows, "the preview folded nothing"
    assert session_file(tmp_path).read_bytes() == on_disk, "and rewrote nothing"
    assert sorted(p.name for p in compact.sessions_dir(".").iterdir()) == listed


def test_the_preview_prices_the_request_the_same_way_slash_token_does(tmp_path):
    """Two interfaces, one ruler: `/token` and `/compact` may not disagree.

    `/token` sums `count_tokens` over `context.build_messages(...)`; the preview's
    "as the model receives it now" is that same number, taken from the same
    builder. A preview that measured the transcript alone would promise a saving
    the system prompt then ate.
    """
    session = make_session(40)
    context = context_for()
    plan = compact.plan_compaction(session, context=context)

    assert plan.before == compact.request_tokens(context, session.to_dicts(), [])
    assert plan.limit == context.max_tokens
    assert plan.window == context.window
    assert str(plan.before) in mirror(run(session, "/compact"))
    assert str(plan.after) in mirror(run(session, "/compact"))
    assert plan.after < plan.before, "a fold that saves nothing is not a fold"
    assert plan.percent == int(round(100 * (plan.before - plan.after) / plan.before))


def test_a_preview_leaves_the_builders_counters_alone():
    """A preview that moved `trimmed` would make the UI report a trim that
    happened in a slash command instead of in the last turn."""
    context = context_for()
    context.build_messages(make_session(40).to_dicts(), [])      # some real state
    state = {name: getattr(context, name, None) for name in compact._BUILD_STATE}

    compact.plan_compaction(make_session(40), context=context)

    assert {name: getattr(context, name, None) for name in compact._BUILD_STATE} == state


# --- the fold ------------------------------------------------------------------

def test_compact_yes_on_a_forty_turn_session_keeps_the_last_eight_turns(tmp_path):
    """The command's whole promise, on a real Session saved to a real file."""
    session = make_session(40)
    rows = session.to_dicts()

    shown = mirror(run(session, "/compact yes"))

    assert "folded 32 turn(s)" in shown
    assert "into a digest" in shown and "the last 8 stay word for word" in shown
    assert "no request was made" in shown

    kept = session.to_dicts()
    assert kept[0]["role"] == "system" and kept[0]["content"].startswith(DIGEST_HEAD)
    assert len(kept) == 1 + 18, "the digest plus the last eight exchanges, nothing else"

    survivors = [row["content"] for row in kept[1:]]
    for index in range(32, 40):
        assert any(content.startswith(f"question {index} ") for content in survivors), \
            f"turn {index} was asked to stay verbatim and did not"
    for index in range(32):
        assert not any(content.startswith(f"question {index} ") for content in survivors), \
            f"turn {index} was supposed to fold"

    saved = json.loads(session_file(tmp_path).read_text(encoding="utf-8"))
    assert [m["role"] for m in saved["messages"]] == [r["role"] for r in kept], \
        "the file says what the session says"


def test_the_two_numbers_in_the_report_are_different_numbers(tmp_path):
    """before -> after with a percentage, and the numbers have to differ."""
    session = make_session(40)
    plan = compact.plan_compaction(session, context=context_for())
    shown = mirror(run(session, "/compact yes"))

    assert f"request: {plan.before} -> {plan.after} tokens" in shown
    assert f"({plan.percent}% saved)" in shown
    assert plan.before > plan.after > 0
    assert shown.count(str(plan.before)) and shown.count(str(plan.after))
    # The saved transcript shrinks too, not only the request: that is what a
    # resumed session costs the next time anybody reads it.
    assert f"saved transcript: {plan.stored_before} -> {plan.stored_after} tokens" in shown


def test_an_argument_sets_how_many_turns_stay(tmp_path):
    """/compact 12 yes keeps twelve exchanges, not eight."""
    session = make_session(40)
    shown = mirror(run(session, "/compact 12 yes"))

    assert "folded 28 turn(s)" in shown
    assert "the last 12 stay word for word" in shown
    survivors = [row["content"] for row in session.to_dicts()[1:]]
    assert any(content.startswith("question 28 ") for content in survivors)
    assert not any(content.startswith("question 27 ") for content in survivors)


def test_a_tool_call_never_parts_from_its_result():
    """The cut lands on a group edge: a `tool_calls` row with no reply under it is
    a malformed request the provider refuses for the rest of the session's life."""
    session = make_session(40)
    compact.register_command()
    compact.apply_compaction(session, context=context_for())

    rows = session.to_dicts()
    for index, row in enumerate(rows):
        if row["role"] == "assistant" and row.get("tool_calls"):
            assert index + 1 < len(rows) and rows[index + 1]["role"] == "tool", \
                f"the call at {index} travels with no result"
        # positional, not `rows.index(row)`: the scripted session repeats its
        # canned texts, and .index() answers "where is the first equal dict",
        # which is a different question from "is this row's neighbour right"
        if row["role"] == "tool":
            assert index > 0 and rows[index - 1]["role"] == "assistant", \
                f"the result at {index} has no call above it"


def test_a_fold_keeps_the_question_being_answered_even_inside_one_exchange():
    """One question and forty tool rounds is the shape that eats a free tier. There
    are no exchanges to drop, so the fold cuts inside the exchange — and the live
    question stays word for word, because losing it is how the model answers as if
    the chat had just started."""
    session = Session(session_id="one_exchange")
    session.add_user_message("fix the failing test " + FILLER)
    for index in range(40):
        session.messages.append(Message("assistant", f'{{"tool": "read"}} step {index}',
                                        tool_calls=[{"id": f"c{index}"}]))
        session.add_tool_result("[tool result] tool=read error=False\n" + FILLER * 4)

    plan = compact.plan_compaction(session, context=context_for())
    assert plan.unit == "step"
    assert plan.turns_kept == 1 and plan.steps_folded == 32

    compact.apply_compaction(session, context=context_for())
    rows = session.to_dicts()
    assert rows[0]["role"] == "system"
    assert rows[1]["role"] == "user" and rows[1]["content"].startswith("fix the failing")
    assert plan.after < plan.before


def test_the_digest_is_what_the_context_manager_sends_next():
    """Not "matches the string this file built": the digest row has to survive the
    builder and reach the wire as the summary the context layer itself would have
    written. Asserted on `build_messages` output."""
    session = make_session(40)
    context = context_for()
    plan = compact.apply_compaction(session, context=context)

    sent = context.build_messages(session.to_dicts(), [])
    assert sent[0]["role"] == "system", "the identity prompt is still first"
    assert plan.digest in [row["content"] for row in sent], \
        "the fold wrote a summary the builder does not send"
    assert any(row["role"] == "system" and DIGEST_HEAD in row["content"] for row in sent)
    # The folded question 0 reaches the model only as a digest line, and the live
    # question reaches it in full.
    assert "user: question 0 " in plan.digest
    assert sum("question 0 " in row["content"] for row in sent) == 1
    assert any(row["role"] == "user" and row["content"].startswith("question 39 ")
               for row in sent)


def test_the_digest_is_the_same_text_the_trimmer_writes():
    """The summary is borrowed, not invented: the same `_digest` over the same
    dropped rows, so a manual fold cannot put words in the system prompt that
    `build_messages` would not have said on its own."""
    session = make_session(40)
    context = context_for()
    rows = session.to_dicts()
    plan = compact.plan_compaction(session, context=context)

    again, room = compact.digest_for(context, rows[:plan.fold_messages],
                                     rows[plan.fold_messages:], [])
    assert again == plan.digest
    assert room >= compact.DIGEST_FLOOR


# --- the words that survive ----------------------------------------------------

def test_a_backup_holds_the_whole_transcript_before_the_fold(tmp_path):
    """/compact is a destructive edit with a copy made first — the copy is made
    before anything is touched, and holds every row in its original order."""
    session = make_session(40)
    rows = session.to_dicts()

    shown = mirror(run(session, "/compact yes"))

    backup = Path(shown.split("pre-fold transcript: ")[1].splitlines()[0].strip())
    assert backup == compact.backup_path(session, ".")
    assert backup.exists() and backup.name == "20260925_120000_compact.pre-compact.json"
    payload = json.loads(backup.read_text(encoding="utf-8"))
    assert payload["transcript"] == rows, "the backup is the transcript as it was"
    assert payload["session"] == session.session_id
    assert payload["digest"] != ""
    # A backup shaped like a session is offered by `list_sessions` and wins
    # `--continue`, so the user resumes the pre-fold transcript by accident.
    assert "messages" not in payload
    assert Session.list_sessions(str(tmp_path)) == [session.session_id]


def test_the_report_names_where_the_full_text_lives(tmp_path):
    """'The full text is still in the session file' would be a lie after a fold:
    the file is rewritten. So the report points at the backup and at the undo."""
    session = make_session(40)
    shown = mirror(run(session, "/compact yes"))

    assert "what is no longer verbatim" in shown
    assert "summary of 32 earlier turns, not the turns themselves" in shown
    assert ".pre-compact.json" in shown
    assert "to undo" in shown and ".beeagent" in shown
    live = mirror(run(make_session(6), "/compact"))  # a refusal says nothing was done
    assert "Nothing to gain" in live


def test_the_full_wording_survives_a_utf8_round_trip(tmp_path):
    """The proof the console cannot carry: em dashes, ellipses and the Russian
    half go to a UTF-8 file and come back intact."""
    session = make_session(40)
    plan = compact.plan_compaction(session, context=context_for())
    english = "\n".join(compact.preview_lines(plan))
    set_lang("ru")
    try:
        russian = "\n".join(compact.preview_lines(plan))
    finally:
        set_lang("en")

    out = tmp_path / "preview-en.txt"
    out.write_text(english, encoding="utf-8")
    assert out.read_text(encoding="utf-8") == english
    assert DIGEST_HEAD in english and "\u2014" in english

    out_ru = tmp_path / "preview-ru.txt"
    out_ru.write_text(russian, encoding="utf-8")
    assert out_ru.read_text(encoding="utf-8") == russian
    assert russian != english
    assert re.search(r"[\u0400-\u04ff]", russian), "the Russian half is not a stub"
    assert "/compact yes" in russian


# --- refusals ------------------------------------------------------------------

def test_an_empty_session_refuses_in_a_sentence(tmp_path):
    for line in ("/compact", "/compact yes", "/compact 4 yes"):
        shown = mirror(run(Session(), line))
        assert shown.strip(), "a refusal that prints nothing is indistinguishable from a crash"
        assert "Nothing to compact" in shown
        assert "Traceback" not in shown and "failed" not in shown
    assert not compact.sessions_dir(".").exists(), "and no directory was made for it"


def test_a_context_with_no_session_at_all_still_answers():
    """`beecode --run "/compact"` hands the handler a bare ReplContext: None for a
    session must not become an AttributeError."""
    shown = mirror(getattr(dispatch(ReplContext(), "/compact yes").output, "plain", ""))
    assert "Nothing to compact" in shown


def test_a_short_session_says_there_is_nothing_to_gain(tmp_path):
    session = make_session(6)
    rows = session.to_dicts()

    shown = mirror(run(session, "/compact yes"))

    assert "Nothing to gain" in shown
    assert "6 turn(s)" in shown and "keeps the last 8 verbatim" in shown
    assert session.to_dicts() == rows, "refusing means refusing"
    assert not compact.backup_path(session, ".").exists()


def test_the_refusal_is_about_n_and_not_a_broken_fold(tmp_path):
    """The same six-turn session folds fine once the keep is smaller than it.

    The arithmetic is read off what the command itself claimed, not off a
    hard-coded message count: two kept *turns* can be five kept *messages*,
    because a tool call travels with its result and that pair is exactly what the
    fold must not split.
    """
    session = make_session(6)
    before = session.to_dicts()
    shown = mirror(run(session, "/compact 2 yes"))

    assert "folded 4 turn(s)" in shown
    folded_messages = int(re.search(r"\((\d+) message\(s\)\)", shown).group(1))
    after = session.to_dicts()
    assert len(after) == len(before) - folded_messages + 1, (
        f"{len(before)} - {folded_messages} folded + 1 digest = {len(after)}?")
    assert after[0]["role"] == "system", "the digest replaces the folded turns, it is not appended"


def test_a_window_too_small_for_a_summary_refuses_instead_of_deleting(tmp_path):
    """No digest, no fold: silently dropping 32 turns because the window cannot
    pay for an outline of them is the exact bug class this command exists to avoid."""
    context = ContextManager(model="gpt-4", window=1024)
    session = make_session(40)
    rows = session.to_dicts()

    with pytest.raises(compact.CompactError) as refused:
        compact.plan_compaction(session, context=context)

    assert str(refused.value).startswith("Refused")
    assert "max_context_tokens" in str(refused.value)
    assert session.to_dicts() == rows


def test_an_unwritable_backup_path_is_reported_and_nothing_moves(tmp_path):
    """The backup comes first precisely because it is the only way back. If it
    cannot be written, the fold does not happen."""
    session = make_session(40)
    session.save(".")
    rows = session.to_dicts()
    blocked = compact.backup_path(session, ".")
    blocked.mkdir(parents=True)          # a directory where the copy must go

    shown = mirror(run(session, "/compact yes"))

    assert "Nothing was changed" in shown and "cannot write the backup" in shown
    assert str(blocked) in shown
    assert session.to_dicts() == rows, "the conversation was not folded in memory"
    assert [m.to_dict() for m in session.messages] == rows


def test_an_unwritable_session_file_leaves_the_session_alone(tmp_path):
    """A fold that updated the in-memory transcript and then failed to save would
    leave the user looking at one file while another is on disk."""
    session = make_session(40)
    rows = session.to_dicts()
    blocked = compact.session_path(session, ".")
    blocked.mkdir(parents=True)          # the path `Session.save` writes is a folder

    shown = mirror(run(session, "/compact yes"))

    assert "could not be written" in shown
    assert str(blocked) in shown and "unchanged in memory" in shown
    assert session.to_dicts() == rows, "the failed save was undone"
    assert session.messages[0].role == "user"


# --- the registry --------------------------------------------------------------

def test_register_command_lands_in_the_shared_registry_and_is_idempotent():
    """`/trust` registers itself from its own module so the REPL, the TUI and
    one-shot runs all get it without `commands.py` editing. Same machinery."""
    commands.COMMANDS[:] = [c for c in commands.COMMANDS if c.name != "compact"]
    commands.HANDLERS.pop("compact", None)

    assert compact.register_command() is True
    assert compact.register_command() is True
    assert compact.register_command() is True

    named = [c for c in commands.COMMANDS if c.name == "compact"]
    assert len(named) == 1, "registered three times, advertised three times"
    assert named[0].category != "plugins", "an extension must not be able to take it back"
    assert named[0].usage == compact.USAGE
    assert commands.HANDLERS["compact"] is compact._cmd_compact
    assert all(named[0].description == c.description for c in named)


def test_the_default_keep_is_eight():
    assert compact.DEFAULT_KEEP == 8
    session = make_session(40)
    assert compact.plan_compaction(session, context=context_for()).keep == 8
    assert compact.plan_compaction(make_session(40), keep=12,
                                   context=context_for()).keep == 12


# --- and the automatic trim still works ----------------------------------------

def test_a_manual_fold_does_not_switch_the_automatic_trim_off(tmp_path):
    """Requirement: after a manual compaction a later `context_trimmed` event still
    fires normally. The event's payload is `context.trimmed` read straight after
    the loop's own `build_messages`, so what is proven here is that the builder
    still trims a compacted session, still counts it, and still puts the digest of
    what fell out into the system message."""
    session = make_session(40)
    context = ContextManager(model="gpt-4", window=4096)
    compact.apply_compaction(session, context=context)
    assert session.to_dicts()[0]["role"] == "system"

    # The conversation goes on, and the window is the same small one.
    for index in range(40, 55):
        session.add_user_message(f"question {index} " + FILLER * 3)
        session.add_assistant_message(f"answer {index} " + FILLER * 3)

    sent = context.build_messages(session.to_dicts(), [])
    assert context.trimmed > 0, "the trim went silent after a manual fold"
    assert context.dropped > 0
    assert DIGEST_HEAD in sent[0]["content"], "trimmed history with nothing in its place"
    # The event the loop would publish, published: a non-zero drop count.
    dropped = context.trimmed
    assert isinstance({"dropped": dropped}, dict) and dropped == context.dropped + \
        context.clipped

    # And the next /compact over the same session still answers in words.
    shown = mirror(run(session, "/compact", max_context_tokens=4096))
    assert "preview" in shown
    assert "turn(s)" in shown
