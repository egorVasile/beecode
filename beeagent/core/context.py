"""BeeCode context builder with forceful tool-use contract.

Weak free g4f models often ignore the `system` role, so the tool contract
is injected twice: in the system message AND appended to the last user
message, where every model pays maximum attention.

Two invariants hold this file together:

* The request being worked on always travels. If it cannot fit, it is
  shrunk — never dropped.
* Nothing is ever dropped silently. Messages that do not fit are compressed
  into a digest that rides along in the system message, so a small-context
  model still sees the shape of the conversation instead of a blank page.
"""
import re

from beeagent.i18n import L
from beeagent.utils.tokens import count_tokens

SYSTEM_PROMPT = """You are BeeCode, an autonomous AI coding agent running on the user's machine.
You are not a chatbot: you have a real terminal and real files, and you use them.

# TOOL CALL FORMAT (MANDATORY)
To use a tool, answer with ONLY a JSON code block and nothing else:

```json
{"tool": "tool_name", "args": {"param": "value"}}
```

- One tool call per answer. Wait for its result, then decide the next step.
- No prose around the block: when you call a tool, the block is the whole answer.
- Only the tools in the catalog below exist. Never invent a tool name or an argument.
- NEVER say you cannot read or write files, and never ask the user to paste file contents. You can.

# AFTER A TOOL RUNS
The result returns as a message starting with `[tool result]`. Act on it: call another tool, or,
when the task is finished, answer in plain text with no JSON.

# HOW TO THINK
- Before acting, settle four things: what the user actually wants; what this code is supposed to
  do, judging by its names and layout; what you already know versus what you must look up; the
  smallest change that solves it and how you will check it.
- Ambiguity is not a reason to stall. Take the most useful reading, state the assumption in one
  line, and keep moving. Ask only when the answer changes what you would do.
- After each tool result, compare it with what you expected. A surprise means stop and rethink —
  do not keep executing a plan that has just proved wrong.
- Choose the simplest thing that is correct: no abstraction for one caller, no handling for cases
  that cannot happen, no half-finished implementations left behind.

# LOOK BEFORE YOU CLAIM
- `list_directory` shows a folder (subdirectories end with `/`); `read` opens one file, with
  offset/limit for long ones; `glob` finds files by name; `grep` finds text inside them;
  `bash` does anything else a shell can do; `git` handles version control.
- A request that mentions a file does not prove the file exists. Look.
- Read a file before editing it, and replace text exactly as it appears.
- Verify with a tool before reporting: run the tests, read the file back, check `git status`.
  Never claim an edit, a build or a test result you did not observe.

# TOOL USAGE POLICY
- Each tool's own description says how to use it. Follow it rather than improvising.
- Files go through `read`, `write`, `edit`, `grep`, `glob`, `list_directory` — never through shell
  plumbing. `bash` is for running things: builds, tests, installs, docker, git.
- Lookups that do not depend on each other go in one answer as several calls. Steps that do
  depend on a result wait for it.
- Before creating a file or directory, check where it goes with `list_directory`; quote paths
  that contain spaces; never fake two calls by splitting one command over newlines.
- Git: commit, amend, push and open PRs only when the user asks. Before committing look at
  status, diff and recent log; stage only what you changed; never commit secrets; never
  force-push, skip hooks, rewrite published history or edit git config on your own initiative.
- If a command fails, read the error and fix the cause; do not retry it unchanged or bypass it.

# WORK DISCIPLINE
- Do the task, not the description of how it could be done. For read-only and clearly reversible
  steps, act without asking — the user asked you to work, not to propose.
- Finish what was asked before offering anything extra. No unrequested refactors, no speculative
  features, no "while I was in there".
- Never commit, push or publish unless the user explicitly asks. They decide when work is committed.
- Stop when the task is done. Do not summarise what you did or paste back code you already showed.
- Before anything destructive or hard to reverse: deleting files you did not create, `rm -rf`,
  `git reset --hard`, force pushes, dropping tables, rewriting history, editing secrets.
- When a command changes the user's system and is not obvious, say in one line what it does and
  why — before running it, not after.
- Never put API keys, tokens or passwords into files, commits, commands or logs, and never send
  them anywhere. If one is already in a file, say so instead of echoing it.
- When you get something wrong: say what went wrong, fix it, move on. One sentence of
  accountability — no self-criticism, no apology spiral. Stay steady if the user is rude.
- If you do not know, say you do not know. Never guess file contents, URLs, versions or API names.
- Anything that may have changed since training (current versions, prices, news): use
  `web_search` first, and say which parts you verified.
- Tools are for working, not for talking: no messages through echo, printf or code comments.

# WORKING ON SOMEONE ELSE'S CODE
- Search widely before writing: a wrong assumption costs more than three extra lookups.
- Never assume a library, framework or script is available. Check imports, the manifest or
  lockfile, neighbouring files, and whatever the project documents (README, AGENTS.md) — then
  match the conventions you actually saw: naming, error handling, comment density, test style.
- Do not add comments to code unless the user asks, or the file is already commented that way.
- Verify with the project's own tools: find how it runs tests and lint, then run them. Never
  invent the command; if you cannot find it, ask once and suggest writing it down.
- Report what you observed, not what you intended. If a check failed, say so.

# STYLE
- Reply in the language the user writes in. Code, comments and commit messages follow whatever
  the project already uses.
- Be concise: on the command line, four short lines usually beat a paragraph. Expand only when
  the user asks for detail or when the work itself needs explaining.
- Every sentence must add something: no preamble, no recap of the request, no filler, no
  "here is what I will do next".
- Do not quote the user's message back at them, and do not use emojis unless they do.
- Use lists and headings only when the content is genuinely multi-part; plain prose otherwise.
  Keep caveats short and put the answer first.
- Across a long run of tool calls, one short progress sentence every few calls is enough.
- Avoid "genuinely", "honestly", "straightforward". State the point instead of selling it.
- Point at code as `path:line` so the user can jump straight to it.
"""


# Short reinforcement appended to the last user message every turn.
REMINDER_SUFFIX = (
    "\n\n---\n"
    "[SYSTEM: You are BeeCode, an autonomous coding agent on this machine with real filesystem "
    "and shell access. Use the tools in your system prompt. To call one, answer with ONLY:\n"
    '```json\n{"tool": "tool_name", "args": {...}}\n```'
    "\nDo not say you lack file access — you do not lack it. Do not ask the user to paste file "
    "contents. Look, act, and verify with a tool before you claim anything.]"
)


# How much of the window the reply may have; the request must leave room for it.
REPLY_RESERVE_RATIO = 8          # window // 8
REPLY_RESERVE_MIN = 512

# What we assume for a model we cannot recognise. Deliberately small: guessing
# high is what sent oversized requests to the endpoint, which trimmed the
# history itself and the model answered as if the chat had just started.
DEFAULT_WINDOW = 8192

# Even a 1M-context model is not sent an unlimited prompt: the tool catalog,
# skills and huge dumps make cost grow fast.
MAX_WINDOW = 32768

# A measured limit may be trusted further than a guess from the model name.
MEASURED_MAX_WINDOW = 262144

# The "… N tokens truncated …" marker is part of a clipped message's cost.
CLIP_MARKER_TOKENS = 40

_WINDOW_MARKERS = re.compile(r"(\d+(?:\.\d+)?)\s*k\b")
_WINDOW_MARKERS_M = re.compile(r"(\d+(?:\.\d+)?)\s*m\b")

_MODEL_FAMILIES = (
    ("gpt-4.1", 128000), ("gpt-4o", 128000), ("gpt-4-turbo", 128000),
    ("gpt-4", 8192), ("gpt-3.5", 16385),
    ("claude", 200000), ("gemini", 1000000),
    ("llama-3", 8192), ("llama3", 8192), ("mistral-nemo", 128000),
    ("qwen2.5", 32768), ("qwen", 32768), ("glm-4", 8192), ("deepseek", 65536),
)


def window_for(model: str) -> int:
    """The context window a model id advertises, conservative when unknown.

    A measured value wins: it came from the endpoint refusing a real prompt,
    while everything below is a guess from the model's name.
    Sizes written into the id take precedence over the family table
    ("llama-3.1-8b-128k", "glm-4-9b-32k"); the "8b" form is parameter count,
    not window, so only k/m markers are read.
    """
    from beeagent.core import windows

    measured = windows.measured(model or "")
    if measured:
        return max(1024, min(MEASURED_MAX_WINDOW, measured))
    name = (model or "").lower()
    for pattern, unit in ((_WINDOW_MARKERS, 1024), (_WINDOW_MARKERS_M, 1024 * 1024)):
        match = pattern.search(name)
        if match:
            try:
                return max(1024, min(MAX_WINDOW, int(float(match.group(1)) * unit)))
            except ValueError:
                continue
    for token, size in _MODEL_FAMILIES:
        if token in name:
            return max(1024, min(MAX_WINDOW, size))
    return DEFAULT_WINDOW


class ContextManager:
    def __init__(self, max_tokens: int = None, model: str = "gpt-4", window: int = None):
        self.model = model
        # A configured budget is a ceiling, not a pin: the model's own window
        # decides the size, so a 4k model gets a 4k-sized request instead of a
        # 12000-token payload the endpoint would trim behind our back.
        self._window_cap = window or max_tokens
        # Optional extra system-prompt section (installed skills).
        self.skills_section: str | None = None
        # What the model may do without asking (see core/permissions.py).
        self.permissions_section: str | None = None
        # How many messages the last build had to compress into the digest.
        self.trimmed = 0

    # One big tool output (a file read, a recursive glob) must not evict the
    # whole conversation, so oversized messages are clipped instead.
    MAX_MSG_TOKENS = 2000

    # A digest line per message, tried widest first: only as short as needed.
    DIGEST_LINE_CHARS = (220, 140, 80, 40)

    @property
    def window(self) -> int:
        auto = window_for(self.model)
        return min(auto, self._window_cap) if self._window_cap else auto

    @property
    def max_tokens(self) -> int:
        """What the request may cost: the window minus room for the reply."""
        return max(1024, self.window - max(REPLY_RESERVE_MIN, self.window // REPLY_RESERVE_RATIO))

    @max_tokens.setter
    def max_tokens(self, value: int):
        self._window_cap = value

    def build_messages(self, session_messages: list[dict], tool_schemas: list[dict]) -> list[dict]:
        from beeagent.core.parser import CommandParser

        parser = CommandParser()

        # Tool catalog as a compact, explicit contract.
        tool_prompt = parser.format_tool_prompt(tool_schemas)
        base = SYSTEM_PROMPT + "\n" + tool_prompt
        if self.skills_section:
            base += "\n\n" + self.skills_section
        if self.permissions_section:
            base += "\n\n" + self.permissions_section

        # The reminder is appended to one message below, so it is paid for here.
        budget = self.max_tokens - self._cost(base) - self._cost(REMINDER_SUFFIX)

        # First try to carry the conversation verbatim.
        kept, dropped = self._window(session_messages, budget)
        digest = ""
        if dropped:
            # Something has to be summarised, and the summary itself takes
            # room. Reserving a fixed slice (rather than "whatever is left")
            # is what makes this settle: the digest is built to fit the slice,
            # so the window carved out by that cost is stable across passes.
            room = self.digest_room(budget)
            for _ in range(3):
                digest = self._digest(dropped, room, base)
                kept, dropped_next = self._window(
                    session_messages, max(0, budget - self._digest_cost(base, digest)))
                if len(dropped_next) == len(dropped):
                    break
                dropped = dropped_next
            # Cover whatever the final window actually left out.
            digest = self._digest(dropped, room, base)
        self.trimmed = len(dropped)

        messages = [{"role": "system", "content": f"{base}\n\n{digest}" if digest else base}]
        messages.extend(kept)

        # Reinforce the tool contract on the last user message — in a copy,
        # because these dicts belong to the caller's session: appending to them
        # in place made every build longer than the last.
        for position, msg in enumerate(reversed(messages)):
            if msg.get("role") == "user":
                index = len(messages) - 1 - position
                messages[index] = {**msg,
                                   "content": (msg.get("content") or "") + REMINDER_SUFFIX}
                break
        else:
            # Nothing but the system prompt survived — keep the contract there.
            messages[0]["content"] += REMINDER_SUFFIX

        return messages

    # --- window -----------------------------------------------------------

    def _window(self, session_messages: list[dict], budget: int) -> tuple[list[dict], list[dict]]:
        """Newest-first verbatim window; everything it cannot hold is returned.

        Messages that do not fit are skipped one by one rather than ending the
        scan, and the request being worked on is always pulled back in — losing
        it is what made the model answer as if the chat had just started.
        """
        anchor = self._anchor_index(session_messages)
        kept: list[dict] = []
        dropped: list[dict] = []
        used = 0

        for index in range(len(session_messages) - 1, -1, -1):
            msg = self._clip(session_messages[index])
            cost = self._cost(msg)
            room = budget - used
            if cost > room:
                if index == anchor or (not kept and index == len(session_messages) - 1):
                    # The live request is the one thing that must travel:
                    # shrink it instead of dropping it. The marker costs a
                    # little, so aim below the room to land inside it.
                    msg = self._clip(session_messages[index], max(1, room - CLIP_MARKER_TOKENS))
                    cost = self._cost(msg)
                else:
                    dropped.insert(0, msg)
                    continue
            kept.insert(0, msg)
            used += cost

        return kept, dropped

    @staticmethod
    def _anchor_index(session_messages: list[dict]) -> int:
        """The request being answered now: the last user message."""
        for index in range(len(session_messages) - 1, -1, -1):
            if session_messages[index].get("role") == "user":
                return index
        return len(session_messages) - 1 if session_messages else -1

    def _clip(self, msg: dict, tokens: int = None) -> dict:
        """Cut a message to `tokens` (default MAX_MSG_TOKENS), keeping head and tail."""
        from beeagent.utils.tokens import cut_tokens

        content = str(msg.get("content") or "")
        head, tail, removed = cut_tokens(content, tokens or self.MAX_MSG_TOKENS, self.model)
        if not removed:
            return msg
        marker = L(
            f"[… {removed} tokens truncated to fit the context window — "
            "run the tool again if you need the middle …]",
            f"[… {removed} токенов обрезано, чтобы влезть в контекст — "
            "перезапусти инструмент, если нужна середина …]",
        )
        return {**msg, "content": f"{head}\n{marker}\n{tail}"}

    # --- digest -----------------------------------------------------------

    def digest_room(self, budget: int) -> int:
        """Tokens the summary may take out of the history budget."""
        return max(256, min(1200, budget // 6)) if budget > 0 else 0

    def _digest(self, dropped: list[dict], budget: int, base: str = "") -> str:
        """A compressed outline of the turns that did not fit, in the system message.

        This is the answer to "the model forgot everything once and remembered
        after I repeated myself": a bare *N messages were dropped* leaves the
        model with no thread to pull, while the outline of what was asked and
        done keeps it working. Guaranteed to cost no more than `budget`.
        """
        if not dropped or budget < 96:
            return ""

        base_cost = self._cost(base) if base else 0

        def price(text: str) -> int:
            # Measured in place, not standalone: joining the summary onto the
            # system prompt costs more than the two parts added up.
            if not base:
                return self._cost(text)
            return max(0, self._cost(f"{base}\n\n{text}") - base_cost)

        for cap in self.DIGEST_LINE_CHARS:
            lines = [line for line in (self._entry(msg, cap) for msg in dropped) if line]
            text = self._digest_text(lines, len(dropped))
            if price(text) <= budget:
                return text

        # Even the shortest form is too much: keep what the conversation is
        # about (the opening request) and where it got to (the newest turns),
        # and say what was left out in between.
        lines = [line for line in (self._entry(msg, self.DIGEST_LINE_CHARS[-1])
                                   for msg in dropped) if line]
        full = price(self._digest_text(lines, len(dropped)))
        # Lines cost about the same, so estimate the count instead of walking
        # down it one pop at a time — the walk re-tokenised the whole digest
        # once per dropped message.
        keep = len(lines) if full <= budget else max(1, len(lines) * budget // max(1, full))
        chosen = lines[:1] + lines[max(1, len(lines) - keep + 1):]
        omitted = len(lines) - len(chosen)
        while chosen and price(self._digest_text(chosen, len(dropped), omitted)) > budget:
            if len(chosen) > 2:
                chosen.pop(1)
                omitted += 1
            else:
                chosen.pop()
                omitted += 1
        if not chosen:
            return ""
        return self._digest_text(chosen, len(dropped), omitted)

    def _digest_cost(self, base: str, digest: str) -> int:
        """What appending the summary to the system prompt actually costs.

        Tokenisation is not additive across the seam — measuring the digest on
        its own under-read it by ~10%, which put the finished request over the
        window and the endpoint trimmed history again.
        """
        if not digest:
            return 0
        return max(0, self._cost(f"{base}\n\n{digest}") - self._cost(base))

    @staticmethod
    def _digest_text(lines: list[str], total: int, omitted: int = 0) -> str:
        header = L(
            f"# CONVERSATION SO FAR — {total} earlier message(s) were compressed to fit the "
            "context window. This is the whole thread, not a fresh start; continue from here.",
            f"# ХОД РАЗГОВОРА — {total} более ранних сообщений сжаты, чтобы влезть в контекст. "
            "Это вся нить, а не новый чат; продолжай отсюда.",
        )
        if omitted:
            header += "\n" + L(f"({omitted} of them are left out below; the opening and the "
                               "latest turns are kept)",
                               f"({omitted} из них опущены; начало разговора и последние реплики "
                               "сохранены)")
        return header + "\n" + "\n".join(lines)

    @staticmethod
    def _entry(msg: dict, cap: int = 220) -> str:
        """One digest line: who spoke, and the smallest thing worth remembering."""
        role = str(msg.get("role") or "user")
        content = re.sub(r"\s+", " ", str(msg.get("content") or "")).strip()
        if not content:
            return ""

        label = {"user": "user", "assistant": "bee", "tool": "tool"}.get(role, role)
        if role == "tool":
            head = re.match(r"\[tool result\]\s*tool=(\w+)(.*?)(?:\n|$)", content)
            if head:
                failed = "error=True" in head.group(2)
                content = "→ " + head.group(1) + (L(" failed", " с ошибкой") if failed else "")
            else:
                content = "→ " + content
        elif role == "assistant":
            prose = re.sub(r"```(?:json)?[^`]*```", "", content).strip()
            call = re.search(r'"tool"\s*:\s*"(\w+)"', content)
            content = prose or (L("called ", "вызвала ") + call.group(1) if call else content)

        if len(content) > cap:
            content = content[:cap].rstrip() + "…"
        return f"{label}: {content}"

    # --- cost -------------------------------------------------------------

    def _cost(self, msg_or_text) -> int:
        text = (msg_or_text if isinstance(msg_or_text, str)
                else str(msg_or_text.get("content") or ""))
        return count_tokens(text, self.model)
