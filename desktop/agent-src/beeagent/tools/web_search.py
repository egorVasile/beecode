"""Search several public endpoints and report failures without inventing results."""
import urllib.parse
import base64
from html.parser import HTMLParser

import httpx

from beeagent.i18n import L

from .base import BaseTool, ToolResult

ENDPOINT = "https://html.duckduckgo.com/html/"
LITE_ENDPOINT = "https://lite.duckduckgo.com/lite/"
BING_ENDPOINT = "https://www.bing.com/search"
# What one page of DDG markup holds; anything past it is reported as dropped.
LIMIT = 10


class _ResultParser(HTMLParser):
    """Title and href of every `result__a` link, in page order."""

    def __init__(self):
        super().__init__()
        self.hits: list[tuple[str, str]] = []
        self._in_link = False
        self._href = ""
        self._text = ""

    def handle_starttag(self, tag, attrs):
        if tag != "a" or self._in_link:
            return
        classes = next((value or "" for key, value in attrs if key == "class"), "")
        if not {"result__a", "result-link"}.intersection(classes.split()):
            return
        self._in_link = True
        self._href = next((value or "" for key, value in attrs if key == "href"), "")
        self._text = ""

    def handle_data(self, data):
        if self._in_link:
            self._text += data

    def handle_endtag(self, tag):
        if tag != "a" or not self._in_link:
            return
        self._in_link = False
        title = " ".join(self._text.split())
        if title:
            self.hits.append((title, _target_of(self._href)))


def _target_of(href: str) -> str:
    """The page a result link really opens.

    DuckDuckGo wraps its outbound links in `/l/?uddg=<encoded>`; handing the model
    the wrapper is a title with no address — it cannot cite, open or read the
    source it was told about.
    """
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parts = urllib.parse.urlsplit(href)
    host = (parts.hostname or "").lower()
    if (host == "duckduckgo.com" or host.endswith(".duckduckgo.com")) and parts.path.startswith("/l/"):
        wrapped = urllib.parse.parse_qs(parts.query).get("uddg") or [""]
        return wrapped[0]
    if (host == "bing.com" or host.endswith(".bing.com")) and parts.path.startswith("/ck/"):
        wrapped = (urllib.parse.parse_qs(parts.query).get("u") or [""])[0]
        if wrapped.startswith("a1"):
            try:
                payload = wrapped[2:]
                decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8")
                if urllib.parse.urlsplit(decoded).scheme in ("http", "https"):
                    return decoded
            except (ValueError, UnicodeError):
                pass
        return ""
    return href


class _BingParser(HTMLParser):
    """Extract links from result headings only; ignore ads and navigation."""
    def __init__(self):
        super().__init__()
        self.hits = []
        self.depth = 0
        self.result_depth = None
        self.heading_depth = None
        self.link_depth = None
        self.href = ""
        self.text = ""

    def handle_starttag(self, tag, attrs):
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            return
        self.depth += 1
        attrs = dict(attrs)
        if tag == "li" and "b_algo" in (attrs.get("class") or "").split() and self.result_depth is None:
            self.result_depth = self.depth
        elif tag == "h2" and self.result_depth is not None and self.heading_depth is None:
            self.heading_depth = self.depth
        elif tag == "a" and self.heading_depth is not None and self.link_depth is None:
            self.link_depth = self.depth
            self.href, self.text = attrs.get("href") or "", ""

    def handle_data(self, data):
        if self.link_depth is not None:
            self.text += data

    def handle_endtag(self, tag):
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            return
        if tag == "a" and self.link_depth == self.depth:
            title = " ".join(self.text.split())
            url = _target_of(self.href)
            if title and url: self.hits.append((title, url))
            self.link_depth = None
        if tag == "h2" and self.heading_depth == self.depth: self.heading_depth = None
        if tag == "li" and self.result_depth == self.depth: self.result_depth = None
        self.depth = max(0, self.depth - 1)


class WebSearchTool(BaseTool):
    name = "web_search"
    description = (
        "Search the internet. Returns numbered results, each with the URL behind "
        "it; a refusal from the search endpoint comes back as an error, not as an "
        "empty result set."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    }

    def execute(self, query: str) -> ToolResult:
        wanted = " ".join(str(query).split())
        if not wanted:
            return ToolResult(
                output=L("the query is empty — nothing was searched",
                         "запрос пуст — поиск не выполнялся"),
                error=True)
        failures = []
        hits = []
        status = None
        source = ""
        for endpoint, parser_type in ((ENDPOINT, _ResultParser),
                                      (LITE_ENDPOINT, _ResultParser),
                                      (BING_ENDPOINT, _BingParser)):
            try:
                resp = httpx.get(endpoint, params={"q": wanted},
                                 headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                status = resp.status_code
                if status != 200:
                    failures.append(f"{endpoint}: HTTP {status}")
                    continue
                parser = parser_type()
                parser.feed(resp.text or "")
                parser.close()
                # Only real links are useful: skip challenge and malformed pages.
                hits = [(title, url) for title, url in parser.hits
                        if urllib.parse.urlsplit(url).scheme in ("http", "https")]
                if hits:
                    source = endpoint
                    break
                failures.append(f"{endpoint}: no result links")
            except Exception as exc:
                failures.append(f"{endpoint}: {type(exc).__name__}: {str(exc)[:160]}")
        if not hits:
            return ToolResult(
                output=L("Search endpoints refused or could not provide results. "
                         "This does not mean there are no results. " + "; ".join(failures),
                         "Не удалось получить результаты ни от одного поискового сервера. "
                         "Это не означает, что результатов нет. " + "; ".join(failures)),
                error=True, metadata={"status": status, "results": 0, "dropped": 0})

        shown = hits[:LIMIT]
        dropped = len(hits) - len(shown)
        linkless = sum(1 for _, url in shown if not url)
        lines = []
        for number, (title, url) in enumerate(shown, start=1):
            lines.append(f"{number}. {title}")
            lines.append(f"   {url}" if url else
                         L("   (the page gave no URL)", "   (страница без URL)"))
        notes = []
        if dropped:
            notes.append(L(f"{dropped} more result(s) on the page were dropped "
                           f"(only the first {LIMIT} are returned)",
                           f"ещё {dropped} результат(ов) со страницы отброшено "
                           f"(возвращаются только первые {LIMIT})"))
        if linkless:
            notes.append(L(f"{linkless} of the {len(shown)} results came back "
                           "without a URL",
                           f"{linkless} из {len(shown)} результатов пришли без URL"))
        if notes:
            lines.append("; ".join(notes) + ".")
        return ToolResult(
            output="\n".join(lines),
            error=False,
            metadata={"status": status, "source": source, "results": len(shown),
                      "found": len(hits), "dropped": dropped,
                      "linkless": linkless})

    def is_safe(self) -> bool:
        # The query leaves the machine, and read + search is a working
        # exfiltration pair: a file the model can read it can also send out.
        return False
