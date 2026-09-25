"""A search result that says what really came back.

`html.duckduckgo.com/html/` answers a scripted request with HTTP 202 and a page
of its own; reading that as "the web has nothing" is how a tool ends up lying
about the world. So the status is checked, every title is returned with the URL
behind it, and the results we did not show are counted out loud.

No provider was added, no key is needed and no challenge is bypassed: a refusal
from the endpoint is reported as a refusal, and the model decides what to do
about it.
"""
import urllib.parse
from html.parser import HTMLParser

import httpx

from beeagent.i18n import L

from .base import BaseTool, ToolResult

ENDPOINT = "https://html.duckduckgo.com/html/"
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
        if "result__a" not in classes.split():
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
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        wrapped = urllib.parse.parse_qs(parts.query).get("uddg") or [""]
        return wrapped[0]
    return href


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
        try:
            resp = httpx.get(
                ENDPOINT,
                params={"q": wanted},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
        except Exception as exc:
            return ToolResult(
                output=L(f"the search never answered: {exc}",
                         f"поиск не ответил: {exc}"),
                error=True)

        # Read straight off the response: a default of 200 for a missing status
        # would be the same lie as an unchecked one.
        status = resp.status_code
        body = resp.text or ""
        size = len(body.encode("utf-8", "replace"))
        if status != 200:
            # The whole point: a refusal has to read as a refusal. HTTP 202 is
            # exactly what every audit query drew, and the old code fed its empty
            # body to the parser and reported "No results found" with error=False.
            reason = resp.reason_phrase or ""
            return ToolResult(
                output=L(
                    f"the search endpoint refused the query: HTTP {status} {reason} "
                    f"from {ENDPOINT} ({size} bytes of body, no results read). That "
                    "is the endpoint's answer, not an empty web — the query was "
                    "never checked. Say so rather than concluding there is nothing "
                    "out there.",
                    f"поисковый сервер отклонил запрос: HTTP {status} {reason} "
                    f"({ENDPOINT}, {size} байт ответа, результаты не читались). Это "
                    "отказ сервера, а не пустой интернет — запрос не проверялся. Так "
                    "и скажите, вместо «в сети ничего нет»."),
                error=True,
                metadata={"status": status, "results": 0, "dropped": 0})

        parser = _ResultParser()
        try:
            parser.feed(body)
            parser.close()
        except Exception as exc:
            return ToolResult(
                output=L(f"the answer arrived (HTTP {status}) but could not be "
                         f"parsed: {exc}",
                         f"ответ пришёл (HTTP {status}), но разобрать его не "
                         f"удалось: {exc}"),
                error=True,
                metadata={"status": status})

        hits = parser.hits
        if not hits:
            # 200 with no result links: could be a real empty result, could be a
            # page shaped like something else. Say which of the two we can prove.
            return ToolResult(
                output=L(
                    f"HTTP {status} from {ENDPOINT} and {size} bytes of page, but "
                    f"the page held no result links. Either nothing matches "
                    f"{wanted!r}, or this answer is not a results page — do not "
                    "treat it as proof the web has nothing.",
                    f"HTTP {status} от {ENDPOINT}, {size} байт страницы, но ссылок "
                    "с результатами на ней нет. Либо по запросу ничего не найдено, "
                    "либо это не страница результатов — не считайте это "
                    "доказательством, что в сети пусто."),
                error=False,
                metadata={"status": status, "results": 0, "dropped": 0})

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
            metadata={"status": status, "results": len(shown),
                      "found": len(hits), "dropped": dropped,
                      "linkless": linkless})

    def is_safe(self) -> bool:
        # The query leaves the machine, and read + search is a working
        # exfiltration pair: a file the model can read it can also send out.
        return False
