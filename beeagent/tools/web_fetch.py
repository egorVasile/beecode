"""Read one page, and report what the page actually was.

Without this tool a model answering "what does that docs page say" guesses from
training data. So the fetch has to be trustworthy where it matters: the bytes are
capped and the cap announced, HTML comes back as its text with scripts and
styles dropped, a PDF or an image is named and refused instead of arriving as an
empty string, and a 404 is reported as a 404.

The address is checked before the request goes out. Names are not resolved
first, so a public hostname pointing at an internal address is the one case this
misses; every literal an operator can type — `localhost`, `127.0.0.1`,
`169.254.169.254`, `10.x`, `192.168.x` — is refused, as is any scheme that is
not http(s), so `file:///etc/passwd` cannot ride in through a URL argument.
"""
import ipaddress
import re
import urllib.parse
from html.parser import HTMLParser

import httpx

from beeagent.i18n import L

from .base import BaseTool, ToolResult

MAX_RESPONSE_BYTES = 1_500_000      # bigger than this is a download, not a read
DEFAULT_TEXT_CHARS = 12_000
MAX_TEXT_CHARS = 40_000
TIMEOUT_SECONDS = 15.0
USER_AGENT = "BeeCode/1.0 (+https://github.com/egorVasile/beecode)"

_TEXT_TYPES = ("text/", "application/xhtml+xml", "application/xml", "text/xml",
               "application/json")
_DROP_TAGS = {"script", "style", "noscript", "svg", "template"}
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata",
                  "metadata.google.internal"}
_SPACE = re.compile(r"[ \t\f\v]+")


class _Page(HTMLParser):
    """The visible text of an HTML page and its <title>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._chunks: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "section"):
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = " ".join(data.split())[:200]
        if not self._skip and data.strip():
            self._chunks.append(data)

    def text(self, cap: int) -> str:
        raw = "".join(self._chunks)
        lines = (_SPACE.sub(" ", line).strip() for line in raw.splitlines())
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:cap]


def refuse_reason(url: str) -> str:
    """Why this address must not be fetched, or "" when it may be."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        return L(f"only http and https can be read, not “{parts.scheme or 'no scheme'}” — "
                 f"a local file is opened with the read tool, not through a URL",
                 f"читать можно только http и https, а не «{parts.scheme or 'без схемы'}» — "
                 f"локальный файл открывают инструментом read, а не через URL")
    host = (parts.hostname or "").strip().strip("[]").lower()
    if not host:
        return L("the address has no host to fetch", "в адресе нет хоста для запроса")
    if host in _BLOCKED_HOSTS or host.endswith((".local", ".internal")):
        return L(f"“{host}” is this machine — not fetched",
                 f"«{host}» — это эта машина, не запрашиваем")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""
    if (address.is_loopback or address.is_private or address.is_link_local
            or address.is_reserved or address.is_multicast):
        return L(f"{host} is an internal address (loopback, a private range, or a "
                 f"cloud metadata endpoint) — no request was sent",
                 f"{host} — внутренний адрес (петля, приватная сеть или метаданные "
                 f"облака): запрос не отправлялся")
    return ""


class WebFetchTool(BaseTool):
    name = "web_fetch"
    aliases = ("webfetch", "fetch", "read_url", "open_url")
    description = (
        "Fetch one http(s) page and return its text (title kept, scripts and "
        "styles dropped). Reports the status, the address it finally landed on "
        "and what was cut off. A binary or an oversized page is named and "
        "refused, not returned empty; internal addresses and non-http schemes "
        "are never requested."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL"},
            "max_chars": {"type": "integer",
                          "description": f"Text to return (default {DEFAULT_TEXT_CHARS}, "
                                         f"max {MAX_TEXT_CHARS})"},
        },
        "required": ["url"],
    }

    def execute(self, url: str = "", max_chars: int = DEFAULT_TEXT_CHARS) -> ToolResult:
        target = str(url or "").strip()
        if not target:
            return ToolResult(output=L("no url given", "не указан url"), error=True)
        refused = refuse_reason(target)
        if refused:
            return ToolResult(output=refused, error=True, metadata={"refused": True})
        try:
            cap = max(500, min(MAX_TEXT_CHARS, int(max_chars or DEFAULT_TEXT_CHARS)))
        except (TypeError, ValueError):
            cap = DEFAULT_TEXT_CHARS

        fetched = self._download(target)
        if isinstance(fetched, ToolResult):
            return fetched
        status, final_url, content_type, body, capped = fetched

        kind = content_type.split(";")[0].strip().lower()
        if status != 200:
            return ToolResult(
                output=L(f"HTTP {status} {response_reason(status)} from {final_url} "
                         f"({len(body)} bytes read{' — size capped' if capped else ''}). "
                         "The page gave nothing usable: check the address or take "
                         "another source.",
                         f"HTTP {status} {response_reason(status)} от {final_url} "
                         f"(прочитано {len(body)} байт"
                         f"{', объём обрезан' if capped else ''}). Страница ничего "
                         "пригодного не дала: проверьте адрес или возьмите другой "
                         "источник."),
                error=True, metadata={"status": status, "bytes": len(body)})
        if kind and not kind.startswith(_TEXT_TYPES) and "html" not in kind:
            return ToolResult(
                output=L(f"{final_url} is a {kind} file ({len(body)} bytes), not text. "
                         "If the user needs the file itself, download it with a shell "
                         "command.",
                         f"{final_url} — это файл {kind} ({len(body)} байт), не текст. "
                         "Если нужен сам файл, скачайте его командой в шелле."),
                error=True, metadata={"status": status, "content_type": kind})

        page = _Page()
        if "html" in kind or "xml" in kind or not kind:
            try:
                page.feed(body.decode("utf-8", "replace"))
                page.close()
                text = page.text(cap)
            except Exception as exc:
                return ToolResult(
                    output=L(f"the page arrived (HTTP {status}) but could not be read: "
                             f"{exc}",
                             f"страница пришла (HTTP {status}), но прочитать её не "
                             f"удалось: {exc}"),
                    error=True)
        else:
            text = _plain(body, cap)

        if not text.strip():
            return ToolResult(
                output=L(f"HTTP 200 and {len(body)} bytes from {final_url}, but the "
                         "page holds no readable text (scripts and styles are "
                         "dropped). It is probably drawn by JavaScript — this tool "
                         "does not run a browser.",
                         f"HTTP 200 и {len(body)} байт от {final_url}, но читаемого "
                         "текста нет (скрипты и стили отбрасываются). Скорее всего "
                         "страница рисуется JavaScript — этот инструмент не запускает "
                         "браузер."),
                error=False, metadata={"status": status, "chars": 0})

        notes = []
        if len(text) >= cap:
            notes.append(L(f"text cut at {cap} characters — raise max_chars for more",
                           f"текст обрезан на {cap} символах — поднимите max_chars"))
        if capped:
            notes.append(L(f"the body stopped at {MAX_RESPONSE_BYTES // 1024} KB",
                           f"тело оборвано на {MAX_RESPONSE_BYTES // 1024} КБ"))
        head = f"“{page.title}” — {final_url}" if page.title else final_url
        output = f"{head}\n{text}"
        if notes:
            output += "\n\n— " + "; ".join(notes) + "."
        return ToolResult(output=output, error=False,
                          metadata={"status": status, "chars": len(text),
                                    "bytes": len(body), "title": page.title,
                                    "truncated": bool(notes)})

    @staticmethod
    def _download(target: str):
        """(status, final url, content-type, bytes, capped) or a ToolResult failure."""
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True,
                              headers={"User-Agent": USER_AGENT}) as client:
                with client.stream("GET", target) as response:
                    status = response.status_code
                    final_url = str(response.url)
                    content_type = response.headers.get("content-type") or ""
                    raw = bytearray()
                    capped = False
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            capped = True
                            break
                    return (status, final_url, content_type, bytes(raw), capped)
        except (httpx.HTTPError, OSError) as exc:
            return ToolResult(
                output=L(f"the page never answered: {exc.__class__.__name__}: {exc}",
                         f"страница не ответила: {exc.__class__.__name__}: {exc}"),
                error=True)

    def is_safe(self) -> bool:
        # Same reasoning as web_search: read + fetch is a working exfiltration
        # pair, so reaching outside needs the user's grant.
        return False


def _plain(body: bytes, cap: int) -> str:
    return body.decode("utf-8", "replace")[:cap]


def response_reason(status: int) -> str:
    """The phrase for a code, without a second HTTP round trip."""
    import http.client

    try:
        return http.client.responses.get(status, "")
    except Exception:
        return ""
