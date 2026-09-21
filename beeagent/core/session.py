import json
import os
import secrets
from pathlib import Path
from datetime import datetime

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
        # half a file — and then `--continue` cannot start at all. Replace atomically.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)

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
