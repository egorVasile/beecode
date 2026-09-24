"""The market client: an index you can audit, and bytes you can trust.

No test in this file opens a socket. `beeagent.plugins.catalog._httpx` is the one
place httpx is imported, and every test replaces it with a fake that records the
requests and answers from a script — including the case that matters most, the
box that never answers at all.

What is pinned here is the promise the index exists to keep: a listing names its
licence and its commit, an entry with no licence is refused, a download whose
sha256 does not match leaves the disk untouched, and an unreachable market
degrades to the shipped catalog instead of raising.
"""
import hashlib
import io
import json
import zipfile

import httpx
import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core.session import Session
from beeagent.plugins import catalog
from beeagent.plugins.catalog import MarketError, MarketItem, fetch_market, parse_market
from beeagent.plugins.manager import PluginManager
from beeagent.ui.commands import ReplContext, dispatch

POOL = "https://pool.invalid"
ARTIFACT = b"---\nname: demo\n---\n# Demo skill\n\nDo the thing.\n"
WRONG = b"a different file entirely\n"
DIGEST = hashlib.sha256(ARTIFACT).hexdigest()
COMMIT = "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"
DOWNLOAD = "https://raw.invalid/someone/skills/" + COMMIT + "/skills/demo/SKILL.md"


def demo_entry(**over):
    """One index entry, in the shape the live pool serves: `kind`, provenance, hash."""
    entry = {"id": "someone--demo", "name": "demo", "kind": "skill", "license": "MIT",
             "description": "a demo skill",
             "source": {"repo": "someone/skills", "path": "skills/demo", "commit": COMMIT},
             "download": DOWNLOAD, "sha256": DIGEST}
    return {**entry, **over}


def index_body(entries=None, policy=("MIT", "Apache-2.0"), groups=None, **over):
    if groups is None:
        groups = {"skills": entries if entries is not None else [demo_entry()]}
    body = {"generated": "2026-09-24T00:00:00Z",
            "policy": {"allowed_licenses": list(policy)},
            "counts": {"skills": sum(len(v) for v in groups.values())},
            "items": groups}
    return {**body, **over}


# --- httpx, without httpx -------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code = status
        self._body = body
        self.content = content

    def json(self):
        if self._body is None:
            raise ValueError("body is not JSON")
        return self._body


class DeadBox(httpx.ConnectError):
    """What a slept-through or mis-addressed pool raises."""


class FakeClient:
    def __init__(self, pool, timeout):
        self.pool = pool
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        if self.pool.error is not None:
            raise self.pool.error
        if url not in self.pool.routes:
            # A route nobody scripted is a socket nobody meant to open.
            raise AssertionError(f"the market client asked for an unstubbed url: {url}")
        self.pool.requests.append((url, dict(headers or {}), self.timeout))
        answer = self.pool.routes[url]
        return answer() if callable(answer) else answer


class FakeHttpx:
    """Stand-in for the httpx module: the same `Client`, the same exception names."""

    HTTPError = httpx.HTTPError
    ConnectError = httpx.ConnectError

    def __init__(self, routes=None, error=None):
        self.routes = routes or {}
        self.error = error
        self.requests = []

    def Client(self, timeout=None, follow_redirects=False):
        return FakeClient(self, timeout)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """Extensions live in ./.beeagent — keep the repo clean."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture()
def stub(monkeypatch):
    """Install a fake httpx; hand back the factory that serves one."""

    def serve(routes=None, error=None):
        fake = FakeHttpx(routes, error)
        monkeypatch.setattr(catalog, "_httpx", lambda: fake)
        return fake

    return serve


def context(token="seat-token", url=POOL):
    config = BeeConfig()
    config.pool_url = url
    config.pool_token = token
    return ReplContext(agent=None, config=config, session=Session())


def plain(result, width=400):
    """A CommandResult rendered the way the terminal renders it."""
    body = result.output
    if isinstance(body, str):
        return body
    from rich.console import Console

    console = Console(file=io.StringIO(), width=width, force_terminal=False)
    console.print(body)
    return console.file.getvalue()


def flat(result, width=200):
    """...and folded back onto one line: a table caption wraps to the table's own
    width, which is not the sentence's fault."""
    return " ".join(plain(result, width).split())


def market_url():
    return POOL + "/v1/market"


def served(project, name):
    return (project / ".beeagent" / "plugins" / name).exists()


def state(project):
    path = project / ".beeagent" / "plugins.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("installed", {})


# --- the listing ----------------------------------------------------------------

def test_the_market_listing_names_the_licence_and_the_pinned_commit(project, stub):
    stub({market_url(): FakeResponse(200, index_body())})
    text = plain(dispatch(context(), "/plugins market"))

    assert "MIT" in text, "the licence is the whole reason the index exists"
    assert COMMIT[:12] in text, "the commit it was pinned to, short but real"
    assert "someone/skills/skills/demo" in text, "and the repo it came from"
    assert "pool.invalid" in text, "which box answered"
    assert "not reached" not in text


def test_the_listing_asks_the_pool_with_the_seat_it_already_has(project, stub):
    fake = stub({market_url(): FakeResponse(200, index_body())})
    plain(dispatch(context(), "/plugins market"))

    assert [url for url, _h, _t in fake.requests] == [market_url()]
    url, headers, timeout = fake.requests[0]
    assert headers.get("Authorization") == "Bearer seat-token"
    assert timeout == catalog.MARKET_TIMEOUT, "short, and only on an explicit ask"


def test_the_market_listing_counts_what_the_licence_gate_refused(project, stub):
    stub({market_url(): FakeResponse(200, index_body(entries=[
        demo_entry(),
        demo_entry(id="anthropics--pdf", name="pdf", license="Anthropic Doc Use"),
        demo_entry(id="nobody--mystery", name="mystery", license=None),
    ]))})
    text = flat(dispatch(context(), "/plugins market"))

    assert "2 refused" in text, text
    assert "Anthropic Doc Use" not in text and "mystery" not in text


def test_a_filter_narrows_the_market_listing(project, stub):
    stub({market_url(): FakeResponse(200, index_body(entries=[
        demo_entry(),
        demo_entry(id="someone--other", name="other", description="another skill")]))})
    text = plain(dispatch(context(), "/plugins market demo"))

    assert "a demo skill" in text and "another skill" not in text


def test_the_group_and_kind_fields_the_live_index_carries_are_the_ones_used(project, stub):
    """`kind`, not `type`; and the index's own excluded count, not only ours."""
    body = index_body(groups={"wrappers": [demo_entry(name="pack", kind="mcp")]},
                      counts={"skills": 1, "excluded": 14})
    stub({market_url(): FakeResponse(200, body)})
    market = parse_market(body, host="pool.invalid")

    assert market.entries[0].type == "mcp", "an unknown group defers to `kind`"
    assert market.published_excluded == 14
    text = flat(dispatch(context(), "/plugins market"))
    assert "the index itself excludes 14 entries" in text, "what the pool left out is said too"


def test_the_local_catalog_listing_never_opens_a_connection(project, stub):
    fake = stub({})
    text = plain(dispatch(context(), "/plugins"))

    assert "code-review" in text
    assert fake.requests == [], "a listing that needs no network must not use one"


def test_a_missing_licence_field_is_refused_by_the_client_too(project):
    """The pool filters server-side; a stale mirror must not smuggle it back in."""
    body = index_body(entries=[demo_entry(license=None), demo_entry(name="kept")])
    body["policy"]["allowed_licenses"] = ["MIT", "Apache-2.0", "Anthropic Doc Use", None]
    market = parse_market(body, host="pool.invalid")

    assert [e.name for e in market.entries] == ["kept"]
    assert market.excluded == 1, "refused, and counted as refused"


def test_a_licence_the_client_has_never_heard_of_is_refused():
    assert parse_market(index_body(entries=[demo_entry(license="Proprietary")])).entries == []
    assert parse_market(index_body(entries=[demo_entry(license="")])).excluded == 1
    # The index may narrow the allow list, never widen it.
    narrowed = parse_market(index_body(policy=("Apache-2.0",)))
    assert narrowed.entries == []


# --- installing -----------------------------------------------------------------

def test_an_install_off_the_market_writes_the_verified_bytes(project, stub):
    stub({market_url(): FakeResponse(200, index_body()),
          DOWNLOAD: FakeResponse(200, content=ARTIFACT)})
    text = plain(dispatch(context(), "/plugin install demo"))

    written = project / ".beeagent" / "plugins" / "demo" / "SKILL.md"
    assert written.read_bytes() == ARTIFACT, text
    assert "MIT" in text and COMMIT[:12] in text, "provenance is said out loud"
    assert "verified" in text

    source = state(project)["demo"]["source"]
    assert source["kind"] == "market" and source["license"] == "MIT"
    assert source["commit"] == COMMIT and source["sha256"] == DIGEST
    assert source["repo"] == "someone/skills" and source["path"] == "skills/demo"


def test_the_download_is_fetched_without_the_seat_token(project, stub):
    fake = stub({market_url(): FakeResponse(200, index_body()),
                 DOWNLOAD: FakeResponse(200, content=ARTIFACT)})
    dispatch(context(), "/plugin install demo")

    assert [url for url, _h, _t in fake.requests] == [market_url(), DOWNLOAD]
    for url, headers, _t in fake.requests[1:]:
        assert "Authorization" not in headers, f"a seat token travelled to {url}"


def test_a_sha256_mismatch_aborts_the_install_and_writes_nothing(project, stub):
    stub({market_url(): FakeResponse(200, index_body()),
          DOWNLOAD: FakeResponse(200, content=WRONG)})
    text = plain(dispatch(context(), "/plugin install demo"))

    assert "sha256 mismatch" in text, text
    assert hashlib.sha256(WRONG).hexdigest() in text, "what it actually was"
    assert DIGEST in text, "what it promised"
    assert not served(project, "demo")
    assert state(project) == {}, "a refused install is not an installed thing"


def test_an_entry_the_index_gave_no_hash_for_is_refused_before_fetching(project, stub):
    fake = stub({market_url(): FakeResponse(200, index_body(
        entries=[demo_entry(sha256=None)]))})
    text = plain(dispatch(context(), "/plugin install demo"))

    assert "no sha256" in text
    assert [url for url, _h, _t in fake.requests] == [market_url()], "nothing fetched"
    assert not served(project, "demo")


def test_an_entry_the_index_gave_no_download_for_is_refused(project, stub):
    stub({market_url(): FakeResponse(200, index_body(entries=[demo_entry(download=None)]))})
    text = plain(dispatch(context(), "/plugin install demo"))

    assert "no download" in text and "someone/skills" in text, "still names where it is"
    assert not served(project, "demo")


def test_the_gate_runs_at_install_even_if_an_entry_slipped_past_parsing(project, stub):
    """A licence-less item handed straight to the manager is still refused."""
    fake = stub({})
    item = MarketItem(name="sneaky", type="skill", category="skills", description="",
                      source={"kind": "market", "id": "someone--sneaky"},
                      id="someone--sneaky", license="", sha256=DIGEST, download=DOWNLOAD)
    with pytest.raises(MarketError) as caught:
        PluginManager().install("sneaky", item=item)

    assert "licence" in str(caught.value).lower()
    assert fake.requests == [], "refused before a byte was fetched"
    assert not served(project, "sneaky")


def test_an_ambiguous_name_is_refused_not_guessed(project, stub):
    stub({market_url(): FakeResponse(200, index_body(entries=[
        demo_entry(id="anthropics--skill-creator", name="skill-creator"),
        demo_entry(id="openai--skill-creator", name="skill-creator")]))})
    text = plain(dispatch(context(), "/plugin install skill-creator"))

    assert "not unique" in text
    assert "anthropics--skill-creator" in text and "openai--skill-creator" in text
    assert not served(project, "skill-creator")


def test_an_entry_is_still_reachable_by_its_id_when_the_name_is_not_unique(project, stub):
    """Half the live index shares a name with the other half; the id does not."""
    stub({market_url(): FakeResponse(200, index_body(entries=[
        demo_entry(),
        demo_entry(id="openai--demo", description="the other one")])),
        DOWNLOAD: FakeResponse(200, content=ARTIFACT)})
    text = plain(dispatch(context(), "/plugin install someone--demo"))

    assert "installed" in text, text
    assert (project / ".beeagent" / "plugins" / "demo" / "SKILL.md").read_bytes() == ARTIFACT


def test_a_market_plugin_needs_the_same_trust_as_a_git_one(project, stub):
    fake = stub({market_url(): FakeResponse(
        200, index_body(groups={"plugins": [demo_entry(name="pack")]}))})
    text = plain(dispatch(context(), "/plugin install pack"))

    assert "--trust" in text, "someone else's code runs inside BeeCode as tools"
    assert [url for url, _h, _t in fake.requests] == [market_url()], "nothing downloaded"
    assert not served(project, "pack")


def test_an_archive_is_unpacked_into_the_install_folder(project, stub):
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as archive:
        archive.writestr("SKILL.md", ARTIFACT.decode("utf-8"))
    body = index_body(entries=[demo_entry(sha256=hashlib.sha256(packed.getvalue()).hexdigest())])
    stub({market_url(): FakeResponse(200, body),
          DOWNLOAD: FakeResponse(200, content=packed.getvalue())})

    assert "MIT" in plain(dispatch(context(), "/plugin install demo"))
    assert (project / ".beeagent" / "plugins" / "demo" / "SKILL.md").read_bytes() == ARTIFACT


def test_a_name_the_index_invented_cannot_become_a_path_outside_the_install_folder(
        project, stub):
    """`name` is the folder a market entry is written into, and it is remote text."""
    fake = stub({})
    for bad in ("../../escaped", "skills/demo", "nul", "", "x" * 61):
        item = MarketItem(name=bad, type="skill", category="skills", description="",
                          source={"kind": "market"}, id="someone--x", license="MIT",
                          sha256=DIGEST, download=DOWNLOAD)
        with pytest.raises(MarketError) as caught:
            PluginManager().install("x", item=item)
        assert "not a folder name" in str(caught.value), bad
    assert fake.requests == [], "refused before a byte was fetched"
    assert list(project.rglob("escaped*")) == []


def test_remote_text_arrives_without_its_control_characters(project):
    body = index_body(entries=[demo_entry(name="two\nlines",
                                          description="a \x1b]52;c;aGVsbG8=BEL trick",
                                          license="MIT")])
    entry = parse_market(body, host="pool.invalid").entries[0]

    assert entry.name == "two lines" and "\n" not in entry.description
    assert "\x1b" not in entry.description


def test_unpacking_stops_at_the_size_cap(project, monkeypatch):
    """A hash that matches says the bytes are the promised ones, not that they are small."""
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as archive:
        archive.writestr("SKILL.md", "x" * 4096)
    monkeypatch.setattr(catalog, "MAX_ARTIFACT", 64)
    target = project / "out"

    with pytest.raises(MarketError) as caught:
        catalog.unpack(packed.getvalue(), target)

    assert "MiB" in str(caught.value)
    assert sum(p.stat().st_size for p in target.rglob("*")) <= 64


def test_an_archive_that_reaches_outside_its_folder_writes_nothing(project, stub):
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as archive:
        archive.writestr("../../escaped.md", "no\n")
    body = index_body(entries=[demo_entry(sha256=hashlib.sha256(packed.getvalue()).hexdigest())])
    stub({market_url(): FakeResponse(200, body),
          DOWNLOAD: FakeResponse(200, content=packed.getvalue())})
    (project / ".beeagent" / "plugins").mkdir(parents=True)

    text = plain(dispatch(context(), "/plugin install demo"))
    assert "escapes the install folder" in text, text
    assert not (project / "escaped.md").exists()
    assert not served(project, "demo")
    assert list((project / ".beeagent" / "plugins").iterdir()) == [], "no staging left behind"


# --- when the market is not there ------------------------------------------------

def test_an_unreachable_market_falls_back_to_the_local_catalog(project, stub):
    stub({}, error=DeadBox("connect timed out"))
    text = plain(dispatch(context(), "/plugins market"), width=200)

    assert "market not reached" in text
    assert "DeadBox" in text, "the failure names the exception it met, not 'unknown'"
    assert "pool.invalid" in text
    assert "catalog shipped with BeeCode" in text
    assert "code-review" in text and "json-tool" in text, "the local catalog arrived"


def test_a_pool_with_no_market_endpoint_says_so_and_still_lists_locally(project, stub):
    stub({market_url(): FakeResponse(
        503, {"error": "the marketplace index is not installed on this pool"})})
    text = plain(dispatch(context(), "/plugins market"), width=200)

    assert "503" in text and "marketplace index is not installed" in text
    assert "code-review" in text


def test_a_market_that_answers_with_html_is_reported_not_raised(project, stub):
    stub({market_url(): FakeResponse(200, None, content=b"<html>nope</html>")})
    text = plain(dispatch(context(), "/plugins market"), width=200)

    assert "not JSON" in text and "code-review" in text


def test_install_with_no_market_says_which_half_failed(project, stub):
    stub({}, error=DeadBox("name or service not known"))
    text = plain(dispatch(context(), "/plugin install demo"), width=200)

    assert "not in the shipped catalog" in text
    assert "market was not reached" in text
    assert not served(project, "demo")


def test_no_pool_address_is_one_readable_line_and_no_connection(project, stub):
    fake = stub({})
    text = plain(dispatch(context(url="", token=""), "/plugins market"), width=200)

    assert "/pool url" in text and "code-review" in text
    assert fake.requests == [], "an install with no pool asks nothing of anyone"


def test_fetch_market_raises_a_readable_error_of_its_own(project, stub):
    stub({market_url(): FakeResponse(401, {"error": "no seat — /pool enroll first"})})
    with pytest.raises(MarketError) as caught:
        fetch_market(POOL, "expired-token")

    assert "401" in str(caught.value) and "/pool enroll" in str(caught.value)


def test_nothing_in_the_catalog_module_touches_the_network_at_import():
    """The 57-second boot incident is why this test exists."""
    import subprocess
    import sys

    code = ("import sys; import beeagent.plugins.catalog as c; "
            "assert 'httpx' not in sys.modules, 'httpx was imported at boot'; "
            "assert 'httpx' not in sys.modules and c.Catalog().items(); print('clean')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert "clean" in out.stdout, out.stderr[-500:]
