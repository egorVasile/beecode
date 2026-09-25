"""The fetch tool: what a page is allowed to say about itself.

No request leaves this file — the suite-wide socket guard would catch it, and a
test that spends the user's network is a bug, not a check.
"""
import pytest

import beeagent.tools.web_fetch as wf
from beeagent.core.permissions import Permissions
from beeagent.tools.web_fetch import WebFetchTool


class _Response:
    def __init__(self, status=200, body=b"", content_type="text/html; charset=utf-8",
                 url="https://example.com/docs"):
        self.status_code = status
        self.url = url
        self.headers = {"content-type": content_type} if content_type else {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        chunk = 4096
        for start in range(0, len(self._body), chunk):
            yield self._body[start:start + chunk]


class _Client:
    """The pieces the tool uses: a stream() that hands back one canned response."""

    def __init__(self, response=None, error=None, seen=None):
        self.response = response or _Response()
        self.error = error
        self.seen = seen if seen is not None else []

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        self.seen.append((method, url))
        if self.error:
            raise self.error
        return self.response


def _patch(monkeypatch, response=None, error=None, seen=None):
    client = _Client(response, error, seen)
    monkeypatch.setattr(wf.httpx, "Client", client)
    return client


PAGE = b"<html><head><title>The real docs</title>" \
       b"<style>body{color:red}</style><script>secret()</script></head>" \
       b"<body><h1>Install</h1><p>run pip install beecode</p></body></html>"


def test_the_title_and_the_visible_text_come_back_the_scripts_do_not(monkeypatch):
    _patch(monkeypatch, _Response(body=PAGE))
    result = WebFetchTool().execute(url="https://example.com/docs")
    assert not result.error
    assert "The real docs" in result.output
    assert "run pip install beecode" in result.output
    assert "secret()" not in result.output, "a script body is not page text"
    assert "color:red" not in result.output
    assert result.metadata["status"] == 200


def test_the_address_that_was_actually_reached_is_reported(monkeypatch):
    _patch(monkeypatch, _Response(url="https://docs.example.com/final/page"))
    result = WebFetchTool().execute(url="https://example.com/redirects")
    assert "https://docs.example.com/final/page" in result.output


def test_a_404_is_a_404_and_not_an_empty_page(monkeypatch):
    _patch(monkeypatch, _Response(status=404, body=b"<html>nope</html>"))
    result = WebFetchTool().execute(url="https://example.com/gone")
    assert result.error is True
    assert "404" in result.output
    assert "nope" not in result.output, "the body of a refusal is not the answer"


def test_a_binary_is_named_and_refused_rather_than_returned_empty(monkeypatch):
    _patch(monkeypatch, _Response(body=b"\x89PNG....", content_type="image/png"))
    result = WebFetchTool().execute(url="https://example.com/logo.png")
    assert result.error is True
    assert "image/png" in result.output


def test_a_page_with_no_text_says_it_is_probably_javascript(monkeypatch):
    _patch(monkeypatch, _Response(body=b"<html><body><div id=app></div></body></html>"))
    result = WebFetchTool().execute(url="https://example.com/spa")
    assert not result.error, "the page did answer; it simply had nothing to say"
    assert "JavaScript" in result.output


def test_text_cut_off_says_so_and_sizes_the_cut(monkeypatch):
    body = b"<html><body>" + b"word " * 4000 + b"</body></html>"
    _patch(monkeypatch, _Response(body=body))
    result = WebFetchTool().execute(url="https://example.com/long", max_chars=1000)
    assert "cut at 1000 characters" in result.output, result.output[-300:]


def test_an_oversized_body_is_stopped_and_the_stop_is_reported(monkeypatch):
    body = b"<html><body>" + b"word " * (wf.MAX_RESPONSE_BYTES // 5) + b"</body></html>"
    _patch(monkeypatch, _Response(body=body))
    result = WebFetchTool().execute(url="https://example.com/huge")
    assert "body stopped at" in result.output, result.output[-300:]
    assert result.metadata["truncated"] is True


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://localhost:8077/v1/pool",
    "http://127.0.0.1/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/internal",
    "http://[::1]/",
])
def test_the_addresses_that_are_never_requested(url, monkeypatch):
    seen = []
    _patch(monkeypatch, seen=seen)
    result = WebFetchTool().execute(url=url)
    assert result.error is True
    assert seen == [], "no request may leave for these addresses"


def test_a_transport_failure_is_reported_as_one(monkeypatch):
    _patch(monkeypatch, error=wf.httpx.ConnectError("no route"))
    result = WebFetchTool().execute(url="https://example.com")
    assert result.error is True
    assert "never answered" in result.output


def test_an_empty_url_does_not_fetch_anything(monkeypatch):
    seen = []
    _patch(monkeypatch, seen=seen)
    assert WebFetchTool().execute(url="").error is True
    assert seen == []


def test_fetching_needs_the_users_grant():
    """read + fetch is an exfiltration pair, so this tool is not pre-trusted."""
    tool = WebFetchTool()
    assert tool.is_safe() is False
    assert not Permissions("ask").allows(tool)
    assert Permissions("ask", allowed=["web_fetch"]).allows(tool)


def test_the_names_a_model_invents_resolve_to_this_tool():
    from beeagent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    tool = WebFetchTool()
    registry.register(tool)
    for alias in ("webfetch", "fetch", "read_url", "open_url"):
        assert registry.get(alias) is tool, alias
