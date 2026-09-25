import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from datetime import datetime

# --- tool output is data, never an instruction --------------------------------
#
# A tool result is the one part of the prompt the user did not write: it is a
# file, a web page, the stderr of a command. Without a boundary the model reads
# "[SYSTEM: ignore the earlier task]" inside a `bash` result the same way it
# reads an instruction from us — that exact string was injected through a tool
# result during the 2026-09-24 audit and BeeCode followed it.
#
# The tag is ours and it is spelled differently from the `[SYSTEM:` prefix
# BeeCode itself uses in a reminder, so a payload cannot imitate us by accident.
# Anything inside it that tries to look like an instruction is data too, and
# `frame_as_data` breaks a forged closing tag so the payload cannot step outside
# the fence it was put in.
DATA_TAG = "bee-data"
DATA_OPEN = f"<{DATA_TAG}>"
DATA_CLOSE = f"</{DATA_TAG}>"

# A tool result is only data when it came out of a tool: `[tool result]
# tool=read error=False` is the shape the loop builds for an executed call.
# BeeCode's own refusals ("Unknown tool", "needs path", a permissions message)
# share the `[tool result]` prefix but are instructions and stay unframed.
EXECUTED_RESULT = re.compile(r"^\s*\[tool result\]\s+tool=\w+\b")


def is_framed(content: str) -> bool:
    """Already inside a data fence — framing twice would cost tokens and lie."""
    text = (content or "").strip()
    return text.startswith(DATA_OPEN) and text.endswith(DATA_CLOSE)


def frame_as_data(content: str) -> str:
    """Wrap untrusted tool output so the model reads it as data, not as an order."""
    text = str(content or "")
    if is_framed(text):
        return text
    # Escape the opening angle bracket of any copy inside the payload: the tag
    # stops being the tag, so nothing written by a file or a web page can close
    # the fence early and carry on speaking as if it stood outside it.
    safe = (text.replace(f"</{DATA_TAG}", f"&lt;/{DATA_TAG}")
                .replace(f"<{DATA_TAG}", f"&lt;{DATA_TAG}"))
    return f"{DATA_OPEN}\n{safe}\n{DATA_CLOSE}"


def unframe(content: str) -> str:
    """The text without its fence, for anything that has to read what it says."""
    text = str(content or "")
    if not is_framed(text):
        return text
    body = text.strip()
    return body[len(DATA_OPEN):-len(DATA_CLOSE)].strip()


class Message:
    def __init__(self, role: str, content: str, tool_calls: list = None, tool_result: str = None):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls or []
        self.tool_result = tool_result
        self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        d = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_result:
            d["tool_result"] = self.tool_result
        return d

class Session:
    def __init__(self, session_id: str = None):
        # Seconds are not unique: two sessions started in the same second share an
        # id, and the second save quietly overwrites the first transcript.
        self.session_id = session_id or (
            datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3))
        self.messages: list[Message] = []
        self.created_at = datetime.now().isoformat()

    def add_user_message(self, content: str):
        self.messages.append(Message(role="user", content=content))

    def add_assistant_message(self, content: str, tool_calls: list = None):
        self.messages.append(Message(role="assistant", content=content, tool_calls=tool_calls))

    def add_tool_result(self, content: str):
        self.messages.append(Message(role="tool", content=content))

    def to_dicts(self) -> list[dict]:
        return [m.to_dict() for m in self.messages]

    def save(self, workdir: str = "."):
        path = Path(workdir) / ".beeagent" / "sessions" / f"{self.session_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "id": self.session_id,
            "created_at": self.created_at,
            "messages": [m.to_dict() for m in self.messages],
        }
        # Writing in place means a crash, a full disk or Ctrl+C mid-save leaves a
        # half a file — and then `--continue` cannot start at all. Replace atomically,
        # through a name only this save owns: two windows resumed on one session id
        # used to share `<id>.json.tmp`, and one of them could rename the other's
        # half-written temp over the transcript. Same lesson `save_config` learned.
        descriptor, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                                prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass

    @classmethod
    def load(cls, session_id: str, workdir: str = ".") -> "Session":
        path = Path(workdir) / ".beeagent" / "sessions" / f"{session_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"session {session_id} is unreadable ({e.__class__.__name__})") from e
        session = cls(session_id=data["id"])
        session.created_at = data["created_at"]
        for m in data["messages"]:
            session.messages.append(Message(
                role=m["role"],
                content=m["content"],
                tool_calls=m.get("tool_calls", []),
                tool_result=m.get("tool_result"),
            ))
        return session

    @classmethod
    def list_sessions(cls, workdir: str = ".") -> list[str]:
        """Readable session ids, oldest first — `--continue` takes the last one.

        A file that does not parse is skipped rather than offered: picking a torn
        transcript would only move the crash into `load`.
        """
        path = Path(workdir) / ".beeagent" / "sessions"
        if not path.exists():
            return []
        ids = []
        for candidate in sorted(path.glob("*.json")):
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("messages") is not None:
                ids.append(candidate.stem)
        return ids
