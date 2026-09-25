"""/compact: fold the oldest turns into a digest now, on this machine, with no call out.

The ContextManager already shrinks a session that does not fit the window
(`core/context.py`: `digest_room`, `_digest`, the "CONVERSATION SO FAR" block), so
a long session never loses its thread — but it loses it *late*, on the turn where
the request was already too big, and the user watching the quota burn has no way
to say "shrink it now". This command is that button.

Two rules decide the whole design:

* **No model call, no network.** A slash command is dispatched on the thread that
  draws the Textual UI, so one blocking provider request here freezes the screen;
  and spending the user's quota on a housekeeping command is backwards. Every
  number below is arithmetic over the transcript from `utils/tokens.py` — the same
  counter `/token` prints with, which is the only way the preview and the status
  line can agree.
* **Reuse the digest the context layer writes, do not invent one.** A private
  summary format would mean `/compact` puts words in the system prompt that
  `build_messages` never agreed to send. So this file calls the same
  `ContextManager` methods the automatic trim calls, and what a manual fold stores
  in the transcript is exactly the text the builder would have produced.

What it costs is honesty about loss: 40 old turns do not become a shorter version
of themselves, they become one line each. That is said out loud — in the preview
before anything changes and in the result after — together with where the full
text still lives: a `pre-compact` copy written *before* the transcript is touched,
never after. A destructive edit that leaves no way back is the bug class this
project hates most, so the order is fixed: plan, back up, mutate, save, and undo
the mutation in memory if the save itself fails.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from beeagent.i18n import L
from beeagent.core.context import MIN_HISTORY_BUDGET, QUESTION_MIN_TOKENS, ContextManager
from beeagent.core.session import Message, Session
from beeagent.utils.tokens import count_tokens

# Turns kept word for word. A turn is one exchange: a user message and everything
# that followed it until the next user message. A session too short in exchanges
# to fold — the classic one question and forty tool rounds — folds inside the
# exchange, in units of *steps*, which is what `ContextManager._groups` calls the
# pair of a tool call and the results that answered it. Keeping that pairing whole
# is not a style choice: a call that reaches the wire with no result under it is a
# malformed request the provider refuses for the rest of the session's life.
DEFAULT_KEEP = 8
MAX_KEEP = 500

# The least `_digest` will write an outline with; a window that cannot pay it gets
# a refusal rather than a fold that deletes the thread.
DIGEST_FLOOR = 96

BACKUP_SUFFIX = ".pre-compact.json"

# The role the digest rides in as. `build_messages` puts a trim summary in the
# *system* message, so this is the same kind of row in the same place; as `user`
# it would be an instruction the user never typed, which is the injection the
# `<bee-data>` fence exists to stop.
DIGEST_ROLE = "system"

# What `build_messages` writes on the way out. A preview must not leave a counter
# behind: the UI reads `trimmed` after the loop's own build, and a stale number
# from a preview would be reported as though the last turn had trimmed.
_BUILD_STATE = ("trimmed", "dropped", "clipped", "trimmed_tokens", "shed", "_msg_cap")

# The line `_digest` writes when even the summary cannot afford one line per
# dropped message. Both languages, because the digest is written in whichever one
# the interface is set to and this has to be readable in either.
_OMITTED_RE = re.compile(r"\((\d+) (?:of them are left out|из них опущены)")

YES_WORDS = ("yes", "y", "да", "д", "confirm", "run", "do")
NO_WORDS = ("no", "n", "preview", "dry")


class CompactError(Exception):
    """/compact found a reason to do nothing. The message is the answer, in words."""


@dataclass
class Fold:
    """Where a transcript would be cut, as group and message indices.

    `unit` says what `/compact <N>` counted. Normally N *turns* — exchanges, a
    user message and everything that followed it. When the session is too few
    exchanges to fold but one exchange is enormous (one question, forty tool
    rounds, the shape that eats a free tier), it counts N *steps* instead, which
    is `ContextManager._groups`' unit: a call and the results that answered it.
    """

    unit: str = "turn"
    groups: int = 0                 # steps in the session
    turns: int = 0                  # exchanges in the session
    keep_from: int = 0              # the group the verbatim tail starts at
    kept_turns: int = 0
    folded_turns: int = 0
    kept_steps: int = 0
    folded_steps: int = 0
    kept_index: list = field(default_factory=list)
    folded_index: list = field(default_factory=list)


@dataclass
class Plan:
    """Everything a fold would change, worked out before any of it happens."""

    session_id: str
    model: str
    keep: int
    unit: str = "turn"               # what `keep` counted: turns or steps
    turns: int = 0                   # exchanges in the session
    steps: int = 0                   # conversation units, in the trimmer's sense
    messages: int = 0                # rows in the session
    turns_folded: int = 0
    turns_kept: int = 0
    steps_folded: int = 0
    steps_kept: int = 0
    fold_messages: int = 0
    keep_messages: int = 0
    digest: str = ""
    folded: list = field(default_factory=list)
    kept: list = field(default_factory=list)
    kept_index: list = field(default_factory=list)
    before: int = 0                  # the request as the model receives it now
    after: int = 0                   # the same build over the folded transcript
    stored_before: int = 0           # the transcript as it is saved on disk
    stored_after: int = 0
    limit: int = 0                   # context.max_tokens, what /token prints beside it
    window: int = 0
    session_path: str = ""
    backup_path: str = ""

    @property
    def saved(self) -> int:
        return self.before - self.after

    @property
    def percent(self) -> int:
        if self.before <= 0:
            return 0
        return int(round(100 * self.saved / self.before))

    @property
    def fold_units(self) -> int:
        """How much of the conversation stops being readable word for word."""
        return self.turns_folded if self.unit == "turn" else self.steps_folded

    @property
    def keep_units(self) -> int:
        return self.turns_kept if self.unit == "turn" else self.steps_kept

    @property
    def digest_lines(self) -> int:
        return len([line for line in self.digest.splitlines() if line.strip()])

    @property
    def digest_omitted(self) -> int:
        """Folded rows the digest itself could not afford a line for.

        `_digest` says this in its own header ("(15 of them are left out below)")
        when the summary budget cannot pay for one line per dropped message. The
        report repeats it: a user told "32 turns become a summary" deserves to
        know that fifteen of them are not even in the summary.
        """
        match = _OMITTED_RE.search(self.digest)
        return int(match.group(1)) if match else 0

    def unit_words(self, plural: bool = False) -> tuple[str, str]:
        """(english, russian) for the unit this fold counted in."""
        if self.unit == "step":
            return ("steps", "шагов") if plural else ("step(s)", "шаг(а)")
        return ("turns", "реплик") if plural else ("turn(s)", "реплик(и)")


# --- the counting: the same ruler /token uses ---------------------------------

def transcript_tokens(rows: list[dict], model: str) -> int:
    """What the transcript costs as stored: the messages themselves, no header."""
    return sum(count_tokens(str(r.get("content") or ""), model) for r in rows or [])


def request_tokens(context: ContextManager, rows: list[dict], tool_schemas: list[dict],
                   native: bool = False) -> int:
    """What the next request would cost, exactly as `build_messages` sends it.

    Summed over the message contents rather than over `json.dumps(messages)`, the
    way `_cmd_token` does it: the escaping in a JSON dump is not something the
    model reads, and it charges ~3 tokens per Cyrillic letter for bytes that never
    travel.
    """
    messages = context.build_messages(rows, tool_schemas, native=native)
    return sum(count_tokens(str(m.get("content") or ""), context.model)
               for m in messages)


def _measured(context: ContextManager, rows, tool_schemas, native=False) -> int:
    """`request_tokens`, with the builder's counters left exactly as they were."""
    saved = {name: getattr(context, name, None) for name in _BUILD_STATE}
    try:
        return request_tokens(context, rows, tool_schemas, native)
    finally:
        for name, value in saved.items():
            setattr(context, name, value)


# --- the cut -------------------------------------------------------------------

def _group_offsets(groups: list[list[dict]]) -> list[int]:
    offsets, edge = [], 0
    for group in groups:
        offsets.append(edge)
        edge += len(group)
    return offsets


def plan_fold(context: ContextManager, rows: list[dict], keep: int) -> Fold:
    """Cut the transcript into what folds and what stays, or None if it cannot.

    Three of `build_messages`' own invariants are honoured here:

    * a tool call never parts from its result — the cut lands on a group edge, so
      a folded call goes into the digest together with what it returned;
    * the request being answered now is the floor of the conversation and is never
      folded, even where that means the kept set is not one suffix: losing the
      live question is what made a model answer as if the chat had just started;
    * the oldest content is what goes.

    `keep` counts exchanges when the session has more of them than it keeps. It
    counts steps only when the exchanges are too few to fold and one of them is
    bigger than the whole keep — the "one question, forty tool rounds" shape —
    because six plain question-and-answer pairs must be refused, not folded.
    """
    groups = context._groups(rows)
    if not groups:
        return None
    wanted = max(1, min(int(keep), MAX_KEEP))
    offsets = _group_offsets(groups)
    anchor = context._anchor_index(rows)
    anchor_group = max(index for index, off in enumerate(offsets) if off <= anchor)
    edges = [index for index, group in enumerate(groups)
             if str(group[0].get("role") or "") == "user"]
    bounds = edges + [len(groups)]
    longest = max((bounds[index + 1] - bounds[index] for index in range(len(edges))),
                  default=0)
    if len(edges) > wanted:
        unit, keep_from = "turn", edges[len(edges) - wanted]
    elif longest > wanted:
        unit, keep_from = "step", len(groups) - wanted
    else:
        return None

    kept_groups = list(range(keep_from, len(groups)))
    if anchor_group < keep_from:
        kept_groups = [anchor_group] + kept_groups
    kept_index = sorted(index for group in kept_groups
                        for index in range(offsets[group],
                                           offsets[group] + len(groups[group])))
    is_kept = set(kept_index)
    kept_steps = len(kept_groups)
    # Folding inside one exchange does not fold the exchange away: it stays, with
    # its middle gone, so the turn counters say nothing for that case and the step
    # counters say the truth.
    kept_turns = (len([edge for edge in edges if edge >= keep_from])
                  if unit == "turn" else len(edges))
    return Fold(unit=unit, groups=len(groups), turns=len(edges), keep_from=keep_from,
                kept_turns=kept_turns, folded_turns=len(edges) - kept_turns,
                kept_steps=kept_steps, folded_steps=len(groups) - kept_steps,
                kept_index=kept_index,
                folded_index=[index for index in range(len(rows))
                              if index not in is_kept])


def digest_for(context: ContextManager, folded: list[dict], kept: list[dict],
               tool_schemas: list[dict] = None, native: bool = False) -> tuple[str, int]:
    """The summary of the folded turns, written by the context layer itself.

    Sized the way `build_messages` sizes it — `digest_room` over the history
    budget — and further capped by what the kept turns leave, so a manual fold
    cannot hand back a digest that pushes the fresh request over the window. The
    empty text it can return means the window has no room for an outline at all,
    and the caller refuses instead of deleting.
    """
    tool_schemas = tool_schemas or []
    anchor = context._anchor_index(kept)
    question = str(kept[anchor].get("content") or "") if anchor >= 0 else ""
    # The same floor the header is shed to: the question being answered is what
    # the digest must not crowd out.
    need = min(context._cost(question), QUESTION_MIN_TOKENS)
    base, reminder, _shed = context._fit_header(native, tool_schemas, need)
    budget = max(context.max_tokens - context._cost(base) - context._cost(reminder),
                 MIN_HISTORY_BUDGET)
    header = _measured(context, [], tool_schemas, native)
    kept_cost = max(0, _measured(context, kept, tool_schemas, native) - header)
    # What the kept turns leave is the whole question. Clamping it UP to the floor
    # made the refusal below unreachable: with a 1024-token window and eight kept
    # turns there was no room at all, and `/compact` wrote a 370-character digest
    # and deleted 32 turns to pay for it. A floor is a minimum worth writing, not
    # a loan against a budget that is already spent.
    available = int(budget - kept_cost)
    if available < DIGEST_FLOOR:
        return "", max(0, available)
    room = min(context.digest_room(budget), available)
    return context._digest(folded, int(room), base), int(room)


def plan_compaction(session, keep: int = DEFAULT_KEEP, *, context: ContextManager = None,
                    tool_schemas: list[dict] = None, native: bool = False,
                    workdir: str = ".") -> Plan:
    """Work out what a fold would do. Writes nothing and changes nothing."""
    context = context or ContextManager()
    tool_schemas = tool_schemas or []
    rows = _rows(session)
    if not rows:
        raise CompactError(L(
            "Nothing to compact: this session holds no turns yet.",
            "Сжимать нечего: в этой сессии пока нет реплик."))

    wanted = max(1, min(int(keep), MAX_KEEP))
    rows_count = len(rows)
    steps_count = len(context._groups(rows))
    turns_count = len([row for row in rows if str(row.get("role") or "") == "user"])
    fold = plan_fold(context, rows, wanted)
    if fold is None:
        raise CompactError(L(
            f"Nothing to gain: this session is {turns_count} turn(s) in "
            f"{rows_count} message(s) and {steps_count} step(s), and /compact keeps "
            f"the last {wanted} verbatim anyway. It starts to pay for itself once the "
            "transcript holds more than that — try /compact <N> with a smaller N.",
            f"Выгоды нет: в сессии реплик(и) {turns_count}, сообщений {rows_count}, "
            f"шагов {steps_count}, а /compact оставляет последние {wanted} дословно. "
            "Он окупается, когда запись длиннее; попробуй /compact <N> с меньшим N."))

    folded = [rows[index] for index in fold.folded_index]
    kept = [rows[index] for index in fold.kept_index]
    digest, room = digest_for(context, folded, kept, tool_schemas, native)
    if not digest:
        raise CompactError(L(
            f"Refused: with a {context.max_tokens}-token budget an outline of the "
            f"folded turns would fit in {room} token(s), and the digest builder writes "
            "nothing that small — /compact would only delete the thread. Raise "
            "max_context_tokens, or /export the session and /reset it.",
            f"Отказ: при бюджете в {context.max_tokens} токенов на сводку остаётся "
            f"{room} ток.(а), и конспект таких размеров не пишет — /compact просто "
            "стёр бы нить. Подними max_context_tokens или сохрани сессию через "
            "/export, а потом /reset."))

    after_rows = [{"role": DIGEST_ROLE, "content": digest}] + kept
    return Plan(
        session_id=str(getattr(session, "session_id", "")),
        model=context.model,
        keep=wanted,
        unit=fold.unit,
        turns=fold.turns,
        steps=fold.groups,
        messages=len(rows),
        turns_folded=fold.folded_turns,
        turns_kept=fold.kept_turns,
        steps_folded=fold.folded_steps,
        steps_kept=fold.kept_steps,
        fold_messages=len(folded),
        keep_messages=len(kept),
        digest=digest,
        folded=folded,
        kept=kept,
        kept_index=fold.kept_index,
        before=_measured(context, rows, tool_schemas, native),
        after=_measured(context, after_rows, tool_schemas, native),
        stored_before=transcript_tokens(rows, context.model),
        stored_after=transcript_tokens(after_rows, context.model),
        limit=context.max_tokens,
        window=context.window,
        session_path=str(session_path(session, workdir)),
        backup_path=str(backup_path(session, workdir)),
    )


def apply_compaction(session, keep: int = DEFAULT_KEEP, *, context: ContextManager = None,
                     tool_schemas: list[dict] = None, native: bool = False,
                     workdir: str = ".") -> Plan:
    """Fold, then save through the session's own path.

    Nothing moves until the pre-fold transcript is safely on disk, and a failed
    save puts the in-memory messages back exactly as they were: the user's view of
    the conversation never gets ahead of, or behind, the file.
    """
    plan = plan_compaction(session, keep, context=context, tool_schemas=tool_schemas,
                           native=native, workdir=workdir)
    originals = list(getattr(session, "messages", []) or [])
    try:
        backup = write_backup(session, plan, workdir)
    except OSError as e:
        raise CompactError(L(
            f"Nothing was changed: I cannot write the backup copy at "
            f"{plan.backup_path} ({e.__class__.__name__}) — the full transcript has "
            "to survive the fold somewhere before I touch it.",
            f"Ничего не изменено: не могу записать резервную копию в "
            f"{plan.backup_path} ({e.__class__.__name__}) — полный текст должен "
            "пережить сворачивание ещё до правки.")) from e
    plan.backup_path = str(backup)

    try:
        survivors = [originals[index] for index in plan.kept_index]
        session.messages[:] = [Message(role=DIGEST_ROLE, content=plan.digest)] + survivors
        session.save(workdir)
    except OSError as e:
        session.messages[:] = originals
        raise CompactError(L(
            f"The session file {plan.session_path} could not be written "
            f"({e.__class__.__name__}) — the conversation is unchanged in memory, "
            f"and a copy of it is at {backup}.",
            f"Файл сессии {plan.session_path} записать не вышло "
            f"({e.__class__.__name__}) — разговор в памяти не тронут, "
            f"копия лежит в {backup}.")) from e
    return plan


def _rows(session) -> list[dict]:
    """The transcript as plain rows, whatever handed it over: a Session or a list."""
    if session is None:
        return []
    if isinstance(session, Session):
        return session.to_dicts()
    if isinstance(session, list):
        return [dict(row) for row in session]
    to_dicts = getattr(session, "to_dicts", None)
    return list(to_dicts()) if callable(to_dicts) else []


# --- where the text survives --------------------------------------------------

def sessions_dir(workdir: str = ".") -> Path:
    return Path(workdir) / ".beeagent" / "sessions"


def session_path(session, workdir: str = ".") -> Path:
    """The file `Session.save` writes — recomputed here because the user has to be
    told which path a backup goes back over."""
    return sessions_dir(workdir) / f"{getattr(session, 'session_id', 'session')}.json"


def backup_path(session, workdir: str = ".") -> Path:
    """`<id>.pre-compact.json`, beside the session file.

    Named like a session and stored next to one on purpose, but its JSON carries
    no top-level `messages` key — see `write_backup`. `Session.list_sessions`
    offers every `*.json` whose payload has messages in it, and a backup that
    answered to that shape would show up in `/sessions` and win `--continue`, so
    the user would resume yesterday's pre-fold transcript believing it was today's.
    """
    return sessions_dir(workdir) / (
        f"{getattr(session, 'session_id', 'session')}{BACKUP_SUFFIX}")


def write_backup(session, plan: Plan, workdir: str = ".") -> Path:
    """The whole pre-fold transcript, written before anything is edited."""
    path = backup_path(session, workdir)
    payload = {
        "kind": "beecode-pre-compact",
        "session": plan.session_id,
        "created_at": getattr(session, "created_at", ""),
        "compacted_at": datetime.now().isoformat(timespec="seconds"),
        "model": plan.model,
        "unit": plan.unit,
        "kept_units": plan.keep_units,
        "folded_units": plan.fold_units,
        "kept_turns": plan.turns_kept,
        "folded_turns": plan.turns_folded,
        "kept_steps": plan.steps_kept,
        "folded_steps": plan.steps_folded,
        "digest_lines_left_out": plan.digest_omitted,
        "folded_messages": plan.fold_messages,
        "request_tokens_before": plan.before,
        "request_tokens_after": plan.after,
        "digest": plan.digest,
        # Not "messages": see `backup_path`.
        "transcript": [dict(row) for row in (plan.folded + plan.kept)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


# --- what the user reads -------------------------------------------------------

def _pct(part: int, whole: int) -> int:
    return int(100 * part / whole) if whole else 0


def _digest_sample(digest: str, take: int = 3) -> list[str]:
    """The digest's first lines: enough to judge what the thread will remember."""
    parts = [line for line in digest.splitlines() if line.strip()]
    shown = parts[:1] + parts[1:1 + take]
    if len(parts) > len(shown):
        shown.append(f"... {len(parts) - len(shown)} more line(s)")
    return shown


def _inside_a_turn(plan: Plan) -> str:
    """Why the fold had to cut steps and not whole exchanges — said, not implied."""
    if plan.unit != "step":
        return ""
    return L(f"  (this session is {plan.turns} exchange(s) and only {plan.keep} of "
             f"them would fit the keep, but the one exchange runs to {plan.steps} "
             "steps — so it folds inside it. A tool call and its result still travel "
             "together, and the question you asked stays word for word.)",
             f"  (обменов в сессии {plan.turns}, а оставить просили {plan.keep} — "
             f"зато один обмен растянулся на {plan.steps} шаг(ов), поэтому сворачиваем "
             "внутри него. Вызов инструмента и его результат всё равно идут вместе, "
             "и твой вопрос остаётся дословно.)")


def preview_lines(plan: Plan) -> list[str]:
    """The preview block: what a fold would cost, what it would lose, what to type."""
    word, ru_word = plan.unit_words()
    lines = [L("/compact — a preview. Nothing has been changed.",
               "/compact — предпросмотр. Ничего не изменено."), ""]
    lines.append(L(f"session {plan.session_id}: {plan.turns} turn(s), "
                   f"{plan.steps} step(s), {plan.messages} message(s)",
                   f"сессия {plan.session_id}: реплик {plan.turns}, шагов "
                   f"{plan.steps}, сообщений {plan.messages}"))
    lines.append(L(f"as the model receives it now: {plan.before} token(s) of a "
                   f"{plan.limit} budget ({_pct(plan.before, plan.limit)}%); the "
                   f"transcript as saved: {plan.stored_before} token(s)",
                   f"как модель получает сейчас: {plan.before} ток. из бюджета "
                   f"{plan.limit} ({_pct(plan.before, plan.limit)}%); сама запись: "
                   f"{plan.stored_before} ток."))
    lines.append("")
    lines.append(L(f"would fold: the {plan.fold_units} oldest {word} "
                   f"({plan.fold_messages} message(s)) into one digest line each",
                   f"свернутся: {plan.fold_units} самых старых {ru_word} "
                   f"({plan.fold_messages} сообщ.) в построчный конспект"))
    if _inside_a_turn(plan):
        lines.append(_inside_a_turn(plan))
    lines.append(L(f"stays word for word: the last {plan.keep_units} {word} "
                   f"({plan.keep_messages} message(s))",
                   f"останется дословно: последние {plan.keep_units} {ru_word} "
                   f"({plan.keep_messages} сообщ.)"))
    lines.append(L("the digest would say:", "конспект говорил бы:"))
    lines.extend(f"    {line}" for line in _digest_sample(plan.digest))
    saving = (L(f", {plan.percent}% saved", f", экономия {plan.percent}%")
              if plan.saved > 0 else L(" — no saving", " — выгоды нет"))
    lines.append(L(f"after: {plan.before} -> {plan.after} token(s)" + saving,
                   f"после: {plan.before} -> {plan.after} ток." + saving))
    lines.append("")
    lines.append(_loss_line(plan))
    lines.append(L(f"  the words are not gone: a copy of the whole transcript goes to "
                   f"{plan.backup_path}, and the session file is rewritten with the "
                   "digest in it",
                   f"  текст не исчезает: копия всей записи ляжет в "
                   f"{plan.backup_path}, а файл сессии перезапишется с конспектом"))
    lines.append(L("  this preview touched no file and asked nothing of a model",
                   "  этот предпросмотр не тронул файлов и не спросил ни у какой "
                   "модели"))
    lines.append(L("run it:  /compact yes        (or /compact <N> yes to keep N turns)",
                   "выполни:  /compact yes      (или /compact <N> yes, оставить "
                   "N реплик)"))
    return lines


def result_lines(plan: Plan) -> list[str]:
    """What a finished fold did: both numbers, and where the words went."""
    word, ru_word = plan.unit_words()
    lines = [L(f"folded {plan.fold_units} {word} ({plan.fold_messages} message(s)) "
               f"into a digest — the last {plan.keep_units} stay word for word",
               f"свернуто {plan.fold_units} {ru_word} ({plan.fold_messages} сообщ.) в "
               f"конспект — последние {plan.keep_units} остаются дословно")]
    if _inside_a_turn(plan):
        lines.append(_inside_a_turn(plan))
    lines.append(L(f"request: {plan.before} -> {plan.after} tokens "
                   + (f"({plan.percent}% saved)" if plan.saved > 0
                      else "(no saving: the digest costs as much as the turns it "
                           "replaced)"),
                   f"запрос: {plan.before} -> {plan.after} ток. "
                   + (f"(экономия {plan.percent}%)" if plan.saved > 0
                      else "(выгоды нет: конспект стоит столько же, сколько "
                           "заменённые реплики)")))
    lines.append(L(f"saved transcript: {plan.stored_before} -> {plan.stored_after} "
                   f"tokens, {plan.messages} -> {plan.keep_messages + 1} messages",
                   f"запись: {plan.stored_before} -> {plan.stored_after} ток., "
                   f"{plan.messages} -> {plan.keep_messages + 1} сообщ."))
    lines.append("")
    lines.append(_loss_line(plan))
    lines.append(L(f"  full pre-fold transcript: {plan.backup_path}",
                   f"  полный текст до сворачивания: {plan.backup_path}"))
    lines.append(L(f"  to undo: stop BeeCode, copy that file over {plan.session_path} "
                   "and start again",
                   f"  отменить: останови BeeCode, скопируй тот файл поверх "
                   f"{plan.session_path} и запусти заново"))
    lines.append(L("nothing left this machine: no request was made, no quota spent.",
                   "ничего не уходило наружу: запрос не делался, квота не тронута."))
    return lines


def _loss_line(plan: Plan) -> str:
    """The sentence that says what is no longer there, in numbers.

    It counts the units the summary replaces, not the characters: the model still
    sees one line per folded turn, so "41 turns become a summary" is the true
    shape of the loss. `plan.digest_omitted` is added when even the summary has to
    leave rows out, because that is a second, smaller loss on top of the first.
    """
    word, ru_word = plan.unit_words(plural=True)
    more = ""
    if plan.digest_omitted:
        more = L(f" ({plan.digest_omitted} of the folded messages are not even in the "
                 "summary: it could not afford a line for each)",
                 f" ({plan.digest_omitted} свёрнутых сообщений не попадут и в сводку: "
                 "строки на все не хватило)")
    return L(f"what is no longer verbatim: the model will read a summary of "
             f"{plan.fold_units} earlier {word}, not the {word} themselves — one line "
             f"each of who spoke and what they got{more}.",
             f"чего больше нет дословно: модель прочитает сводку по {plan.fold_units} "
             f"предыдущих {ru_word}, а не по самим {ru_word} — по строке на каждую"
             f"{more}.")


def usage_line() -> str:
    return L("usage: /compact [N] — preview · /compact yes [N] — fold, keeping the "
             "last N turns (default 8)",
             "сборка: /compact [N] — предпросмотр · /compact yes [N] — свернуть, "
             "оставив последние N реплик (по умолчанию 8)")


# --- the command ---------------------------------------------------------------

COMMAND_NAME = "compact"
USAGE = "/compact [N] | /compact yes [N]"
DESCRIPTION = "Fold the oldest turns into a digest now (no model call, no quota)"


def register_command() -> bool:
    """Put `/compact` into the shared command machinery, the way `/trust` does.

    Both interfaces go through `commands.dispatch`, so one registration covers the
    classic REPL, the Textual TUI (the sidebar reads COMMANDS at mount) and
    one-shot runs. The category is a core one, so `drop_command` — which only ever
    takes back what a plugin contributed — cannot remove it.
    """
    from beeagent.ui import commands as core

    existing = next((c for c in core.COMMANDS if c.name == COMMAND_NAME), None)
    if existing is not None and core.HANDLERS.get(COMMAND_NAME) is _cmd_compact:
        return True
    if existing is None:
        core.add_command(COMMAND_NAME, DESCRIPTION, usage=USAGE, category="session")
    core.HANDLERS[COMMAND_NAME] = _cmd_compact
    return True


def _cmd_compact(ctx, args):
    """`/compact` previews, `/compact yes` folds, `/compact <N>` sets what stays."""
    from beeagent.ui.commands import CommandResult
    from rich.text import Text

    try:
        keep, apply_now = _parse_args(args)
    except ValueError:
        return CommandResult(output=Text(usage_line(), style="bold"))

    session = getattr(ctx, "session", None)
    if not getattr(session, "messages", None):
        return CommandResult(output=Text(L(
            "Nothing to compact: this session holds no turns yet.",
            "Сжимать нечего: в этой сессии пока нет реплик."), style="bold"))

    context, tool_schemas, native, workdir = _wiring(ctx)
    try:
        if apply_now:
            plan = apply_compaction(session, keep, context=context,
                                    tool_schemas=tool_schemas, native=native,
                                    workdir=workdir)
            return CommandResult(output=Text("\n".join(result_lines(plan)), style="bold"))
        plan = plan_compaction(session, keep, context=context,
                               tool_schemas=tool_schemas, native=native, workdir=workdir)
        return CommandResult(output=Text("\n".join(preview_lines(plan))))
    except CompactError as e:
        return CommandResult(output=Text(str(e), style="bold"))


def _parse_args(args: list[str]) -> tuple[int, bool]:
    """`/compact`, `/compact 12`, `/compact yes`, `/compact yes 12`."""
    keep, apply_now = DEFAULT_KEEP, False
    for word in args or []:
        low = str(word).strip().lower()
        if low in YES_WORDS:
            apply_now = True
            continue
        if low in NO_WORDS:
            apply_now = False
            continue
        if low.isdigit():
            number = int(low)
            if number < 1:
                raise ValueError("keep at least one turn")
            keep = min(number, MAX_KEEP)
            continue
        raise ValueError(f"not understood: {word}")
    return keep, apply_now


def _wiring(ctx):
    """The builder, the tool list and the folder this session's file lives in.

    Read off the running agent so the preview prices the same request the loop
    would send — the same system prompt, the same catalogue, the same native
    choice. With no agent (a script, a one-shot run) a bare ContextManager over the
    configured model answers just as well: this command needs no endpoint.
    """
    agent = getattr(ctx, "agent", None)
    config = getattr(ctx, "config", None) or getattr(agent, "config", None)
    context = getattr(agent, "context", None)
    if context is None:
        context = ContextManager(model=str(getattr(config, "model", "") or "g4f-model"),
                                 window=getattr(config, "max_context_tokens", 0) or None)
    try:
        tool_schemas = list(agent.tools.to_schemas())
    except Exception:
        tool_schemas = []
    native = False
    try:
        provider = agent.providers.get(str(getattr(config, "provider", "") or "g4f"))
        native = bool(getattr(provider, "supports_tools", False)) \
            and bool(getattr(config, "native_tools", False))
    except Exception:
        native = False
    workdir = str(getattr(agent, "workdir", None) or getattr(ctx, "workdir", None) or ".")
    return context, tool_schemas, native, workdir
