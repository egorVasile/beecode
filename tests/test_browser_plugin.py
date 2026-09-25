"""The `browser` pack, tested without a network and without a browser.

Neither Playwright nor Chromium may be assumed on this machine — one is not
installed here at all — so `playwright.sync_api` is replaced by a module that
records what the tool asked it to do. That is the point of the fake: every claim the
tool makes about a page has to be visible in the calls it issued, and every failure
path (no Playwright, no page, zero matches, a timeout, a capped body, a screenshot
that never landed) has to come back as `error=True` with words that name it. A
refusal that reads like a result is the bug this file exists to keep out.

The two views are checked separately on purpose: a person gets the picture drawn
into their terminal (or a sentence naming the fallback and why), and the model gets
text *about* the page, because a keyless model cannot see images and a silent gap
where a summary should be would read as "the page was empty".
"""
import ast
import base64
import importlib.util
import json
import shutil
import sys
import types
from pathlib import Path

import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core.permissions import Permissions
from beeagent.plugins.catalog import TEMPLATES_DIR

PACK = TEMPLATES_DIR / "plugins" / "browser"
PACK_MODULE = "browser_pack_under_test"
DEV_SERVER = "http://127.0.0.1:8000/login"

#: Playwright's own exception is literally named `TimeoutError` and is a subclass of
#: its `Error`; the tool recognises a timeout by walking the MRO, so the fake carries
#: those two names instead of the tool being loosened to suit a lazy fake.
PW_ERROR = type("Error", (Exception,), {})
PW_TIMEOUT = type("TimeoutError", (PW_ERROR,), {})


class FakeElement:
    """One matched node: `inner_text()` is the only thing the tool calls on it."""

    def __init__(self, text):
        self._text = text

    def inner_text(self):
        return self._text


@pytest.fixture()
def pack():
    """A fresh import of plugin.py — and so a fresh `TOOLS[0]`, i.e. a fresh session."""
    spec = importlib.util.spec_from_file_location(PACK_MODULE, PACK / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACK_MODULE] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(PACK_MODULE, None)


# ------------------------------------------------------------------ the fake ----

class FakePage:
    """Only the surface `plugin.py` touches; an unexpected call is an AttributeError."""

    def __init__(self):
        self.log: list = []
        self.matches: list = []
        self.summary = {"title": "Sign in - dev", "url": DEV_SERVER,
                        "ready": "complete", "textChars": 1284,
                        "headings": ["h1 Sign in", "h2 Local dev"],
                        "fields": ["input text name=user id=u", "input password name=p"],
                        "buttons": ["Log in"]}
        self.text_length = 1284
        self.value = {"ok": True}
        self.title_text = "Sign in - dev"
        self.url = "about:blank"
        self.shot_bytes = b"\x89PNG\r\n\x1a\n" + b"z" * 3000          # 3008 bytes
        self.goto_error = self.click_error = None
        self.fill_error = self.evaluate_error = self.screenshot_error = None
        self.selector_error = None
        self.closed = False

    def goto(self, url, **kwargs):
        self.log.append(("goto", url, kwargs))
        if self.goto_error:
            raise self.goto_error
        self.url = url
        return FakeResponse(200, url)

    def query_selector_all(self, selector):
        self.log.append(("query", selector))
        if self.selector_error:
            raise self.selector_error
        return list(self.matches)

    def click(self, selector, **kwargs):
        self.log.append(("click", selector, kwargs))
        if self.click_error:
            raise self.click_error

    def fill(self, selector, text, **kwargs):
        self.log.append(("fill", selector, text, kwargs))
        if self.fill_error:
            raise self.fill_error

    def evaluate(self, expression, **kwargs):
        self.log.append(("evaluate", expression[:24], kwargs))
        if self.evaluate_error:
            raise self.evaluate_error
        if "textChars" in expression:                 # the structure summary
            return self.summary
        if expression.startswith("document.body ?"):  # the visible-text length
            return self.text_length
        return self.value

    def screenshot(self, **kwargs):
        self.log.append(("screenshot", kwargs))
        if self.screenshot_error:
            raise self.screenshot_error
        if kwargs.get("path"):
            Path(kwargs["path"]).write_bytes(self.shot_bytes)
        return self.shot_bytes

    def title(self):
        return self.title_text

    def close(self):
        self.log.append(("page.close",))
        self.closed = True


class FakeResponse:
    def __init__(self, status, url):
        self.status = status
        self.url = url


class FakeBrowser:
    def __init__(self, page, log):
        self.page, self.log = page, log

    def new_page(self, **kwargs):
        self.log.append(("new_page", kwargs))
        return self.page

    def close(self):
        self.log.append(("browser.close",))


class FakeChromium:
    def __init__(self, browser, log, launch_error=None):
        self.browser, self.log, self.launch_error = browser, log, launch_error
        self.launch_kwargs: list = []

    def launch(self, **kwargs):
        self.launch_kwargs.append(kwargs)
        self.log.append(("launch", kwargs))
        if self.launch_error:
            raise self.launch_error
        return self.browser


class FakePlaywright:
    def __init__(self, browser, log, launch_error=None):
        self.chromium = FakeChromium(browser, log, launch_error)
        self.log = log

    def stop(self):
        self.log.append(("stop",))


class FakeManager:
    """What `sync_playwright()` returns: the handle you call `start()` on."""

    def __init__(self, playwright):
        self.playwright = playwright

    def start(self):
        return self.playwright


def install(monkeypatch, page=None, launch_error=None):
    """Put a fake `playwright.sync_api` in sys.modules and hand back the witnesses."""
    page = page or FakePage()
    log: list = []
    page.log = log                     # one ledger: page, browser and driver calls
    browser = FakeBrowser(page, log)
    playwright = FakePlaywright(browser, log, launch_error)
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: FakeManager(playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return types.SimpleNamespace(page=page, browser=browser, playwright=playwright,
                                 chromium=playwright.chromium, module=module, log=log)


def opened(pack, monkeypatch, page=None, **kwargs):
    """A tool holding an already-navigated page — the state every other action needs."""
    env = install(monkeypatch, page=page, **kwargs)
    result = pack.TOOLS[0].execute(action="navigate", url=DEV_SERVER)
    assert not result.error, result.output
    return pack.TOOLS[0], env


def fake_graphics(caps=None, renders="ESC-ART-ESCAPES", describes=None,
                  render_raises=None, caps_raises=False):
    """A `beeagent.ui.graphics` lookalike on the three-function contract."""
    calls: list = []

    def capabilities():
        calls.append(("capabilities",))
        if caps_raises:
            raise PW_ERROR("capabilities(): no console attached")
        return dict({"images": True, "max_cols": 120} if caps is None else caps)

    def describe(path):
        calls.append(("describe", path))
        if describes is None:
            raise PW_ERROR("describe() is not implemented here")
        return describes

    def render(path, max_rows=None):
        calls.append(("render", path, max_rows))
        if render_raises:
            raise render_raises
        return renders

    module = types.ModuleType("beeagent.ui.graphics")
    module.capabilities = capabilities
    module.render = render
    module.describe = describe
    module.calls = calls
    return module


def logged(env, kind):
    return [entry for entry in env.log if entry[0] == kind]


# --------------------------------------------------------------- the pack -------

def test_the_pack_loads_through_the_real_plugin_loader(tmp_path, monkeypatch):
    """The folder format is proven, not assumed: manifest, entry, TOOLS, registration."""
    monkeypatch.chdir(tmp_path)
    shutil.copytree(PACK, tmp_path / ".beeagent" / "plugins" / "browser")
    (tmp_path / ".beeagent" / "plugins.json").write_text(
        json.dumps({"installed": {"browser": {"type": "plugin", "enabled": True}}}),
        encoding="utf-8")

    from beeagent.core.agent import Agent

    agent = Agent(config=BeeConfig(), workdir=str(tmp_path))
    assert not agent.plugins.load_errors, agent.plugins.load_errors
    assert "browser" in agent.tools.list_names()

    tool = agent.tools.get("browser")
    assert tool.from_extension is True, "the loader marks it, so the gate treats it as ours"
    assert tool.is_safe() is False and tool.writes_files is True
    assert "browser" in tool.description and "/allow browser" in tool.description


def test_the_gate_refuses_it_until_the_user_grants_browser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shutil.copytree(PACK, tmp_path / ".beeagent" / "plugins" / "browser")
    (tmp_path / ".beeagent" / "plugins.json").write_text(
        json.dumps({"installed": {"browser": {"type": "plugin", "enabled": True}}}),
        encoding="utf-8")
    from beeagent.core.agent import Agent

    agent = Agent(config=BeeConfig(), workdir=str(tmp_path))
    tool = agent.tools.get("browser")

    ask = Permissions(mode="ask")
    assert not ask.allows(tool), "a browser that runs JavaScript must not run ungranted"
    assert "/allow browser" in ask.refusal(tool)
    ask.grant("browser")
    assert ask.allows(tool)
    # Read-only is a ceiling, not a queue: a granted tool that writes screenshots stops.
    assert not Permissions(mode="readonly").allows(tool)
    assert not Permissions(mode="readonly", allowed=["browser"]).allows(tool)
    assert Permissions(mode="auto").allows(tool)


def test_the_manifest_and_the_catalog_advertise_the_same_thing():
    manifest = json.loads((PACK / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "browser" and manifest["type"] == "plugin"
    assert (PACK / manifest["entry"]).is_file()
    assert manifest["description"].strip() and manifest["version"]

    catalog = json.loads((TEMPLATES_DIR.parent / "data" / "catalog.json").read_text(
        encoding="utf-8"))
    entries = [item for item in catalog["items"] if item["id"] == "browser"]
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["type"] == "plugin" and entry["category"] == "plugins"
    assert entry["source"] == {"kind": "builtin", "path": "plugins/browser"}
    assert (PACK).is_dir()
    assert "playwright" in entry["description"].lower(), "the dependency is the deal"
    assert "install" in entry["description"].lower()


def test_the_pack_cannot_install_or_download_anything():
    """Rule 1, held structurally: no subprocess, no urllib, no socket, no httpx.

    The refusal names the two commands and stops there. Importing anything that could
    run them for the user is exactly what this test forbids.
    """
    tree = ast.parse((PACK / "plugin.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= {"base64", "json", "os", "re", "time", "datetime", "inspect",
                        "pathlib", "importlib", "beeagent"}, sorted(imported)
    code = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    code |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for forbidden in ("subprocess", "urlopen", "urlretrieve", "Popen", "popen",
                      "system", "httpx", "socket", "requests"):
        assert forbidden not in code, f"the pack reached for {forbidden}"


def test_the_browser_is_launched_vanilla_because_stealth_is_not_the_feature():
    """No `args=`, no `user_agent=`, no `storage_state=`, no injected script — at the
    call sites, where it would matter, not merely in the prose."""
    tree = ast.parse((PACK / "plugin.py").read_text(encoding="utf-8"))
    launches = [node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("launch", "launch_persistent_context")]
    assert launches, "the pack stopped launching a browser at all?"
    for call in launches:
        assert {kw.arg for kw in call.keywords} <= {"headless"}, ast.unparse(call)

    keywords = {kw.arg for node in ast.walk(tree) if isinstance(node, ast.Call)
                for kw in node.keywords}
    assert not keywords & {"user_agent", "storage_state", "bypass_csp", "locale",
                           "timezone_id", "http_credentials", "proxy", "args"}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attributes & {"webdriver", "add_init_script", "set_extra_http_headers",
                            "route", "emulate_media"}


# ------------------------------------------------------ missing Playwright ------

def test_a_machine_without_playwright_gets_an_error_naming_the_exact_commands(pack,
                                                                             monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    monkeypatch.delitem(sys.modules, "playwright", raising=False)

    result = pack.TOOLS[0].execute(action="navigate", url=DEV_SERVER)

    assert result.error is True, "a refusal must not read as a result"
    assert "pip install playwright" in result.output
    assert "playwright install chromium" in result.output
    assert "Nothing was installed or downloaded" in result.output
    assert result.metadata["playwright"] is False and result.metadata["refused"] is True


@pytest.mark.parametrize("action,extra", [
    ("click", {"selector": "#login"}),
    ("type", {"selector": "#user", "text": "bea"}),
    ("get_text", {}),
    ("evaluate", {"js": "1+1"}),
    ("screenshot", {}),
])
def test_every_page_action_refuses_the_same_way(pack, monkeypatch, action, extra):
    """No branch may invent a page, an empty string, or a quiet success."""
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    monkeypatch.delitem(sys.modules, "playwright", raising=False)

    result = pack.TOOLS[0].execute(action=action, **extra)

    assert result.error is True, (action, result.output)
    assert "did not run" in result.output, result.output
    assert "playwright install chromium" in result.output
    # `close` is the exception: closing nothing is a success, not a refusal.
    assert pack.TOOLS[0].execute(action="close").error is False


def test_a_phone_is_not_handed_a_command_that_cannot_work(pack, monkeypatch):
    """Playwright ships no Chromium for Android, so the desktop recipe is a lie there."""
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    monkeypatch.delitem(sys.modules, "playwright", raising=False)
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")

    result = pack.TOOLS[0].execute(action="navigate", url=DEV_SERVER)

    assert result.error is True
    assert "Termux" in result.output and "web_fetch" in result.output
    assert "playwright install chromium" not in result.output
    assert result.metadata["termux"] is True and result.metadata["install"] == ""


def test_a_broken_but_present_playwright_is_refused_the_same_way(pack, monkeypatch):
    module = types.ModuleType("playwright.sync_api")      # no sync_playwright at all
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    result = pack.TOOLS[0].execute(action="get_text")
    assert result.error and "no sync_playwright" in result.output


# ------------------------------------------------------------------ navigate ----

def test_navigate_reports_the_page_and_launches_without_stealth(pack, monkeypatch):
    env = install(monkeypatch)

    result = pack.TOOLS[0].execute(action="navigate", url="127.0.0.1:8000/login")

    assert not result.error, result.output
    assert env.chromium.launch_kwargs == [{"headless": True}]
    assert ("new_page", {}) in logged(env, "new_page"), "a fresh context, none of our options"
    url, kwargs = logged(env, "goto")[0][1:]
    assert url == DEV_SERVER, "a schemeless host got http:// — and says so below"
    assert kwargs == {"timeout": pack.DEFAULT_TIMEOUT_MS,
                      "wait_until": "domcontentloaded"}
    assert "requested: http://127.0.0.1:8000/login" in result.output
    assert "HTTP status: 200" in result.output
    assert "title: Sign in - dev" in result.output
    assert "visible text: 1284 chars" in result.output
    assert "a Chromium and a blank page were started" in result.output
    assert "fresh empty context" in result.output
    assert "default Playwright automation flags" in result.output
    assert result.metadata == {"action": "navigate", "url": DEV_SERVER,
                              "requested": "http://127.0.0.1:8000/login",
                              "status": 200, "title": "Sign in - dev",
                              "timeout_ms": pack.DEFAULT_TIMEOUT_MS}


def test_the_second_navigate_reuses_the_page_and_says_nothing_about_starting(pack,
                                                                            monkeypatch):
    tool, env = opened(pack, monkeypatch)
    result = tool.execute(action="navigate", url="http://127.0.0.1:8000/account")
    assert not result.error
    assert len(logged(env, "launch")) == 1 and len(logged(env, "new_page")) == 1
    assert "a Chromium and a blank page were started" not in result.output
    assert "session:" not in result.output


def test_navigate_names_the_timeout_it_hit_and_keeps_the_page(pack, monkeypatch):
    tool, env = opened(pack, monkeypatch)
    env.page.goto_error = PW_TIMEOUT(f"Timeout {pack.DEFAULT_TIMEOUT_MS}ms exceeded.")

    result = tool.execute(action="navigate", url="http://127.0.0.1:9999/", timeout_ms=2500)

    assert result.error is True
    assert "no answer from http://127.0.0.1:9999/ within 2.5s" in result.output
    assert result.metadata["timeout"] is True and result.metadata["timeout_ms"] == 2500
    assert tool._page is not None, "a slow page is not a dead session"
    assert ("browser.close",) not in env.log


def test_a_page_that_dies_mid_navigate_takes_the_session_down_with_it(pack, monkeypatch):
    tool, env = opened(pack, monkeypatch)
    env.page.goto_error = PW_ERROR("Target page, context or browser has been closed")

    result = tool.execute(action="navigate", url="http://127.0.0.1:8000/x")

    assert result.error is True and "The session was closed" in result.output
    assert tool._page is None and tool._browser is None
    assert ("browser.close",) in env.log and ("stop",) in env.log


@pytest.mark.parametrize("url,reason", [
    ("file:///etc/passwd", "file:"),
    ("data:text/html,<script>alert(1)</script>", "data:"),
    ("javascript:void(document.cookie)", "javascript:"),
    ("chrome://version", "chrome:"),
    ("ws://127.0.0.1:8000/socket", "ws:"),
])
def test_navigate_refuses_a_scheme_that_would_read_the_disk_back(pack, monkeypatch, url,
                                                                reason):
    env = install(monkeypatch)
    result = pack.TOOLS[0].execute(action="navigate", url=url)

    assert result.error is True and f"`{reason}`" in result.output, result.output
    assert "Serve the file over http" in result.output
    assert env.log == [], f"{url} was navigated to anyway"
    # ...while the use case — a local dev server, with or without a scheme — is allowed.
    assert pack.TOOLS[0].execute(action="navigate", url=DEV_SERVER).error is False


def test_a_blank_or_unparsable_address_is_named_not_guessed(pack):
    assert pack.TOOLS[0].execute(action="navigate", url="   ").error is True
    bad = pack.TOOLS[0].execute(action="navigate", url="the-usual-page")
    assert bad.error and "no scheme" in bad.output, bad.output
    assert pack.TOOLS[0].execute(action="navigate", url="https://example.com/x").error \
        is True, "no Playwright here, so that is a refusal, not a visit"


# ------------------------------------------------------------- click and type ---

def test_click_tells_zero_matches_from_three(pack, monkeypatch):
    tool, env = opened(pack, monkeypatch)

    none = tool.execute(action="click", selector="#nope")
    assert none.error is True
    assert "click('#nope'): matched 0 element(s)" in none.output
    assert "nothing was clicked" in none.output and "no element on the page" in none.output
    assert "get_text" in none.output, "point at the way to stop guessing"
    assert none.metadata["matched"] == 0 and none.metadata["clicked"] is False

    env.page.matches = [FakeElement("a"), FakeElement("b"), FakeElement("c")]
    three = tool.execute(action="click", selector="button")
    assert not three.error, three.output
    assert "matched 3 element(s)" in three.output
    assert "clicked the first of 3" in three.output and "ambiguous" in three.output
    assert three.metadata["matched"] == 3 and three.metadata["clicked"] is True
    assert ("click", "button", {"timeout": pack.DEFAULT_TIMEOUT_MS}) in env.log
    assert "now at: " + DEV_SERVER in three.output


def test_an_unparsable_selector_is_reported_as_that_and_clicks_nothing(pack, monkeypatch):
    tool, env = opened(pack, monkeypatch)
    env.page.selector_error = PW_ERROR('Unexpected token "" while parsing selector')

    result = tool.execute(action="click", selector="[[[")

    assert result.error is True and "not a CSS selector" in result.output
    assert result.metadata["matched"] is None
    assert not logged(env, "click")


def test_a_timeout_is_reported_as_a_timeout_with_its_seconds(pack, monkeypatch):
    page = FakePage()
    page.matches = [FakeElement("Pay")]
    tool, env = opened(pack, monkeypatch, page=page)
    page.click_error = PW_TIMEOUT("Timeout 15000ms exceeded.")

    result = tool.execute(action="click", selector="button.pay")

    assert result.error is True
    assert "timed out" in result.output and "after 15s" in result.output
    assert "hidden, disabled, or covered" in result.output
    assert "matched 1 element(s)" in result.output, "the selector was fine; the button was not"
    assert result.metadata["timeout"] is True and result.metadata["timeout_ms"] == 15000


def test_a_generic_error_is_not_labeled_a_timeout(pack, monkeypatch):
    page = FakePage()
    page.matches = [FakeElement("Pay")]
    tool, _ = opened(pack, monkeypatch, page=page)

    class Boom(Exception):
        pass

    page.click = lambda *a, **k: (_ for _ in ()).throw(Boom("the driver exploded"))
    result = tool.execute(action="click", selector="button.pay")

    assert result.error and "raised Boom: the driver exploded" in result.output
    assert "timed out" not in result.output
    assert result.metadata["timeout"] is False


def test_type_reports_the_field_it_filled_and_the_one_that_would_not_take_it(pack,
                                                                            monkeypatch):
    page = FakePage()
    page.matches = [FakeElement("")]
    tool, env = opened(pack, monkeypatch, page=page)

    typed = tool.execute(action="type", selector="#user", text=" Beverley ")
    assert not typed.error and "matched 1 element(s)" in typed.output
    assert "typed 10 characters into the field" in typed.output
    assert ("fill", "#user", " Beverley ", {"timeout": pack.DEFAULT_TIMEOUT_MS}) in env.log
    assert typed.metadata["chars"] == 10

    cleared = tool.execute(action="type", selector="#user", text="")
    assert not cleared.error and "typed 0 characters" in cleared.output
    assert "empty text clears the field" in cleared.output

    page.fill_error = PW_TIMEOUT("Timeout 15000ms exceeded.")
    stuck = tool.execute(action="type", selector="#user", text="x", timeout_ms=900)
    assert stuck.error and "not editable within 0.9s" in stuck.output
    assert stuck.metadata["timeout_ms"] == 900 and stuck.metadata["typed"] is False

    page.matches = []
    gone = tool.execute(action="type", selector="#user", text="x")
    assert gone.error and "matched 0 element(s)" in gone.output
    assert "nothing was typed" in gone.output


def test_a_missing_argument_and_an_unknown_action_are_named_not_guessed(pack):
    tool = pack.TOOLS[0]

    missing = tool.execute(action="click")
    assert missing.error and missing.metadata["missing"] == ["selector"]
    assert "needs `selector`" in missing.output and "as you left it" in missing.output

    bogus = tool.execute(action="scroll")
    assert bogus.error and "`navigate`" in bogus.output and "Nothing was opened" in bogus.output
    assert bogus.metadata["valid_actions"] == list(pack.ACTIONS)

    assert tool.execute(action="").error is True


# --------------------------------------------------------------------- text -----

def test_get_text_reports_matches_counts_and_the_cap_it_applied(pack, monkeypatch):
    page = FakePage()
    page.matches = [FakeElement("x" * 5000), FakeElement("y" * 5000)]
    tool, _ = opened(pack, monkeypatch, page=page)
    total = 10_001                                    # 5000 + the joining newline + 5000

    capped = tool.execute(action="get_text", selector=".row")

    body = capped.output.split("--- text ---" + chr(10))[1]
    assert not capped.error, capped.output
    assert "matched 2 element(s)" in capped.output
    assert f"chars: {pack.DEFAULT_TEXT_CHARS} of {total}" in capped.output, capped.output
    assert f"cut at {pack.DEFAULT_TEXT_CHARS}, {total - 8000} dropped" in capped.output
    assert "raise max_chars" in capped.output and "max 40000" in capped.output
    assert len(body) == pack.DEFAULT_TEXT_CHARS
    assert body.startswith("x" * 100) and body.endswith("y" * 10), "the cut is mid-body"
    assert capped.metadata["truncated"] is True
    assert capped.metadata["dropped"] == total - 8000
    assert capped.metadata["chars"] == pack.DEFAULT_TEXT_CHARS
    assert capped.metadata["total_chars"] == total

    small = tool.execute(action="get_text", selector=".row", max_chars=1000)
    assert f"chars: 1000 of {total}" in small.output
    assert f"cut at 1000, {total - 1000} dropped" in small.output

    page.matches = [FakeElement("z" * 45_000)]
    greedy = tool.execute(action="get_text", selector=".row", max_chars=999_999)
    assert "cut at 40000" in greedy.output, "the cap is clamped, and then reported"
    assert "chars: 40000 of 45000" in greedy.output
    assert greedy.metadata["dropped"] == 5000


def test_get_text_without_a_selector_reads_the_body_and_says_so(pack, monkeypatch):
    page = FakePage()
    page.matches = [FakeElement("Total 42 orders")]
    tool, env = opened(pack, monkeypatch, page=page)

    result = tool.execute(action="get_text")

    assert not result.error, result.output
    assert "get_text('body'): matched 1 element(s)" in result.output
    assert "no selector given" in result.output and "`body`" in result.output
    assert "chars: 15 of 15" in result.output and "dropped" not in result.output
    assert "Total 42 orders" in result.output
    assert ("query", "body") in env.log
    assert result.metadata["truncated"] is False


def test_get_text_on_nothing_and_on_an_unrendered_element_are_both_specific(pack,
                                                                           monkeypatch):
    page = FakePage()
    tool, _ = opened(pack, monkeypatch, page=page)

    empty = tool.execute(action="get_text", selector="#gone")
    assert empty.error and "matched 0 element(s)" in empty.output
    assert "title: Sign in - dev" in empty.output, "say where it looked"

    class Hidden(FakeElement):
        def inner_text(self):
            raise PW_ERROR("Element is not visible")

    page.matches = [Hidden("nothing")]
    hidden = tool.execute(action="get_text", selector=".row")
    assert not hidden.error, "one unreadable element is not a failed read"
    assert "1 of the matched elements gave no text" in hidden.output
    assert "not rendered" in hidden.output
    assert "chars: 0 of 0" in hidden.output


def test_get_text_stops_reading_at_a_bound_on_a_thousand_match_page(pack, monkeypatch):
    page = FakePage()
    page.matches = [FakeElement(str(index)) for index in range(200)]
    tool, _ = opened(pack, monkeypatch, page=page)

    result = tool.execute(action="get_text", selector=".item")

    assert "matched 200 element(s)" in result.output
    assert (f"only the first {pack.MAX_MATCHES_READ} of 200 matches were read"
            in result.output)
    assert result.metadata["matched"] == 200


def test_evaluate_returns_the_value_or_the_pages_own_exception_text(pack, monkeypatch):
    page = FakePage()
    page.value = {"rows": 3, "labels": ["a", "b"]}
    tool, env = opened(pack, monkeypatch, page=page)

    ok = tool.execute(action="evaluate", js="document.title.length")
    payload = json.dumps(page.value, ensure_ascii=False)
    assert not ok.error, ok.output
    assert "evaluate returned dict" in ok.output
    assert f"chars: {len(payload)} of {len(payload)}" in ok.output
    assert ok.output.split("--- value ---\n")[1] == payload
    assert ("evaluate", "document.title.length", {"timeout": pack.DEFAULT_TIMEOUT_MS}) \
        in env.log

    page.evaluate_error = PW_ERROR("Page.evaluate: ReferenceError: foo is not defined\n"
                                   "    at <anonymous>:1:1")
    bad = tool.execute(action="evaluate", js="foo()")
    assert bad.error
    assert "ReferenceError: foo is not defined" in bad.output
    assert "came from the page, not from BeeCode" in bad.output
    assert bad.metadata["exception"] == "Error"
    page.evaluate_error = None

    page.value = "x" * 5000
    capped = tool.execute(action="evaluate", js="'x'.repeat(5000)", max_chars=1000)
    assert f"chars: 1000 of {len(json.dumps('x' * 5000))}" in capped.output
    assert f"{len(json.dumps('x' * 5000)) - 1000} dropped at the 1000 cap" in capped.output
    assert capped.metadata["truncated"] is True


def test_evaluate_without_js_is_refused(pack, monkeypatch):
    tool, env = opened(pack, monkeypatch)
    result = tool.execute(action="evaluate", js="   ")
    assert result.error and result.metadata["missing"] == ["js"]
    assert len(logged(env, "evaluate")) == 1, "only the navigate's own reads happened"


# ----------------------------------------------------------------- screenshot ---

def test_screenshot_reports_the_file_the_bytes_and_both_views(tmp_path, monkeypatch, pack):
    monkeypatch.chdir(tmp_path)
    page = FakePage()
    page.shot_bytes = b"\x89PNG\r\n\x1a\n" + b"z" * 4096            # 4104 bytes
    tool, _ = opened(pack, monkeypatch, page=page)
    graphics = fake_graphics(caps={"images": True, "protocol": "kitty"})
    monkeypatch.setitem(sys.modules, "beeagent.ui.graphics", graphics)

    result = tool.execute(action="screenshot", path="shots/one.png")

    written = tmp_path / "shots" / "one.png"
    assert written.is_file() and written.stat().st_size == len(page.shot_bytes)
    assert f"screenshot written: {written}" in result.output
    assert "bytes: 4104" in result.output
    # the human's rendering, with the row cap it asked for
    assert "human view: drawn by beeagent.ui.graphics.render(max_rows=18)" in result.output
    assert "ESC-ART-ESCAPES" in result.output
    assert "chars of terminal output standing in for 4104 bytes of PNG" in result.output
    assert graphics.calls[-1] == ("render", str(written), 18)
    # the model's rendering: words about the page, and the honest reason why
    assert "model view (text only" in result.output and "cannot see images" in result.output
    assert "  title: Sign in - dev" in result.output
    assert "  url: " + DEV_SERVER in result.output
    assert "  visible text: 1284 chars · ready: complete" in result.output
    assert "  headings (2): h1 Sign in; h2 Local dev" in result.output
    assert "  form fields (2):" in result.output and "input password name=p" in result.output
    assert "  buttons and their labels (1): Log in" in result.output
    assert "base64: not attached" in result.output
    assert result.metadata["bytes"] == 4104 and result.metadata["attach_base64"] is False
    assert result.metadata["human_view"] == "human view"


def test_a_page_that_gives_no_structure_summary_says_so_instead_of_looking_empty(
        tmp_path, monkeypatch, pack):
    monkeypatch.chdir(tmp_path)
    page = FakePage()
    page.summary = "not a dict"
    tool, _ = opened(pack, monkeypatch, page=page)
    monkeypatch.setitem(sys.modules, "beeagent.ui.graphics", fake_graphics())

    result = tool.execute(action="screenshot", path="bare.png")

    assert not result.error
    assert "the page gave no structure summary" in result.output
    assert "read it with get_text" in result.output
    assert "  title: Sign in - dev" in result.output, "title/url still come from the page"
    assert "headings (0): none" in result.output


def test_the_default_screenshot_path_lands_in_the_project(tmp_path, monkeypatch, pack):
    monkeypatch.chdir(tmp_path)
    tool, _ = opened(pack, monkeypatch)
    monkeypatch.setitem(sys.modules, "beeagent.ui.graphics", fake_graphics())

    first = tool.execute(action="screenshot")
    second = tool.execute(action="screenshot")

    shots = sorted((tmp_path / ".beeagent" / "screenshots").glob("browser-*.png"))
    assert len(shots) == 2, [p.name for p in shots]
    assert not first.error and not second.error
    assert str(shots[0]) in first.output and str(shots[1]) in second.output
    assert first.metadata["path"].endswith(".png")


def test_a_terminal_that_cannot_show_pictures_is_told_which_fallback_ran(tmp_path,
                                                                       monkeypatch, pack):
    monkeypatch.chdir(tmp_path)
    tool, _ = opened(pack, monkeypatch)

    # 1. no graphics module at all (the state of this checkout before it landed).
    monkeypatch.setitem(sys.modules, "beeagent.ui.graphics", None)
    missing = tool.execute(action="screenshot", path="a.png")
    assert "human view: not drawn — beeagent.ui.graphics is not importable" in missing.output
    assert "the file:" in missing.output and "a.png" in missing.output
    assert "model view (text only" in missing.output and not missing.error

    # 2. the module is there, render() yields nothing: name the fallback that ran.
    monkeypatch.setitem(sys.modules, "beeagent.ui.graphics",
                        fake_graphics(caps={"images": False}, renders="",
                                      describes="a 1280x720 PNG of a login form"))
    described = tool.execute(action="screenshot", path="b.png")
    assert "human view: fallback used — render() returned nothing to draw" in described.output
    assert "capabilities() says images=False" in described.output, "the module's own words"
    assert "graphics.describe(), which is text about the picture" in described.output
    assert "a 1280x720 PNG of a login form" in described.output
    assert "model view (text only" in described.output

    # 3. nothing can draw it and describe() is not there either: say that, out loud.
    monkeypatch.setitem(sys.modules, "beeagent.ui.graphics",
                        fake_graphics(caps={"images": False}, renders=""))
    last = tool.execute(action="screenshot", path="c.png")
    assert "nothing could draw it and graphics.describe() gave nothing" in last.output
    assert "only the path is reported" in last.output
    assert not last.error, "a terminal without pictures is not a failed screenshot"

    # 4. the key shape graphics.py really returns: kitty/iterm2/sixel + `why`.
    plain = {"kitty": False, "iterm2": False, "sixel": False, "truecolor": False,
             "why": "TERM=xterm-256color; stdout is not a TTY, so nothing is drawn"}
    monkeypatch.setitem(sys.modules, "beeagent.ui.graphics",
                        fake_graphics(caps=plain, renders="ASCII-ART-FALLBACK"))
    texted = tool.execute(action="screenshot", path="ascii.png")
    assert "human view: drawn by beeagent.ui.graphics.render(max_rows=18)" in texted.output
    assert "kitty=False, iterm2=False, sixel=False" in texted.output
    assert "stdout is not a TTY" in texted.output, "the module's own reason is passed on"
    assert "graphics.render()'s own text rendering, not a picture" in texted.output
    assert "ASCII-ART-FALLBACK" in texted.output

    monkeypatch.setitem(sys.modules, "beeagent.ui.graphics",
                        fake_graphics(caps={**plain, "kitty": True}, renders="KITTY-ESCAPES"))
    kitty = tool.execute(action="screenshot", path="kitty.png")
    assert "kitty=True" in kitty.output and "not a picture" not in kitty.output
    assert "KITTY-ESCAPES" in kitty.output


def test_a_graphics_module_that_misbehaves_is_still_a_named_fallback(tmp_path,
                                                                    monkeypatch, pack):
    monkeypatch.chdir(tmp_path)
    tool, _ = opened(pack, monkeypatch)
    monkeypatch.setitem(sys.modules, "beeagent.ui.graphics",
                        fake_graphics(caps_raises=True, render_raises=PW_ERROR("no sixel")))
    result = tool.execute(action="screenshot", path="d.png")

    assert not result.error and (tmp_path / "d.png").is_file()
    assert "human view: fallback used — render() returned nothing" in result.output
    assert "capabilities() raised" in result.output
    assert "only the path is reported" in result.output


def test_attach_base64_is_capped_and_the_file_stays_whole(tmp_path, monkeypatch, pack):
    monkeypatch.chdir(tmp_path)
    tool, _ = opened(pack, monkeypatch)
    monkeypatch.setitem(sys.modules, "beeagent.ui.graphics", fake_graphics())
    monkeypatch.setattr(pack, "MAX_BASE64_CHARS", 200)

    result = tool.execute(action="screenshot", path="e.png", attach_base64=True)

    whole = base64.b64encode((tmp_path / "e.png").read_bytes()).decode("ascii")
    block = result.output.split("base64 (")[1]
    payload = block.split("):\n", 1)[1].split("\n")[0]
    assert len(payload) == 200 == len(whole) - (len(whole) - 200)
    assert f"base64 (200 chars of text standing in for 3008 bytes of PNG" in result.output
    assert f"capped at 200 chars, {len(whole) - 200} dropped" in result.output
    assert "is complete on disk" in result.output
    assert (tmp_path / "e.png").stat().st_size == 3008
    assert "not for this model" in result.output


def test_a_screenshot_that_never_landed_is_not_reported_as_a_success(tmp_path,
                                                                    monkeypatch, pack):
    monkeypatch.chdir(tmp_path)
    page = FakePage()
    tool, _ = opened(pack, monkeypatch, page=page)
    page.screenshot_error = PW_TIMEOUT("Timeout 15000ms exceeded.")

    timed = tool.execute(action="screenshot", path="f.png", timeout_ms=1500)
    assert timed.error and "did not paint within 1.5s" in timed.output
    assert "no file was written at" in timed.output
    assert timed.metadata["timeout_ms"] == 1500
    assert not (tmp_path / "f.png").exists()

    page.screenshot_error = None
    page.screenshot = lambda **kwargs: None            # the page lied about writing
    lie = tool.execute(action="screenshot", path="g.png")
    assert lie.error and "is not a readable file" in lie.output
    assert lie.metadata["bytes"] == 0


# -------------------------------------------------------------------- session ---

def test_one_session_is_reused_and_a_closed_page_is_not_reopened_silently(pack,
                                                                          monkeypatch):
    tool, env = opened(pack, monkeypatch)
    env.page.matches = [FakeElement("x")]

    assert tool.execute(action="click", selector="#save").error is False
    assert tool.execute(action="get_text", selector="#save").error is False
    assert len(logged(env, "launch")) == 1 and len(logged(env, "new_page")) == 1, \
        "three calls, one browser, one page"

    closed = tool.execute(action="close")
    assert not closed.error and "closed: page, browser, playwright" in closed.output
    assert "last address: " + DEV_SERVER in closed.output
    assert env.page.closed and ("browser.close",) in env.log and ("stop",) in env.log

    after = tool.execute(action="click", selector="#save")
    assert after.error is True and "no page is open" in after.output
    assert "I will not silently reopen one" in after.output
    assert after.metadata == {"action": "click", "page_open": False,
                             "browser_open": False, "origin": ""}
    assert len(logged(env, "new_page")) == 1 and len(logged(env, "launch")) == 1
    assert len(logged(env, "click")) == 1, "the refused click never reached the page"


def test_a_browser_left_running_without_a_page_is_said_so(pack, monkeypatch):
    tool, env = opened(pack, monkeypatch)
    tool._page = None                       # the page was closed under the session

    result = tool.execute(action="get_text")

    assert result.error and "the browser is running but its page was closed" in result.output
    assert result.metadata["browser_open"] is True
    assert len(logged(env, "new_page")) == 1, "no silent relaunch"


def test_close_after_a_failed_navigate_does_not_raise(pack, monkeypatch):
    tool, env = opened(pack, monkeypatch)
    env.page.goto_error = PW_ERROR("net::ERR_CONNECTION_REFUSED")

    failed = tool.execute(action="navigate", url="http://127.0.0.1:9/locked")
    assert failed.error and "ERR_CONNECTION_REFUSED" in failed.output

    closed = tool.execute(action="close")
    assert not closed.error, closed.output
    assert closed.metadata["closed"] == ["page", "browser", "playwright"]

    again = tool.execute(action="close")
    assert not again.error and "nothing was open" in again.output
    assert again.metadata["closed"] == []


def test_a_handle_that_refuses_to_close_is_named_not_hidden(pack, monkeypatch):
    tool, env = opened(pack, monkeypatch)

    def refuse():
        raise PW_ERROR("browser.kill() failed: Access is denied")

    env.browser.close = refuse
    env.playwright.stop = refuse

    closed = tool.execute(action="close")

    assert not closed.error, "a stuck handle is not a reason to hide it"
    assert "did not close cleanly" in closed.output
    assert closed.output.count("browser.kill() failed") == 2
    assert closed.metadata["unclean"] and len(closed.metadata["closed"]) == 3
    assert tool._page is None and tool._browser is None, "the handles are dropped anyway"


def test_a_chromium_that_will_not_start_leaves_nothing_behind(pack, monkeypatch):
    env = install(monkeypatch, launch_error=PW_ERROR("browserType.launch: Executable "
                                                    "is not found at ~/.cache"))
    result = pack.TOOLS[0].execute(action="navigate", url=DEV_SERVER)

    assert result.error is True
    assert "Playwright is importable but Chromium would not start" in result.output
    assert "playwright install chromium" in result.output
    assert "Nothing was installed or downloaded" in result.output
    assert ("stop",) in env.log, "the driver handle was closed, not leaked"
    assert pack.TOOLS[0]._playwright is None and pack.TOOLS[0]._browser is None


def test_a_page_that_is_closed_under_a_later_call_reports_itself(pack, monkeypatch):
    page = FakePage()
    page.matches = [FakeElement("x")]
    tool, env = opened(pack, monkeypatch, page=page)

    def gone(*_args, **_kwargs):
        raise PW_ERROR("Target page, context or browser has been closed")

    page.click = gone

    result = tool.execute(action="click", selector="#save")

    assert result.error and "raised Error: Target page" in result.output
    assert "The page or browser is gone, so the session was closed" in result.output
    assert "browser(action=navigate" in result.output, "name the way back"
    assert result.metadata["page_gone"] is True and result.metadata["timeout"] is False
    assert tool._page is None and tool._browser is None
    assert ("browser.close",) in env.log and ("stop",) in env.log
    assert ("page.close",) in page.log


def test_every_bound_is_clamped(pack, monkeypatch):
    """A model can ask for an hour or for nothing; the page gets a bounded number."""
    page = FakePage()
    page.matches = [FakeElement("x" * 500)]
    tool, env = opened(pack, monkeypatch, page=page)

    for given, expected in ((10 ** 9, pack.MAX_TIMEOUT_MS), (1, pack.MIN_TIMEOUT_MS),
                            ("soon", pack.DEFAULT_TIMEOUT_MS),
                            (None, pack.DEFAULT_TIMEOUT_MS),
                            (-50, pack.MIN_TIMEOUT_MS)):
        result = tool.execute(action="click", selector=".row", timeout_ms=given)
        assert not result.error, result.output
        assert logged(env, "click")[-1][2] == {"timeout": expected}, given

    small = tool.execute(action="get_text", selector=".row", max_chars=1)
    assert "chars: 100 of 500" in small.output, small.output
    assert "cut at 100, 400 dropped" in small.output, "the floor is named, not silent"
    page.value = 1
    big = tool.execute(action="evaluate", js="1", max_chars=10 ** 9)
    assert "chars: 1 of 1" in big.output

def test_the_tool_is_named_browser_and_advertises_the_actions_it_has(pack):
    tool = pack.TOOLS[0]
    assert tool.name == "browser"
    assert tool.writes_files is True and tool.is_safe() is False
    schema = tool.to_schema()["parameters"]
    assert schema["properties"]["action"]["enum"] == list(pack.ACTIONS)
    assert schema["required"] == ["action"]
    assert tool.params() == ["action", "url", "selector", "text", "js", "path",
                             "attach_base64", "timeout_ms", "max_chars"]
