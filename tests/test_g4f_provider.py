"""The g4f catalogue is a measurement record, not a wishlist.

Every rule here exists because a name in the picker has to be a name that
answered a real request of ours, keyless, through a provider we named. The
network stays out of it: `_pinned_models()` and `discover_models()` are read off
the installed package, so these tests run offline (and `tests/conftest.py` would
block the door anyway).
"""
import pytest

from beeagent.providers import g4f_provider
from beeagent.providers.g4f_provider import (
    KEYLESS_PROVIDERS,
    KILOCODE_MEASURED,
    MEASURED_SILENT,
    PINNED_MEASURED_MODELS,
    G4fProvider,
    _pinned_models,
    _provider_models,
    _providers_for,
    _rate_limited,
    _retry_hint,
)


@pytest.fixture
def fresh():
    """The catalogue and the "served by" map are class-level caches."""
    G4fProvider._discovered = None
    G4fProvider._upstream_map = None
    yield G4fProvider
    G4fProvider._discovered = None
    G4fProvider._upstream_map = None


def test_kilocode_is_pinned_and_is_a_plain_https_client():
    """API-only: no curl_cffi, no playwright, no nodriver, no cookies, no key.

    g4f builds KiloCode in `Provider/__init__.py` out of `OpenaiTemplate` with
    `api_key = None` and a single `Content-Type` header. The browser-driven
    templates in this g4f need one of three packages that are not installed
    here, so a provider that required one could not answer at all — and the
    attribute check is what keeps a future edit from pinning one by accident.
    """
    pytest.importorskip("g4f", reason="the pin list is only meaningful with g4f present")
    import g4f.Provider as P

    assert "KiloCode" in KEYLESS_PROVIDERS
    cls = getattr(P, "KiloCode")
    assert cls.base_url == "https://api.kilo.ai/api/gateway"
    assert cls.api_key is None
    assert not getattr(cls, "needs_auth", False)
    assert getattr(cls, "headers", None) == {"Content-Type": "application/json"}
    assert cls in _providers_for("kilo-auto/free"), "the advertising route must be tried"
    for spy in ("curl_cffi", "playwright", "nodriver"):
        with pytest.raises(ImportError):
            __import__(spy)


def test_kilocode_ships_no_list_so_its_ids_have_to_be_measured():
    """g4f's own `models` for KiloCode is empty until it fetches 394 ids.

    That fetch is the storefront, and 353 of the names on it answer
    `401 PAID_MODEL_AUTH_REQUIRED` to a keyless request. The pin is what keeps
    the catalogue honest, so the test fails if the provider ever grows a static
    list nobody measured.
    """
    pytest.importorskip("g4f")
    import g4f.Provider as P

    cls = getattr(P, "KiloCode")
    assert not (getattr(cls, "models", None) or []), \
        "KiloCode now advertises a list of its own: re-measure before trusting it"
    assert _provider_models(cls) == list(KILOCODE_MEASURED)


def test_every_pinned_id_is_inside_the_free_namespace():
    """Shape check on the pin: this gateway's free ids carry a `:free` suffix or
    are its own `kilo-auto/*` routes. Anything else on that catalogue is paid."""
    for model in KILOCODE_MEASURED:
        assert model.endswith(":free") or model.startswith("kilo-auto/"), model
    assert len(set(KILOCODE_MEASURED)) == len(KILOCODE_MEASURED)


def test_the_measured_ids_are_the_catalogue(fresh):
    catalog = fresh.discover_models()
    pinned = set(_pinned_models())
    assert set(KILOCODE_MEASURED) <= pinned, "a measured id is missing from the pin"
    assert set(KILOCODE_MEASURED) <= set(catalog)
    extra = set(catalog) - pinned
    assert not extra, f"offered without measuring: {sorted(extra)}"


def test_the_two_older_pins_keep_their_measured_ids(fresh):
    """Adding KiloCode must not cost the user one working model."""
    catalog = fresh.discover_models()
    for model in ("command-a-03-2025", "command-r-plus-08-2024", "command-r-08-2024",
                  "command-r7b-12-2024", "default"):
        assert model in catalog, f"{model} answered before the change and must still be listed"
    assert catalog[0] == "command-a-03-2025", "the widest measured route leads"
    assert set(catalog) & set(MEASURED_SILENT) == set()


def test_a_dead_provider_takes_its_ids_with_it(fresh, monkeypatch):
    """The pin must not outlive the provider it was measured on.

    If g4f renames or deletes KiloCode, `_keyless_providers()` stops handing
    back the class, and the catalogue has to fall back to what still answers
    rather than keep offering 13 names nobody can reach.
    """
    import g4f

    real = g4f.Provider

    class WithoutKilo:
        def __getattr__(self, name):
            if name == "KiloCode":
                raise AttributeError(name)
            return getattr(real, name)

    monkeypatch.setattr(g4f, "Provider", WithoutKilo(), raising=False)
    catalog = fresh.discover_models()
    assert not [m for m in catalog if m.endswith(":free") or m.startswith("kilo-auto/")]
    assert catalog == list(G4fProvider.models), "the old five answer on their own"
    assert PINNED_MEASURED_MODELS["KiloCode"], "the pin itself is untouched for the next install"


def test_a_working_flag_flip_also_removes_the_ids(fresh, monkeypatch):
    """`working` is what `_keyless_providers()` filters on."""
    import g4f.Provider as P

    cls = getattr(P, "KiloCode")
    monkeypatch.setattr(cls, "working", False, raising=False)
    assert cls not in g4f_provider._keyless_providers()
    assert not [m for m in fresh.discover_models() if m in KILOCODE_MEASURED]


def test_served_by_names_kilocode_for_the_measured_ids(fresh):
    mapping = fresh.upstream_map()
    for model in KILOCODE_MEASURED:
        assert mapping.get(model) == ["KiloCode"], f"{model} must be credited to what serves it"
    named = {name for providers in mapping.values() for name in providers}
    assert named <= set(KEYLESS_PROVIDERS)


def test_the_advertising_route_is_asked_first_but_nothing_is_skipped(fresh):
    """Order is latency; the whole pin list is still the retry ladder."""
    ordered = _providers_for("kilo-auto/free")
    assert getattr(ordered[0], "__name__", "") == "KiloCode"
    assert len(ordered) == len(KEYLESS_PROVIDERS)
    unknown = _providers_for("no-such-model")
    assert [getattr(c, "__name__", "") for c in unknown] == list(KEYLESS_PROVIDERS)


def test_a_refusal_at_create_reaches_the_user_as_its_own_words(monkeypatch):
    """The first provider raising before any token arrived used to be a crash.

    `chat_stream` read `got_any` in its handler, and the name was only bound
    after the request had been made — so a KiloCode 429, which g4f raises at
    `create()`, surfaced as `UnboundLocalError` and the rate limit went unsaid.
    """
    import asyncio
    import sys
    import types

    class Completions:
        async def create(self, model, messages, stream=True):
            raise RuntimeError("Error 429: Provider returned error")

    class FakeClient:
        def __init__(self, provider=None):
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=Completions().create))

    monkeypatch.setitem(sys.modules, "g4f.client", types.SimpleNamespace(AsyncClient=FakeClient))

    async def drain():
        pieces = []
        async for kind, text in G4fProvider().chat_stream(
                [{"role": "user", "content": "x"}], model="kilo-auto/free"):
            pieces.append(text)
        return pieces

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(drain())
    assert "429" in str(exc.value), f"the throttle was hidden behind {exc.value!r}"
    assert "wait about a minute" in str(exc.value).lower()


def test_a_throttle_says_what_to_do_and_is_not_looped(monkeypatch):
    """429 is a user-visible event on this gateway, not a reason to hammer it."""
    import asyncio
    import sys
    import types

    assert _rate_limited("Error 429: Provider returned error")
    assert _rate_limited("Daily limit reached for x via Y")
    assert not _rate_limited("connection reset")
    hint = _retry_hint("kilo-auto/free", "Error 429")
    assert "wait about a minute" in hint.lower()
    assert "models" in hint.lower()
    assert _retry_hint("kilo-auto/free", "boom") == ""

    calls = []

    class Completions:
        async def create(self, model, messages, stream=False):
            calls.append(model)
            raise RRateLimit("Error 429: Provider returned error")

    class FakeClient:
        def __init__(self, provider=None):
            self.chat = types.SimpleNamespace(completions=Completions())

    class RRateLimit(Exception):
        pass

    monkeypatch.setitem(sys.modules, "g4f.client", types.SimpleNamespace(AsyncClient=FakeClient))
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(G4fProvider().chat([{"role": "user", "content": "x"}],
                                        model="kilo-auto/free"))
    assert len(calls) == len(KEYLESS_PROVIDERS), "one pass over the pins, no retry storm"
    assert "429" in str(exc.value)
    assert "wait about a minute" in str(exc.value).lower()
