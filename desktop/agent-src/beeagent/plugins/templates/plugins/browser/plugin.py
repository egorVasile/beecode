"""`browser` — look at a real page with Playwright, and report what actually happened.

The sibling `web_fetch` tool reads a page without executing anything, which is why
it can only ever answer "this page is drawn by JavaScript, I do not run a browser".
This pack is the answer to that sentence: it drives a real Chromium through
Playwright so the agent can log into a local dev server, click a button, read the
resulting DOM and take a screenshot. Three things made this file be written the way
it is.

**Nothing is installed or downloaded.** Playwright and its browser must already be
on the machine. The refusal when they are not names the two commands the *user* has
to run, and comes back with `error=True` — a tool that quietly answered "no browser
here" in the voice of a result is the worst failure class here, because the model
then relays a missing dependency as a fact about the page. Running those commands
ourselves would be worse: a tool that may `pip install` is a tool that may install
anything, and it would do it without asking. On Termux the refusal says something
else again, because Playwright ships no Chromium for Android: there is no command
that would work, and pretending otherwise would send a phone user chasing one.

**No stealth, ever.** No spoofed User-Agent, no `--disable-blink-features=
AutomationControlled`, no patched `navigator.webdriver`, no captcha solving, no
cookies imported from the user's real profile, no proxy to dodge a block. Each of
those is a lie told to a server about who is knocking, and the only thing it is ever
wanted for is getting past a refusal the site made on purpose. So the launch below
is vanilla on purpose: a fresh empty context, the default automation identity, and
the site's own answer reported verbatim when it refuses us. A local dev server, or a
page the user pointed at, is the use case; evading a bot wall is not.

**It runs arbitrary JavaScript and reaches the network**, so `is_safe()` is False;
the extension gate already refuses an ungranted plugin tool (`from_extension`), and
this states the intent instead of leaning on who registered us. `writes_files` is
True because a screenshot is bytes on disk, which read-only mode has to refuse even
after a grant.

Every action answers in the same shape: what was asked, what matched, what was cut
off, what to do next. A hung browser must never hang the agent, so every page call
carries a bounded timeout, every path is wrapped, and whatever was opened is closed
on the way out.
"""
import base64
import json
import os
import re
import time
from datetime import datetime
from inspect import signature
from pathlib import Path

from beeagent.tools.base import BaseTool, ToolResult

#: The only remedy for a missing Playwright, and the only one this tool may name.
INSTALL_STEPS = (
    "  pip install playwright        # the Python driver\n"
    "  playwright install chromium   # and the browser itself (~170 MB)")

DEFAULT_TIMEOUT_MS = 15_000
MIN_TIMEOUT_MS = 500
MAX_TIMEOUT_MS = 120_000
DEFAULT_TEXT_CHARS = 8_000
MIN_TEXT_CHARS = 100
MAX_TEXT_CHARS = 40_000
SUMMARY_ITEMS = 25          # headings / fields / buttons per kind in a summary
MAX_MATCHES_READ = 50       # a page with 5 000 `.item`s must not cost 5 000 round trips
DEFAULT_MAX_ROWS = 18       # how much of the terminal one picture may take
#: Base64 *text*, not PNG bytes: roughly 3/4 of this many characters is real image.
MAX_BASE64_CHARS = 200_000
SCREENSHOT_DIR = Path(".beeagent") / "screenshots"

ACTIONS = ("navigate", "click", "type", "get_text", "evaluate", "screenshot", "close")

#: `http://` and friends — an address with an authority. `127.0.0.1:8000/x` does not
#: match, which is the point: a host with a port and no scheme is a local dev server,
#: and a leading digit already rules out half of the misparses.
_AUTHORITY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")
#: A scheme written without slashes: `javascript:void(0)`, `data:text/html,...`.
_BARE_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
#: Never navigated to, whatever the caller's reason for asking.
_REFUSED_SCHEMES = frozenset({"file", "data", "javascript", "blob", "about", "chrome",
                              "chrome-extension", "view-source", "devtools", "resource",
                              "ws", "wss", "ftp", "jar"})

#: What each action needs beyond itself, spelled out so a wrong call is named in
#: those words instead of dying inside Playwright with a message about something else.
REQUIRED_ARGS = {
    "navigate": ("url",),
    "click": ("selector",),
    "type": ("selector",),
    "evaluate": ("js",),
}

#: What did *not* happen, for the too-few-arguments refusal. "Nothing was done"
#: would be true but useless; the model has to see which verb it lost.
PAST_TENSE = {"navigate": "navigated", "click": "clicked", "type": "typed",
              "get_text": "read", "evaluate": "evaluated",
              "screenshot": "photographed", "close": "closed"}

#: The page's own shape, read in the page's own words. An expression, not a
#: function, so no caller can steer this string into `eval`; it only reads.
_SUMMARY_JS = r"""
({
  title: document.title,
  url: location.href,
  ready: document.readyState,
  textChars: document.body ? (document.body.innerText || '').length : 0,
  headings: Array.from(document.querySelectorAll('h1,h2,h3,h4')).slice(0, 25).map(
    (el) => el.tagName.toLowerCase() + ' '
      + (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 90)
  ).filter((s) => s.replace(/^[a-z0-9]+ /, '').length > 0),
  fields: Array.from(document.querySelectorAll('input, textarea, select'))
    .filter((el) => (el.type || '').toLowerCase() !== 'hidden').slice(0, 25).map((el) => {
      const bits = [el.tagName.toLowerCase(), el.type || 'text'];
      if (el.name) bits.push('name=' + el.name);
      if (el.id) bits.push('id=' + el.id);
      const ph = el.getAttribute && el.getAttribute('placeholder');
      if (ph) bits.push('placeholder=' + JSON.stringify(ph));
      if (el.tagName.toLowerCase() === 'select') {
        bits.push('options=' + Array.from(el.options).slice(0, 6)
          .map((o) => o.value || o.text).join('|'));
      }
      return bits.join(' ');
    }),
  buttons: Array.from(document.querySelectorAll(
    'button, [role=button], input[type=submit], input[type=button], a.button'))
    .slice(0, 25).map((el) => (el.innerText || el.value
      || el.getAttribute('aria-label') || '(no label)').trim()
      .replace(/\s+/g, ' ').slice(0, 90))
})
"""


class PlaywrightMissing(RuntimeError):
    """Raised inside the tool and turned into the refusal; it never reaches the loop."""


def playwright_module():
    """The already-installed `playwright.sync_api`, or `PlaywrightMissing`.

    Imported through `importlib` rather than a bare `import` so that one
    except-clause covers "not installed", "half a wheel" and "installed but
    broken", and so a caller can hand over a module without a package on disk.
    Nothing in here installs the missing piece: no pip, no download, no subprocess.
    """
    try:
        from importlib import import_module

        module = import_module("playwright.sync_api")
    except Exception as exc:
        raise PlaywrightMissing(str(exc) or exc.__class__.__name__) from exc
    if not getattr(module, "sync_playwright", None):
        raise PlaywrightMissing("playwright.sync_api has no sync_playwright — an "
                                "incomplete or too-old Playwright install")
    return module


def _clamp_timeout(value) -> int:
    """Milliseconds to actually use: the model's number, bounded and never zero."""
    try:
        ms = int(float(value))
    except (TypeError, ValueError):
        ms = DEFAULT_TIMEOUT_MS
    return max(MIN_TIMEOUT_MS, min(MAX_TIMEOUT_MS, ms))


def _clamp_chars(value) -> int:
    try:
        cap = int(float(value))
    except (TypeError, ValueError):
        cap = DEFAULT_TEXT_CHARS
    return max(MIN_TEXT_CHARS, min(MAX_TEXT_CHARS, cap))


def _seconds(ms: int) -> str:
    return f"{ms / 1000:g}s"


def _is_timeout(exc: BaseException) -> bool:
    """Did this exception mean "the page took too long"?

    Playwright raises its own `TimeoutError` (a subclass of its `Error`), so the
    class name is the reliable signal. The message shape is checked too, because a
    build that only says "Timeout 15000ms exceeded." should not come back as a
    mystery failure. Everything else stays what it is: an error, not a timeout.
    """
    if any("timeout" in cls.__name__.lower() for cls in type(exc).__mro__):
        return True
    text = " ".join(str(exc).lower().split())
    return text.startswith("timeout ") and "exceeded" in text


def _is_dead(exc: BaseException) -> bool:
    """The page or browser is gone: closed under us, crashed, or killed."""
    text = " ".join(str(exc).lower().split())
    return any(hint in text for hint in ("has been closed", "target page",
                                         "target closed", "crashed",
                                         "connection closed"))


def _cap(text: str, limit: int) -> tuple[str, int]:
    """Cut to *limit* characters and report how many fell off."""
    if len(text) <= limit:
        return text, 0
    return text[:limit], len(text) - limit


def _on_termux() -> bool:
    """Android's Termux, where Playwright has nothing to install.

    The same marker `/doctor` reads: a phone has no apt and no compiler, and
    Playwright ships no Chromium for it, so handing a phone user
    `playwright install chromium` would be a command that cannot succeed.
    """
    marker = "com.termux"
    return marker in (os.environ.get("PREFIX") or "") or marker in (
        os.environ.get("HOME") or "")


def _guard(func, default):
    """One call that must not take the answer down with it."""
    try:
        return func()
    except Exception:
        return default


def _matches(page, selector: str):
    """The elements a CSS selector hits, or None when the selector itself is bad.

    Counting first is what makes `click` reportable: "matched 0" and "matched 3 and
    I clicked the first" are different answers about the page, and a model that gets
    only "ok" cannot tell a wrong selector from a disabled button.
    """
    try:
        return list(page.query_selector_all(selector))
    except Exception as exc:
        text = str(exc).lower()
        if "selector" in text and any(hint in text for hint in
                                      ("parse", "syntax", "does not match",
                                       "unexpected token", "invalid")):
            return None
        raise


class BrowserTool(BaseTool):
    """One browser session, held by this instance and reused across calls.

    The page survives between calls on purpose: "log in, then look at the account
    page" is two calls and one session. A call that finds no open page says so
    instead of quietly opening one, because silently reopening would show the model
    a blank page where it expected the one it had just logged into.
    """

    name = "browser"
    description = (
        "Drive a real Chromium page (Playwright must already be installed; this "
        "tool never installs or downloads anything). Actions: navigate(url), "
        "click(selector), type(selector, text), get_text(selector, empty for the "
        "whole page), evaluate(js), screenshot(path, attach_base64), close(). One "
        "session is reused between calls. It runs JavaScript and reaches the "
        "network, so it needs /allow browser. No stealth: default User-Agent, a "
        "fresh empty profile holding none of your cookies, no captcha or bot-wall "
        "evasion - if a site refuses the automation, its refusal is the answer. For "
        "a page you only want to read, use web_fetch."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS),
                       "description": "What to do"},
            "url": {"type": "string",
                    "description": "Absolute http(s) address for navigate. A local "
                                   "dev server is fine; file:// and data: are not."},
            "selector": {"type": "string",
                         "description": "CSS selector for click/type/get_text; empty "
                                        "for get_text means the whole page"},
            "text": {"type": "string",
                     "description": "Text to type (an empty string clears the field)"},
            "js": {"type": "string", "description": "JavaScript expression to evaluate"},
            "path": {"type": "string",
                     "description": f"Screenshot target (default {SCREENSHOT_DIR}/)"},
            "attach_base64": {"type": "boolean",
                              "description": f"Also return the image as base64 text, "
                                             f"capped at {MAX_BASE64_CHARS} characters"},
            "timeout_ms": {"type": "integer",
                           "description": f"Per-operation timeout (default "
                                          f"{DEFAULT_TIMEOUT_MS}, {MIN_TIMEOUT_MS}-"
                                          f"{MAX_TIMEOUT_MS})"},
            "max_chars": {"type": "integer",
                          "description": f"Text/eval cap (default {DEFAULT_TEXT_CHARS}, "
                                         f"max {MAX_TEXT_CHARS})"},
        },
        "required": ["action"],
    }

    # A screenshot is a file, so read-only mode has to be able to refuse this tool
    # even after the user granted it.
    writes_files = True

    def __init__(self):
        self._playwright = None     # the started sync_playwright() handle
        self._browser = None        # the launched Chromium
        self._page = None           # the one page this session drives
        self._origin = ""           # the last address navigate was asked for
        self._shot = 0              # screenshot counter, for default file names

    def is_safe(self) -> bool:
        # Not read-only in any sense: it runs arbitrary JavaScript, it opens
        # sockets, and a screenshot writes. `from_extension` already makes the
        # gate ask for `/allow browser`; this keeps the intent in the code rather
        # than depending on whoever registered the tool.
        return False

    # --- the call ---------------------------------------------------------------

    def execute(self, action: str = "", **kwargs) -> ToolResult:
        name = str(action or "").strip().lower()
        if name not in ACTIONS:
            wanted = ", ".join(f"`{item}`" for item in ACTIONS)
            given = (f"unknown action {name!r}" if name else "no action given")
            return ToolResult(
                output=f"browser: {given}. Use one of: {wanted}. Nothing was opened "
                       "and no page was changed.",
                error=True, metadata={"action": name, "valid_actions": list(ACTIONS)})

        missing = [key for key in REQUIRED_ARGS.get(name, ())
                   if not str(kwargs.get(key) or "").strip()]
        if missing:
            return ToolResult(
                output=f"browser(action={name}) needs "
                       + " and ".join(f"`{key}`" for key in missing)
                       + f". Nothing was {PAST_TENSE.get(name, 'done')} — the page is "
                         "exactly as you left it.",
                error=True, metadata={"action": name, "missing": missing})

        handler = getattr(self, "_" + name)
        try:
            return handler(**kwargs)
        except PlaywrightMissing as exc:
            return self._refuse(name, exc)
        except Exception as exc:               # the loop never gets a traceback
            gone = _is_dead(exc)
            leftovers = self._teardown() if gone else []
            note = ""
            if gone:
                note = ("\nThe page or browser is gone, so the session was torn down"
                        + (f" ({'; '.join(leftovers)})" if leftovers else "")
                        + ". Start again with browser(action=navigate, url=...).")
            return ToolResult(
                output=f"browser(action={name}) failed: "
                       f"{type(exc).__name__}: {exc}{note}",
                error=True,
                metadata={"action": name, "exception": type(exc).__name__,
                          "timeout": _is_timeout(exc), "page_gone": gone})

    # --- the refusal that is not a result ---------------------------------------

    def _refuse(self, action: str, exc: PlaywrightMissing) -> ToolResult:
        """The answer on a machine with no browser — and not a promise to fix it."""
        on_phone = _on_termux()
        remedy = (
            "Nothing was installed or downloaded by BeeCode, and this tool will never "
            "do that on its own. Run these yourself, in this order:\n"
            + INSTALL_STEPS) if not on_phone else (
            "This is Termux: Playwright publishes no Chromium build for Android, so no "
            "command makes this tool work on a phone. Read plain pages with web_fetch, "
            "and drive a browser on a desktop machine.")
        return ToolResult(
            output=(f"browser(action={action}) did not run: Playwright is not usable "
                    f"on this machine ({exc}).\n{remedy}\n"
                    "No page was opened and no file was written — this refusal is "
                    "the result, not an answer about the page."),
            error=True,
            metadata={"action": action, "playwright": False, "refused": True,
                      "termux": on_phone,
                      "install": "" if on_phone else INSTALL_STEPS})

    # --- the session -------------------------------------------------------------

    def _live_page(self, action: str):
        """(page, None) when a page is open; else (None, the answer to give)."""
        if self._page is not None:
            return self._page, None
        try:
            playwright_module()
        except PlaywrightMissing as exc:
            # No page is open *because* there is no browser; say the real reason.
            raise exc
        started = self._browser is not None or self._playwright is not None
        return None, ToolResult(
            output=(f"browser(action={action}): no page is open — "
                    + ("the browser is running but its page was closed" if started
                       else "no browser has been started in this session")
                    + (f" (the last address asked for was {self._origin})"
                       if self._origin else "")
                    + ".\nI will not silently reopen one: a fresh page is not the "
                      "page you were looking at. Call browser(action=navigate, "
                      "url=...) first, then repeat this."),
            error=True, metadata={"action": action, "page_open": False,
                                  "browser_open": started, "origin": self._origin})

    def _ensure_browser(self):
        """Start Playwright, Chromium and a page if any of them are missing.

        Returns (page, None) or (None, a refusal). Half-opened state is cleaned up
        here rather than left to a garbage collector: a `playwright` handle whose
        launch failed still holds a driver process.
        """
        try:
            module = playwright_module()
        except PlaywrightMissing:
            raise                       # `execute` answers with the install commands
        try:
            if self._playwright is None:
                self._playwright = module.sync_playwright().start()
            if self._browser is None:
                # Vanilla on purpose: no `args=`, no `user_agent=`, no
                # `storage_state=`. Every one of those is a stealth knob, and a
                # fresh empty context also means none of the user's cookies ride in.
                self._browser = self._playwright.chromium.launch(headless=True)
            if self._page is None:
                self._page = self._browser.new_page()
            return self._page, None
        except Exception as exc:
            # Whatever half of the session exists comes down with it: a `playwright`
            # handle whose launch failed still holds a live driver process.
            problems = self._teardown()
            tail = (f"; torn down: {'; '.join(problems)}" if problems else "") + "."
            if _is_timeout(exc):
                return None, ToolResult(
                    output=f"Chromium did not come up within the timeout: {exc}. "
                           "Nothing was installed or downloaded to work around it"
                           + tail,
                    error=True, metadata={"playwright": True, "launched": False,
                                          "timeout": True})
            return None, ToolResult(
                output=(f"Playwright is importable but Chromium would not start: "
                        f"{type(exc).__name__}: {exc}\n"
                        "Nothing was installed or downloaded to work around that. If "
                        "the browser binary is missing, run `playwright install "
                        "chromium` yourself" + tail),
                error=True, metadata={"playwright": True, "launched": False,
                                      "exception": type(exc).__name__})

    def _gone_note(self, exc: BaseException) -> str:
        """Drop the session when the page died under us, and say that in the result.

        A dead page has to be noticed here rather than left in `self._page`: the
        next call would otherwise repeat the same failure forever, and a model that
        keeps retrying against a closed browser learns nothing.
        """
        if not _is_dead(exc):
            return ""
        problems = self._teardown()
        return ("\nThe page or browser is gone, so the session was closed"
                + (f" ({'; '.join(problems)})" if problems else "")
                + ". Call browser(action=navigate, url=...) to start a fresh one; a "
                  "call that finds no page will say so rather than reopening one.")

    def _teardown(self) -> list[str]:
        """Close what this tool opened, never raising. Anything odd comes back as a line."""
        problems: list[str] = []
        for label in ("page", "browser", "playwright"):
            handle = getattr(self, "_" + label)
            setattr(self, "_" + label, None)
            if handle is None:
                continue
            closer = getattr(handle, "close", None) or getattr(handle, "stop", None)
            if closer is None:
                problems.append(f"{label}: nothing callable to close it with")
                continue
            try:
                closer()
            except Exception as exc:
                problems.append(f"{label}: {type(exc).__name__}: {str(exc)[:120]}")
        return problems

    # --- the actions -------------------------------------------------------------

    def _navigate(self, url: str = "", timeout_ms=None, **_) -> ToolResult:
        ms = _clamp_timeout(timeout_ms)
        asked = str(url or "").strip()
        bad = _url_refusal(asked)
        if bad:
            return ToolResult(output=bad, error=True,
                              metadata={"action": "navigate", "refused": True,
                                        "url": asked})
        target = asked if "://" in asked else "http://" + asked
        was_open = self._page is not None
        self._origin = target

        page, refusal = self._ensure_browser()
        if refusal:
            return refusal
        started_at = time.monotonic()
        try:
            response = page.goto(target, timeout=ms, wait_until="domcontentloaded")
        except Exception as exc:
            if _is_dead(exc):
                self._teardown()
                return ToolResult(
                    output=f"browser(action=navigate): the page died during the "
                           f"navigation ({type(exc).__name__}: {exc}). The session "
                           "was closed; navigate again for a fresh one.",
                    error=True, metadata={"action": "navigate", "page_gone": True})
            timed = _is_timeout(exc)
            return ToolResult(
                output=(f"browser(action=navigate): "
                        + (f"no answer from {target} within {_seconds(ms)}" if timed
                           else f"{type(exc).__name__}: {exc}")
                        + ". The page is still open with whatever it did load; read "
                          "it with get_text, or retry with a larger timeout_ms."),
                error=True,
                metadata={"action": "navigate", "timeout": timed,
                          "timeout_ms": ms if timed else None, "url": target})

        elapsed = time.monotonic() - started_at
        status = getattr(response, "status", None) if response is not None else None
        final = str(getattr(response, "url", "") or "") or _url(page) or target
        title = _guard(lambda: page.title(), "")
        text_chars = _text_length(page, ms)
        reported = (f"HTTP status: {status}" if status is not None
                    else "HTTP status: the navigation reported no response object "
                         "(the page never got an answer, or it was not an http one)")
        lines = [f"navigated to: {final}",
                 f"requested: {target}" + ("" if target == final else " (redirected)"),
                 reported,
                 f"title: {title or '(none)'}",
                 "visible text: " + (f"{text_chars} chars" if text_chars is not None
                                     else "not measurable")
                 + f" · took {elapsed:.2f}s of the {_seconds(ms)} timeout",
                 "profile: fresh empty context — no cookies from your browser, and "
                 "nothing is kept after close()",
                 "identity: default Playwright automation flags, no stealth — a site "
                 "that refuses it is refusing us on purpose, and its words come back "
                 "as the answer"]
        if not was_open:
            lines.append("session: a Chromium and a blank page were started for this "
                         "call — anything from an earlier page is gone")
        if isinstance(status, int) and status >= 400:
            lines.append(f"note: the server answered {status}; its own words are in "
                         "get_text, and they are the answer, not a failure of this "
                         "tool")
        return ToolResult(output="\n".join(lines), error=False,
                          metadata={"action": "navigate", "url": final,
                                    "requested": target, "status": status,
                                    "title": title, "timeout_ms": ms})

    def _click(self, selector: str = "", timeout_ms=None, **_) -> ToolResult:
        ms = _clamp_timeout(timeout_ms)
        page, refusal = self._live_page("click")
        if refusal:
            return refusal
        target = str(selector).strip()
        found = _matches(page, target)
        if found is None:
            return ToolResult(
                output=f"browser(action=click): {target!r} is not a CSS selector this "
                       "page can parse, so nothing was clicked.",
                error=True, metadata={"action": "click", "selector": target,
                                      "matched": None})
        matched = len(found)
        lines = [f"click({target!r}): matched {matched} element(s)"]
        if matched == 0:
            lines.append(f"nothing was clicked — no element on the page matches "
                         f"{target!r}")
            lines.append(_where(page))
            lines.append("look at the DOM before guessing again: get_text(\"\") for "
                         "the text, or evaluate a selector count")
            return ToolResult(output="\n".join(lines), error=True,
                              metadata={"action": "click", "selector": target,
                                        "matched": 0, "clicked": False})
        try:
            page.click(target, timeout=ms)
        except Exception as exc:
            timed = _is_timeout(exc)
            gone = self._gone_note(exc)
            lines.append(f"{'timed out' if timed else 'raised ' + type(exc).__name__}: "
                         + (f"the first match was still not clickable after "
                            f"{_seconds(ms)} — hidden, disabled, or covered by "
                            f"something" if timed else str(exc)))
            lines.append(_where(page))
            return ToolResult(output="\n".join(lines) + gone, error=True,
                              metadata={"action": "click", "selector": target,
                                        "matched": matched, "clicked": False,
                                        "timeout": timed, "page_gone": bool(gone),
                                        "timeout_ms": ms if timed else None})
        lines.append(f"clicked the first of {matched}"
                     + (f" — {matched - 1} other element(s) matched and were not "
                        f"clicked, so the selector is ambiguous" if matched > 1 else ""))
        lines.append(_where(page))
        return ToolResult(output="\n".join(lines), error=False,
                          metadata={"action": "click", "selector": target,
                                    "matched": matched, "clicked": True,
                                    "timeout_ms": ms, "url": _url(page)})

    def _type(self, selector: str = "", text: str = "", timeout_ms=None, **_) -> ToolResult:
        ms = _clamp_timeout(timeout_ms)
        page, refusal = self._live_page("type")
        if refusal:
            return refusal
        target = str(selector).strip()
        value = "" if text is None else str(text)
        found = _matches(page, target)
        if found is None:
            return ToolResult(
                output=f"type({target!r}): not a CSS selector this page can parse, so "
                       "nothing was typed.",
                error=True, metadata={"action": "type", "selector": target,
                                      "matched": None})
        matched = len(found)
        lines = [f"type({target!r}): matched {matched} element(s)"]
        if matched == 0:
            lines.append("nothing was typed — no element matches the selector")
            lines.append(_where(page))
            return ToolResult(output="\n".join(lines), error=True,
                              metadata={"action": "type", "selector": target,
                                        "matched": 0, "typed": False})
        try:
            # fill(), not type(): one action that sets the value and fires the
            # input/change events a framework is listening for, instead of racing a
            # synthetic keyboard against a re-render.
            page.fill(target, value, timeout=ms)
        except Exception as exc:
            timed = _is_timeout(exc)
            gone = self._gone_note(exc)
            lines.append(f"{'timed out' if timed else 'failed'}: "
                         + (f"the field exists but was not editable within "
                            f"{_seconds(ms)} — read-only, disabled, or covered"
                            if timed else f"{type(exc).__name__}: {exc}"))
            return ToolResult(output="\n".join(lines) + gone, error=True,
                              metadata={"action": "type", "selector": target,
                                        "matched": matched, "typed": False,
                                        "timeout": timed, "page_gone": bool(gone),
                                        "timeout_ms": ms if timed else None})
        lines.append(f"typed {len(value)} characters"
                     + (" (empty text clears the field)" if not value else "")
                     + (f" into the first of {matched} matches" if matched > 1
                        else " into the field"))
        lines.append(_where(page))
        return ToolResult(output="\n".join(lines), error=False,
                          metadata={"action": "type", "selector": target,
                                    "matched": matched, "typed": True,
                                    "chars": len(value)})

    def _get_text(self, selector: str = "", max_chars=None, timeout_ms=None,
                  **_) -> ToolResult:
        ms = _clamp_timeout(timeout_ms)
        cap = _clamp_chars(max_chars)
        page, refusal = self._live_page("get_text")
        if refusal:
            return refusal
        whole_page = not str(selector or "").strip()
        target = str(selector or "").strip() or "body"
        found = _matches(page, target)
        note = ("no selector given — the whole page is read through `body`"
                if whole_page else "")
        if found is None:
            return ToolResult(
                output=f"browser(action=get_text): {target!r} is not a CSS selector "
                       "this page can parse; no text was read.",
                error=True, metadata={"action": "get_text", "selector": target,
                                      "matched": None})
        matched = len(found)
        lines = [f"get_text({target!r}): matched {matched} element(s)", _where(page)]
        if note:
            lines.append("note: " + note)
        if matched == 0:
            return ToolResult(output="\n".join(lines), error=True,
                              metadata={"action": "get_text", "selector": target,
                                        "matched": 0, "chars": 0})
        chunks: list[str] = []
        problems: list[str] = []
        for element in found[:MAX_MATCHES_READ]:
            try:
                chunks.append(str(element.inner_text() or ""))
            except Exception as exc:
                problems.append(f"{type(exc).__name__}: {str(exc)[:80]}")
        if matched > MAX_MATCHES_READ:
            lines.append(f"only the first {MAX_MATCHES_READ} of {matched} matches "
                         f"were read")
        text = "\n".join(chunk for chunk in chunks if chunk.strip()).strip()
        kept, dropped = _cap(text, cap)
        lines.append(f"chars: {len(kept)} of {len(text)}"
                     + (f" — cut at {cap}, {dropped} dropped; raise max_chars for the "
                        f"rest (max {MAX_TEXT_CHARS})" if dropped else ""))
        if problems:
            lines.append(f"{len(problems)} of the matched elements gave no text "
                         f"(first: {problems[0]}) — an element that is not rendered "
                         "has no inner text")
        return ToolResult(output="\n".join(lines) + "\n--- text ---\n" + kept,
                          error=False,
                          metadata={"action": "get_text", "selector": target,
                                    "matched": matched, "chars": len(kept),
                                    "total_chars": len(text), "dropped": dropped,
                                    "truncated": bool(dropped)})

    def _evaluate(self, js: str = "", max_chars=None, timeout_ms=None, **_) -> ToolResult:
        ms = _clamp_timeout(timeout_ms)
        cap = _clamp_chars(max_chars)
        page, refusal = self._live_page("evaluate")
        if refusal:
            return refusal
        script = str(js or "").strip()
        try:
            raw = page.evaluate(script, timeout=ms)
        except Exception as exc:
            timed = _is_timeout(exc)
            gone = self._gone_note(exc)
            body = (f"evaluate: the page did not answer within {_seconds(ms)}" if timed
                    else "evaluate: the page raised its own exception:\n"
                         + "\n".join(str(exc).splitlines())[:1000])
            return ToolResult(
                output=f"{body}\n{_where(page)}\n"
                       "note: that text came from the page, not from BeeCode — this "
                       "tool wraps the call, it does not swallow it." + gone,
                error=True, metadata={"action": "evaluate", "timeout": timed,
                                      "page_gone": bool(gone),
                                      "exception": type(exc).__name__,
                                      "timeout_ms": ms if timed else None})
        kind = type(raw).__name__
        serialised = ""
        try:
            payload = json.dumps(raw, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as dump_error:
            payload = str(raw)
            serialised = (f"note: the return value is not JSON-serialisable "
                          f"({type(dump_error).__name__}), so it is printed as text")
        kept, dropped = _cap(payload, cap)
        lines = [f"evaluate returned {kind}",
                 f"chars: {len(kept)} of {len(payload)}"
                 + (f" — {dropped} dropped at the {cap} cap" if dropped else ""),
                 _where(page)]
        if serialised:
            lines.append(serialised)
        return ToolResult(output="\n".join(lines) + "\n--- value ---\n" + kept,
                          error=False,
                          metadata={"action": "evaluate", "kind": kind,
                                    "chars": len(kept), "total_chars": len(payload),
                                    "dropped": dropped, "truncated": bool(dropped)})

    def _screenshot(self, path: str = "", attach_base64: bool = False,
                    timeout_ms=None, **_) -> ToolResult:
        ms = _clamp_timeout(timeout_ms)
        page, refusal = self._live_page("screenshot")
        if refusal:
            return refusal
        target = _screenshot_path(path, self._shot)
        self._shot += 1
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult(
                output=f"screenshot: the folder for {target} could not be created "
                       f"({exc}); nothing was written.",
                error=True, metadata={"action": "screenshot", "path": str(target)})
        try:
            page.screenshot(path=str(target), timeout=ms)
        except Exception as exc:
            timed = _is_timeout(exc)
            gone = self._gone_note(exc)
            return ToolResult(
                output=f"screenshot: "
                       + (f"the page did not paint within {_seconds(ms)}" if timed
                          else f"{type(exc).__name__}: {exc}")
                       + f"; no file was written at {target}." + gone,
                error=True, metadata={"action": "screenshot", "path": str(target),
                                      "timeout": timed, "page_gone": bool(gone),
                                      "timeout_ms": ms if timed else None})
        size = _guard(lambda: target.stat().st_size if target.is_file() else -1, -1)
        if size < 0:
            return ToolResult(
                output=f"screenshot: the page reported success but {target} is not a "
                       "readable file, so nothing can be shown or attached. The "
                       "browser may have written somewhere else or not at all.",
                error=True, metadata={"action": "screenshot", "path": str(target),
                                      "bytes": 0})

        human_line, human_body = _human_view(target, size)
        model_line, model_body = _model_view(page, ms)
        lines = [f"screenshot written: {target}", f"bytes: {size}", human_line]
        if human_body:
            lines.append(human_body)
        lines.append(model_line)
        lines.append(model_body)
        if bool(attach_base64):
            lines.append(_base64_view(target))
        else:
            lines.append("base64: not attached — pass attach_base64=true for the "
                         f"bytes (capped at {MAX_BASE64_CHARS} chars, and of no use "
                         "to a keyless model)")
        return ToolResult(output="\n".join(lines), error=False,
                          metadata={"action": "screenshot", "path": str(target),
                                    "bytes": size, "attach_base64": bool(attach_base64),
                                    "human_view": human_line.split(":", 1)[0],
                                    "model_view_chars": len(model_body)})

    def _close(self, **_) -> ToolResult:
        """Tear the session down. Never raises, and never claims more than it closed."""
        had = [label for label in ("page", "browser", "playwright")
               if getattr(self, "_" + label) is not None]
        problems = self._teardown()
        origin = self._origin
        self._origin = ""
        lines = [("closed: " + ", ".join(had)) if had
                 else "nothing was open — this tool holds no page, browser or driver"]
        if origin:
            lines.append(f"last address: {origin}")
        lines.append("state: the context went away with the browser, so a login made "
                     "in this session has to be made again next time")
        if problems:
            lines.append("did not close cleanly: " + "; ".join(problems))
        return ToolResult(output="\n".join(lines), error=False,
                          metadata={"action": "close", "closed": had,
                                    "unclean": problems})


def _url_refusal(asked: str) -> str:
    """Why this address will not be navigated to, or "" when it will.

    A bare `host:port` is accepted — a local dev server is the use case — so the
    scheme is only read where one is really written (`http://`, or `javascript:` and
    friends with no slashes). `file://` is refused for the reason `web_fetch` refuses
    it: the page would hand the disk back through `get_text`.
    """
    if not asked:
        return "browser(action=navigate): no url given"
    authority = _AUTHORITY_RE.match(asked)
    bare = _BARE_SCHEME_RE.match(asked)
    scheme = (authority or bare).group(1).lower() if (authority or bare) else ""
    if scheme and (scheme in _REFUSED_SCHEMES or (authority and scheme not in ("http",
                                                                               "https"))):
        return (f"browser: refusing to navigate to a `{scheme}:` address — only "
                "http(s) pages are driven here. A file:// page hands the disk back to "
                "the model through get_text, and data:/javascript:/blob: is code "
                "rather than a place. Serve the file over http (a local dev server is "
                "the point of this tool) and give me that address.")
    if not authority:
        head = asked.split("/", 1)[0]
        if "." not in head and ":" not in head:
            return (f"browser: {asked!r} is not an address I can navigate to — no "
                    "scheme and no host to add one for. Pass a full "
                    "http(s)://host:port/path, or host:port for a local server.")
    return ""


def _screenshot_path(path: str, shot: int) -> Path:
    """Where a picture lands. Relative names stay relative to the project cwd."""
    raw = str(path or "").strip()
    if not raw:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        raw = str(SCREENSHOT_DIR / f"browser-{stamp}-{shot}.png")
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = Path(os.getcwd()) / target
    return target


def _text_length(page, ms: int):
    """How much visible text there is, or None when the page will not say."""
    value = _guard(lambda: page.evaluate(
        "document.body ? (document.body.innerText || '').length : 0", timeout=ms), None)
    return value if isinstance(value, int) else None


def _url(page) -> str:
    return str(getattr(page, "url", "") or "")


def _title(page) -> str:
    return str(_guard(lambda: page.title(), "") or "")


def _where(page) -> str:
    return f"now at: {_url(page) or '(no url)'} · title: {_title(page) or '(none)'}"


def _human_view(target: Path, size: int) -> tuple[str, str]:
    """What the person sees: the pixels, drawn into their terminal if it can take them.

    `beeagent.ui.graphics` is a separate module with the contract
    `capabilities() -> dict`, `render(path, max_rows) -> str`, `describe(path) -> str`.
    It may not exist at all, so every branch names the fallback it used rather than
    printing nothing and looking like it had succeeded.
    """
    try:
        from importlib import import_module

        graphics = import_module("beeagent.ui.graphics")
    except Exception as exc:
        return (f"human view: not drawn — beeagent.ui.graphics is not importable "
                f"({type(exc).__name__}), so the file was left for you to open",
                f"  the file: {target} ({size} bytes)")

    caps, caps_error = _caps_of(graphics)
    can, evidence = _image_support(caps, caps_error)
    kwargs = {}
    if "max_rows" in _guard(lambda: set(signature(graphics.render).parameters), ()):
        kwargs["max_rows"] = DEFAULT_MAX_ROWS
    # render() is asked even when the terminal has no image protocol: its own
    # contract is to degrade to a text rendering, which is still for the human.
    body = _guard(lambda: graphics.render(str(target), **kwargs), None)
    drawn = ", ".join(f"{key}={value}" for key, value in kwargs.items())
    if body and str(body).strip():
        tail = ("" if can is not False else
                " — no inline-image protocol in this terminal, so the body below is "
                "graphics.render()'s own text rendering, not a picture")
        return (f"human view: drawn by beeagent.ui.graphics.render({drawn or 'path'})"
                f" — {len(str(body))} chars of terminal output standing in for "
                f"{size} bytes of PNG; {evidence}{tail}", str(body))
    why = f"render() returned nothing to draw ({evidence})"
    described = str(_guard(lambda: graphics.describe(str(target)), "") or "")
    if described.strip():
        return (f"human view: fallback used — {why}; this fell back to "
                f"graphics.describe(), which is text about the picture, not the "
                f"picture", described)
    return (f"human view: fallback used — {why}; nothing could draw it and "
            f"graphics.describe() gave nothing, so only the path is reported",
            f"  the file: {target} ({size} bytes)")


def _caps_of(graphics):
    """capabilities()'s dict, or the reason there is not one."""
    try:
        return dict(graphics.capabilities() or {}), ""
    except Exception as exc:
        return {}, f"capabilities() raised {type(exc).__name__}"


def _image_support(caps, caps_error):
    """(can this terminal take images, the phrase that says so).

    The key set is read generously on purpose. An explicit flag (`images`,
    `supported`, ...) answers for itself; the protocol keys
    `beeagent.ui.graphics.capabilities()` really returns — `kitty`, `iterm2`,
    `sixel` — answer by whether any of them is set. `None` means the module said
    nothing usable, and that is what gets reported instead of a guess.
    """
    if caps_error:
        return None, caps_error
    for key in ("images", "inline_images", "image_support", "supported"):
        if key in caps:
            return bool(caps[key]), f"capabilities() says {key}={caps[key]!r}"
    protocols = [key for key in ("kitty", "iterm2", "sixel") if key in caps]
    if protocols:
        phrase = "capabilities() says " + ", ".join(
            f"{key}={bool(caps[key])}" for key in protocols)
        if caps.get("why"):
            phrase += f"; {str(caps['why'])[:170]}"
        return any(caps[key] for key in protocols), phrase
    return None, f"capabilities() named no image-support key ({_caps_note(caps)})"


def _model_view(page, ms: int) -> tuple[str, str]:
    """What the model sees: words about the page. Never pixels, never a silent gap."""
    facts = _guard(lambda: page.evaluate(_SUMMARY_JS, timeout=ms), None)
    facts = facts if isinstance(facts, dict) else {}
    lines = [] if facts else ["  note: the page gave no structure summary — its own "
                              "text is all there is; read it with get_text"]
    title = str(facts.get("title") or _title(page) or "(none)")
    lines.insert(0, f"  title: {title[:200]}")
    lines.insert(1, f"  url: {facts.get('url') or _url(page) or '(none)'}")
    chars = facts.get("textChars", _text_length(page, ms))
    lines.insert(2, "  visible text: "
                    + (f"{chars} chars" if isinstance(chars, int) else "not measurable")
                    + (f" · ready: {facts['ready']}" if facts.get("ready") else ""))
    for key, label in (("headings", "headings"), ("fields", "form fields"),
                       ("buttons", "buttons and their labels")):
        values = [str(item).strip() for item in (facts.get(key) or [])
                  if str(item).strip()]
        shown = "; ".join(value[:120] for value in values[:SUMMARY_ITEMS]) or "none"
        lines.append(f"  {label} ({len(values)}): {shown}"
                     + (f" — {len(values) - SUMMARY_ITEMS} more not shown"
                        if len(values) > SUMMARY_ITEMS else ""))
    return ("model view (text only — the keyless model behind this session cannot see "
            "images, so nothing here is pixels and the picture above is not for its "
            "eyes):", "\n".join(lines))


def _base64_view(target: Path) -> str:
    raw = _guard(lambda: target.read_bytes(), None)
    if raw is None:
        return "base64: the file could not be re-read, so nothing was attached"
    encoded = base64.b64encode(raw).decode("ascii")
    kept, dropped = _cap(encoded, MAX_BASE64_CHARS)
    return (f"base64 ({len(kept)} chars of text standing in for {len(raw)} bytes of "
            f"PNG — for a vision-capable caller, not for this model):\n{kept}"
            + (f"\nbase64: capped at {MAX_BASE64_CHARS} chars, {dropped} dropped; the "
               f"file at {target} is complete on disk" if dropped else ""))


TOOLS = [BrowserTool()]
