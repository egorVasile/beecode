from types import SimpleNamespace
from beeagent.tools import web_search


def response(status, text=''):
    return SimpleNamespace(status_code=status, text=text)


def test_search_falls_back_from_202_to_lite(monkeypatch):
    called=[]
    def get(url, **kwargs):
        called.append(url)
        if url == web_search.ENDPOINT: return response(202)
        return response(200, '<a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F">Example</a>')
    monkeypatch.setattr(web_search.httpx, 'get', get)
    result = web_search.WebSearchTool().execute('test')
    assert not result.error and 'https://example.com/' in result.output
    assert result.metadata['source'] == web_search.LITE_ENDPOINT
    assert called == [web_search.ENDPOINT, web_search.LITE_ENDPOINT]


def test_search_uses_bing_when_both_ddg_endpoints_fail(monkeypatch):
    def get(url, **kwargs):
        if url != web_search.BING_ENDPOINT: return response(202)
        return response(200, '<html><body><ol><li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS8">Example</a></h2></li></ol></body></html>')
    monkeypatch.setattr(web_search.httpx, 'get', get)
    result = web_search.WebSearchTool().execute('test')
    assert not result.error and 'https://example.com/' in result.output
    assert result.metadata['source'] == web_search.BING_ENDPOINT


def test_all_failed_and_unrecognized_pages_are_not_reported_as_empty_web(monkeypatch):
    monkeypatch.setattr(web_search.httpx, 'get', lambda url, **kwargs: response(202) if url != web_search.BING_ENDPOINT else response(200, '<html>challenge</html>'))
    result = web_search.WebSearchTool().execute('test')
    assert result.error and result.metadata['results'] == 0
    assert '202' in result.output and 'does not mean' in result.output


def test_only_real_duckduckgo_hosts_are_unwrapped():
    spoof = 'https://evilduckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com'
    assert web_search._target_of(spoof) == spoof
