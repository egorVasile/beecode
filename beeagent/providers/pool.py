"""The `pool` provider — BeeCode talking to your own key pool instead of the keys.

Nothing here holds a credential. The pool does, and this client proves who it is
with the seat token the pool gave it once (`/pool enroll`). That ordering is the
whole design: a key that is never sent to a client cannot be read off one.

Errors are written to be actionable, because the failure modes are the user's
problem and not ours to hide: no seat yet, seat awaiting approval, today's
budget spent, every key rate-limited.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

import httpx

from .base import (LIST_NO_SEAT, LIST_REFUSED, LIST_SEAT_REFUSED, LIST_UNREACHABLE,
                   BaseProvider, ModelCatalog, ProviderStreamError, check_error_frame,
                   default_idle_timeout, read_answer_stream, transport_error)

TIMEOUT_CONNECT = 10.0
# A catalogue read is not an answer: `/models` is fetched while a person is
# looking at the picker, so it waits a short fixed while and the cached or
# shipped list is shown rather than a frozen terminal.
MODELS_TIMEOUT = httpx.Timeout(20.0, connect=TIMEOUT_CONNECT)
# How long `/pool status` waits for the seat row after the heart already woke
# the box. `/pool status` runs on the prompt's own thread.
SEAT_WAIT = 15.0
# A free Render instance sleeps after fifteen idle minutes, and the platform's own
# dashboard says a request that wakes it can wait "50 seconds or more" — measured
# against this pool on 2026-09-23, so the wait is set to cover what they promise
# rather than what sounds reasonable. Without it the first ask of the day reports
# a working pool as broken.
COLD_START_WAIT = 55.0
# Measured on the live pool: waking a slept instance took 22.6 s for an endpoint
# that does no work at all. Enrolment used to allow 15, which made the first
# `/pool enroll` of the day the one call that could not wait for the box to wake.
ENROLL_TIMEOUT = 90.0


class PoolError(RuntimeError):
    pass


def _pool_word() -> str:
    """The pool, named in the language the user is reading in."""
    from beeagent.i18n import L
    return L("the pool", "пул")


_MESSAGE_FOR = {
    401: ("this BeeCode has no seat in the pool — run /pool enroll",
          "у этого BeeCode нет места в пуле — выполни /pool enroll"),
    403: ("the pool refused this seat — re-enroll it with /pool enroll",
          "пул отклонил это место — возьми заново: /pool enroll"),
    413: ("the prompt is bigger than the pool accepts",
          "запрос больше, чем пул принимает"),
    429: ("the pool is rate-limited right now, or today's budget is spent",
          "пул сейчас на лимите или дневная норма выбрана"),
    502: ("the pool could not reach the provider",
          "пул не смог дойти до провайдера"),
    503: ("the pool has no keys for that provider",
          "в пуле нет ключей для этого провайдера"),
}


def _unknown_seat(error: BaseException) -> bool:
    """The pool does not know this token: wiped disk, revoked seat, stale file."""
    return "unknown token" in str(error or "")


def _reason(status: int, body: object) -> str:
    detail = ""
    if isinstance(body, dict):
        # A gateway in front may answer FastAPI-style; do not drop message/detail.
        error = body.get("error")
        if isinstance(error, dict):
            error = error.get("message") or error.get("code") or ""
        detail = str(error or body.get("message") or body.get("detail") or "")
    known = _MESSAGE_FOR.get(status)
    if known:
        from beeagent.i18n import L
        text = L(known[0], known[1])
        return f"{text}{f' — {detail}' if detail and detail not in text else ''}"
    return f"the pool answered {status}{f' — {detail}' if detail else ''}"


def _asleep(url: str, error) -> str:
    """What to say when the pool never picked up — a sleeping box or a wrong address."""
    from beeagent.i18n import L
    return L(f"the pool at {url} did not answer even after waiting for it to wake up — "
             f"a free instance sleeps when nobody uses it, and a wrong address sleeps "
             f"forever ({error.__class__.__name__})",
             f"пул по адресу {url} не ответил, даже подождав его пробуждения — "
             f"бесплатный инстанс засыпает без обращений, а неверный адрес спит вечно "
             f"({error.__class__.__name__})")


def _headers(ident: str) -> dict:
    """The install's public id, never a bearer secret.

    Bearer seat tokens are retired: `#beecode…` names who is asking, and the
    Ed25519 signature on every mutating request proves the private key. A
    stolen id without the key file in `~/.beecode` is a dead id.
    """
    from beeagent import __version__

    return {"X-Bee-Id": ident, "User-Agent": f"beecode/{__version__}"}


def install_id(public_hex: str = "") -> str:
    """This install's public name, stable across wipes and re-enrolls.

    Derived from the install public key alone, so no round trip is needed to
    know it — and no secret travels with it, ever.
    """
    import hashlib

    key = public_hex
    if not key:
        _seed, key, _device = install_key()
    digest = hashlib.sha256(str(key or "").encode()).hexdigest()
    return "#beecode%010d" % (int(digest, 16) % 10**10)


def install_key():
    """This machine's signing key: made once, kept in `~/.beecode`, never sent.

    Per machine rather than per project on purpose — BeeCode keeps its settings in
    the folder it runs from, and an install key per folder would mean a new seat
    for every project the same person opens, which is exactly what the daily seat
    cap exists to limit.
    """
    import os
    import secrets

    from beeagent.utils import ed25519

    override = (os.environ.get("BEECODE_POOL_KEY_FILE") or "").strip()
    path = Path(os.path.expanduser(override)) if override \
        else Path.home() / ".beecode" / "pool-key.json"
    seed = None
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            # Valid JSON of the wrong shape (list/str/null) used to kill the
            # whole client with AttributeError instead of regenerating the key.
            raw = bytes.fromhex(str(stored.get("seed") or "")
                                if isinstance(stored, dict) else "")
            seed = raw if len(raw) == 32 else None
        except (OSError, ValueError, AttributeError):
            seed = None
        if seed is not None and os.name == "posix":
            # A key born under a loose umask stayed 0644: repair on every load.
            try:
                path.chmod(0o600)
            except OSError:
                pass
    if seed is None:
        seed = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: crash/full disk between truncate and write left a torn
        # key that the next start then silently replaced (seat lost).
        import tempfile
        descriptor, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix="pool-key.",
                                                suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"seed": seed.hex()}, handle)
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass          # Windows has no file bits to set
            os.replace(tmp_name, path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
            except OSError:
                pass
        try:
            path.chmod(0o600)
        except OSError:
            pass                      # Windows has no file bits to set
    public = ed25519.public_key(seed)
    return seed, public.hex(), public[:8].hex()


def signed_headers(seed: bytes, public_hex: str, body: bytes) -> dict:
    """Prove the request came from this install, without proving anything twice.

    The signature covers the timestamp, a fresh nonce and the bytes of the body:
    a captured request therefore dies in two minutes, and one captured inside that
    window cannot be sent a second time.
    """
    import hashlib
    import secrets
    import time

    from beeagent.utils import ed25519

    stamp = str(time.time())
    nonce = secrets.token_hex(16)
    digest = hashlib.sha256(body).hexdigest()
    signature = ed25519.sign(seed, f"{stamp}\n{nonce}\n{digest}".encode())
    return {"X-Seat-Timestamp": stamp, "X-Seat-Nonce": nonce,
            "X-Seat-Signature": signature.hex()}


def enroll(url: str, timeout: float = ENROLL_TIMEOUT,
           secret: str = "") -> dict:
    """Ask the pool for a seat, naming this install as the one that owns it.

    Only the public half goes out, and only this once; the pool keeps it and
    refuses every later request that cannot sign with the key beside it. Pools
    that enroll by invitation also want the enroll secret — argument first,
    `BEECODE_POOL_ENROLL_SECRET` second, prompt never (commands ask).
    """
    import os

    seed, public, device = install_key()
    secret = (secret or os.environ.get("BEECODE_POOL_ENROLL_SECRET") or "").strip()
    endpoint = (url or "").strip().rstrip("/") + "/v1/enroll"
    payload = {"device": device, "public_key": public}
    if secret:
        payload["enroll_secret"] = secret
    body = json.dumps(payload).encode()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(endpoint, content=body,
                                   headers={"Content-Type": "application/json"})
    except httpx.HTTPError as e:
        # The first enroll of the day finds a free instance asleep — say so,
        # not a raw traceback.
        raise PoolError(_asleep((url or "").strip(), e)) from e
    if response.status_code != 200:
        raise PoolError(_reason(response.status_code, _safe_json(response)))
    data = _safe_json(response) or {}
    # A 200 with an unexpected shape used to become a bare KeyError at the
    # caller ("token"). Fail here, with the pool's own words when there are any.
    # The seat is the install id now (`id`); a legacy `token` is accepted so an
    # old pool answers a new client while hosts update, one release at a time.
    ident = str(data.get("id") or data.get("token") or "")
    if not ident:
        raise PoolError(_reason(response.status_code, data))
    data["id"] = ident
    return data


def approve_seat(url: str, admin_secret: str, token: str,
                 timeout: float = ENROLL_TIMEOUT) -> dict:
    """Approve a seat as the pool operator.

    The secret travels this once and is never stored — the caller prompts for
    it every time. Returns the pool's answer (`{"approved": true}`).
    """
    from beeagent.i18n import L

    endpoint = (url or "").strip().rstrip("/") + "/v1/admin/approve"
    body = json.dumps({"token": token}).encode()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                endpoint, content=body,
                headers={"Content-Type": "application/json",
                         "X-Admin": admin_secret})
    except httpx.HTTPError as e:
        raise PoolError(_asleep((url or "").strip(), e)) from e
    data = _safe_json(response) or {}
    if response.status_code != 200 or not data.get("approved"):
        raise PoolError(_reason(response.status_code, data))
    # Trust, then verify: read the seat back and confirm it is really open.
    # A 200 that approved nothing used to end here with congratulations.
    try:
        with httpx.Client(timeout=timeout) as client:
            seat = _seat_of(client, (url or "").strip().rstrip("/"), token,
                            min(float(timeout), SEAT_WAIT))
    except Exception as e:
        raise PoolError(L(f"approved, but the seat cannot be read back: {e} — "
                          f"the token the pool approved may not be this install's",
                          f"подтверждено, но место не читается: {e} — "
                          f"возможно, подтверждено не это место")) from e
    if not (seat or {}).get("approved", True):
        raise PoolError(L("the pool confirmed the approve, but this seat still "
                          "reads unapproved — enroll again and approve the new token",
                          "пул подтвердил, но место всё ещё непрочитано как "
                          "подтверждённое — возьми место заново и подтверди его"))
    return data


def pool_status(url: str, token: str, timeout: float = ENROLL_TIMEOUT) -> dict:
    """A small read-only peek: is the pool there, and does it answer to *us*?

    Two different questions, and `/pool status` used to answer the first while
    sounding like the second — a heart that says "alive" says nothing about a
    seat that was revoked this morning. The server's own `/healthz` is anonymous
    by design; the seat is proved against `/v1/seat`, which answers 401 for a
    token the pool does not know.
    """
    endpoint = (url or "").rstrip("/")
    with httpx.Client(timeout=timeout) as client:
        server = _safe_json(client.get(endpoint + "/healthz")) or {}
        # The seat question is asked with a shorter wait than the heart: a box
        # that is waking up has already been woken by the first request, and this
        # runs on the thread that owns the prompt, so every second here is a
        # second the interface cannot answer the user.
        seat = _seat_of(client, endpoint, token, min(float(timeout), SEAT_WAIT))
    return {"ok": bool(server.get("ok", True)), "server": server, "seat": seat}


def _seat_of(client, endpoint: str, token: str, wait: float = SEAT_WAIT) -> dict:
    """What the pool says about *this* seat, or why it cannot say.

    The budget row is private to its install, so unlike the models list this
    route signs: id in the header, signature over an empty body.
    """
    from beeagent.i18n import L

    if not token:
        return {"ok": False,
                "error": L("no seat yet — /pool enroll",
                           "места пока нет — /pool enroll")}
    seed, _public, _device = install_key()
    try:
        response = client.get(
            endpoint + "/v1/seat",
            headers=_headers(token) | signed_headers(seed, _public, b""),
            timeout=wait)
    except httpx.HTTPError as e:
        return {"ok": False, "error": str(transport_error(e, _pool_word()))}
    body = _safe_json(response) or {}
    if response.status_code != 200:
        return {"ok": False,
                "error": _reason(response.status_code, body) or
                         f"the pool answered {response.status_code}"}
    seat = {key: value for key, value in body.items() if key != "object"}
    seat["ok"] = True
    return seat


def _safe_json(response) -> dict | None:
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


class PoolStreamError(ProviderStreamError, PoolError):
    """A stream that cannot be reported as an answer, in the pool's own error type.

    The reader in `base.py` raises `ProviderStreamError`; `except PoolError` is
    written down in this client's callers, so the pool's failure is both.
    """


class PoolProvider(BaseProvider):
    name = "pool"
    # What the pool answers for, measured on the box itself. Re-measured
    # 2026-09-26: the endpoint crax sits on rewrote its catalogue and twelve of the
    # thirteen names offered here since 2026-09-24 stopped existing — `glm-5.3`,
    # `glm-5.3-flash` and `glm-5.2` are what answers a chat now, in that order by
    # measured latency. `/models` still asks the pool for the live list — this is
    # what the picker shows when it cannot be reached, because an empty list reads
    # as "the pool has no models" and sends a person to change provider when the
    # box was only asleep.
    #
    # Read it as a catalogue and not as a promise: once the seat has asked,
    # `self.models` answers with what the pool itself named (see `ModelCatalog`),
    # and `model_list_state` says which of the two the user is looking at.
    models = ModelCatalog(["glm-5.3", "glm-5.3-flash", "glm-5.2"])
    list_source = ("the pool", "пул")

    def __init__(self, url: str = "", token: str = "", idle_timeout: float | None = None):
        self.url = (url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.idle_timeout = float(idle_timeout) if idle_timeout else default_idle_timeout()

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def _timeout(self) -> httpx.Timeout:
        """The wait this client keeps, in the same budget the agent narrates."""
        return httpx.Timeout(self.idle_timeout, connect=TIMEOUT_CONNECT)

    def _body(self, messages: list[dict], model: str, stream: bool) -> dict:
        return {"model": model, "messages": messages, "stream": stream}

    def _require(self) -> str:
        if not self.url:
            from beeagent.i18n import L
            raise PoolError(L("the pool has no address — /pool url https://…",
                              "у пула нет адреса — /pool url https://…"))
        if not self.token:
            from beeagent.i18n import L
            raise PoolError(L("no seat token yet — /pool enroll",
                              "нет токена места — /pool enroll"))
        return self.token

    # Called with a fresh token after auto re-enroll: the agent wires it to
    # persist the config, so the next launch does not re-enroll again. Unset
    # (plain provider use, tests) means live-only.
    on_token = None

    def _re_enroll(self, dead: str) -> str | None:
        """A new seat for a dead token, or None when that cannot help."""
        try:
            fresh = enroll(self.url).get("token") or ""
        except Exception:
            return None
        if not fresh or fresh == dead:
            return None
        self.token = fresh
        if callable(self.on_token):
            try:
                self.on_token(fresh)
            except Exception:
                pass
        return fresh

    # --- the wire -----------------------------------------------------------

    def _failure(self, error: Exception, started: int = 0) -> PoolError:
        """An httpx failure, in words. A class name is not a diagnosis.

        `str(httpx.ReadTimeout(""))` is the empty string, and the retry report
        ended "(last error: )"; a pool that is merely slow then reads as a pool
        that is broken.
        """
        from beeagent.i18n import L

        if isinstance(error, httpx.TimeoutException):
            return PoolError(str(transport_error(error, self.source_name(),
                                                 self.idle_timeout)))
        text = str(transport_error(error, self.source_name(), self.idle_timeout))
        if started:
            return PoolError(L(f"{text} — the answer stopped after {started} characters",
                               f"{text} — ответ оборвался на {started} символах"))
        return PoolError(text)

    async def chat(self, messages: list[dict], model: str = "", stream: bool = False) -> str:
        token = self._require()
        for attempt in (1, 2):
            try:
                return await self._chat(messages, model, token)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                if attempt == 2:
                    raise PoolError(_asleep(self.url, e)) from e
                await asyncio.sleep(COLD_START_WAIT)
            except PoolError as e:
                # The seat died server-side (wiped disk era): enroll fresh and
                # retry once with the new token instead of failing the turn.
                # A token that comes back identical was revoked, not lost —
                # retrying it would loop, so the original error stands.
                if attempt == 1 and _unknown_seat(e):
                    fresh = self._re_enroll(token)
                    if fresh is not None:
                        token = fresh
                        continue
                raise
            except httpx.HTTPError as e:
                raise self._failure(e) from e
        return ""                       # unreachable: every path returns or raises

    def _signed(self, body: dict) -> tuple[bytes, dict]:
        """The bytes to send and the headers that vouch for them.

        Sent as bytes rather than as `json=`: httpx would serialise the payload
        itself, and a signature over a different spelling of the same object is
        not a signature.
        """
        seed, public, _device = install_key()
        raw = json.dumps(body).encode()
        headers = _headers(self._require()) | signed_headers(seed, public, raw)
        headers["Content-Type"] = "application/json"
        return raw, headers

    # A pool 429 is "wait a little", not failure: the box sheds bursts it
    # cannot serve right now, and Retry-After says how long. Bounded, so a
    # truly spent budget still surfaces instead of hanging the turn.
    POOL_429_RETRIES = 3
    POOL_429_WAIT_CAP = 20.0

    @staticmethod
    def _retry_after(response) -> float:
        try:
            header = float(response.headers.get("Retry-After") or 0)
        except (TypeError, ValueError):
            header = 0.0
        body = _safe_json(response) or {}
        try:
            payload = float(body.get("retry_after") or 0)
        except (TypeError, ValueError):
            payload = 0.0
        return min(max(header, payload, 1.0), PoolProvider.POOL_429_WAIT_CAP)

    async def _chat(self, messages: list[dict], model: str, token: str) -> str:
        raw, headers = self._signed(self._body(messages, model, False))
        response = None
        for attempt in range(1 + PoolProvider.POOL_429_RETRIES):
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                response = await client.post(self.url + "/v1/chat/completions", content=raw,
                                             headers=headers)
            if response.status_code == 200:
                break
            if response.status_code == 429 and attempt < PoolProvider.POOL_429_RETRIES:
                await asyncio.sleep(self._retry_after(response))
                continue
            raise PoolError(_reason(response.status_code, _safe_json(response)))
        assert response is not None  # the last attempt raises, never falls through
        body = _safe_json(response) or {}
        choices = body.get("choices") or []
        # Upstream may hand back non-objects; .get on them used to raise
        # AttributeError out of chat(). Non-objects carry no answer.
        if not choices or not isinstance(choices[0], dict):
            return ""
        message = choices[0].get("message") or {}
        if not isinstance(message, dict):
            return ""
        return str(message.get("content") or "")

    async def chat_stream(self, messages: list[dict], model: str = "") -> AsyncIterator:
        token = self._require()
        for attempt in (1, 2):
            started = 0
            try:
                async for pair in self._stream(messages, model, token):
                    started += len(pair[1])
                    yield pair
                return
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # Only a connection that never opened is retried: once the first
                # fragment is on screen, repeating the request would print the
                # answer twice and pay for it twice.
                if started or attempt == 2:
                    raise PoolError(_asleep(self.url, e)) from e
                await asyncio.sleep(COLD_START_WAIT)
            except ProviderStreamError:
                raise                    # the reader already said what happened
            except PoolError as e:
                if started or attempt == 2 or not _unknown_seat(e):
                    raise
                fresh = self._re_enroll(token)
                if fresh is None:
                    raise
                token = fresh
            except httpx.HTTPError as e:
                raise self._failure(e, started) from e

    async def _stream(self, messages: list[dict], model: str, token: str) -> AsyncIterator:
        """One ask, one answer: the shared reader turns the frames into text.

        The pool answers in OpenAI-shaped SSE, so `_pieces` is all this provider
        contributes to the reading. The lines are not: reading them here, one
        `data:` line at a time, is what used to hand back
        "I will now write the file and the third" — the split frame dropped, no
        error raised, the seat charged once for a half answer.

        A 429 before the first frame retries here (bounded, honoring
        Retry-After): overload is a wait, not a failure, and nothing billed yet.
        """
        raw, headers = self._signed(self._body(messages, model, True))
        who = self.source_name()
        for attempt in range(1 + PoolProvider.POOL_429_RETRIES):
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                async with client.stream("POST", self.url + "/v1/chat/completions",
                                         content=raw, headers=headers) as response:
                    if response.status_code == 429 \
                            and attempt < PoolProvider.POOL_429_RETRIES:
                        await response.aread()
                        await asyncio.sleep(self._retry_after(response))
                        continue
                    if response.status_code != 200:
                        await response.aread()
                        raise PoolError(_reason(response.status_code, _safe_json(response)))
                    async for piece in read_answer_stream(response.aiter_lines(),
                                                          lambda event: _pieces(event, who),
                                                          source=who, exc=PoolStreamError):
                        yield piece
                    return

    # --- what the pool answers for -----------------------------------------

    def model_list_note(self) -> str:
        """The three states a seat can be in, told apart in one line."""
        from beeagent.i18n import L

        if self.model_list_state == LIST_NO_SEAT:
            detail = self.model_list_reason
            return L(f"this BeeCode has no seat in the pool{f' ({detail})' if detail else ''} "
                     f"— run /pool enroll. The models below are the list that ships with "
                     f"BeeCode, not what your pool serves",
                     f"у этого BeeCode нет места в пуле{f' ({detail})' if detail else ''} — "
                     f"выполни /pool enroll. Модели ниже — список, поставляемый с BeeCode, "
                     f"а не то, что отвечает твой пул")
        if self.model_list_state == LIST_SEAT_REFUSED:
            detail = self.model_list_reason
            return L(f"the pool refused this seat{f' ({detail})' if detail else ''} — the "
                     f"models below are the list that ships with BeeCode, not what your "
                     f"pool serves. Re-enrol with /pool enroll",
                     f"пул отклонил это место{f' ({detail})' if detail else ''} — модели ниже — "
                     f"список, поставляемый с BeeCode, а не то, что отвечает твой пул. "
                     f"Получи место заново: /pool enroll")
        return super().model_list_note()

    def discover_models(self) -> list[str]:
        """What the pool's accounts can answer for.

        The client often cannot ask the provider itself: crax's Cloudflare blocks
        some networks outright (error 1010, on the model list as much as on a
        chat), while the pool's own egress gets through. So the seat asks the
        pool, and gets only the chat models — the same rule the pool enforces on
        every request it forwards.

        When it cannot ask, the shipped list stands in — but the state is recorded
        either way, because "no seat at all", "this seat was revoked" and "the box
        is asleep" look identical to a live list otherwise, and they are three
        different things for the person holding the keyboard.
        """
        from beeagent.i18n import L

        try:
            token = self._require()
        except PoolError as e:
            self.report_model_list_failure(LIST_NO_SEAT, str(e))
            raise
        try:
            with httpx.Client(timeout=MODELS_TIMEOUT) as client:
                response = client.get(self.url + "/v1/models", headers=_headers(token))
        except httpx.HTTPError as e:
            failure = self._failure(e)
            self.report_model_list_failure(LIST_UNREACHABLE, str(failure))
            raise PoolError(str(failure)) from e
        if response.status_code != 200:
            reason = _reason(response.status_code, _safe_json(response)) or \
                L(f"the pool answered {response.status_code}",
                  f"пул ответил {response.status_code}")
            self.report_model_list_failure(
                LIST_SEAT_REFUSED if response.status_code in (401, 403) else LIST_REFUSED,
                reason)
            raise PoolError(reason)
        body = _safe_json(response) or {}
        ids: list[str] = []
        advertised: dict[str, int] = {}
        for item in body.get("data") or []:
            # A gateway may hand back non-objects; .get on them used to raise
            # AttributeError past the model-list error handling.
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
                try:
                    window = int(item.get("context_window") or 0)
                except (TypeError, ValueError):
                    window = 0
                if window > 0:
                    advertised[str(item["id"])] = window
        if advertised:
            # The operator's promise, filed under model@pool: measured records
            # still win (a probe that finds less overwrites), and anything the
            # pool never named keeps the old guesses. Without this every pool
            # model reads as DEFAULT_WINDOW 8192.
            from beeagent.core import windows

            for model, window in advertised.items():
                windows.remember(model, window, "pool")
        found = self.remember_live_models(ids)
        if not found:
            # An empty answer is the pool's answer, but it is not a catalogue the
            # picker can show, so the shipped list stays on screen — labelled.
            self.report_model_list_failure(LIST_UNREACHABLE,
                                           L("the pool listed no chat models",
                                             "пул не назвал ни одной чат-модели"))
        return found


def _pieces(event: dict, source: str = "") -> list[tuple[str, str]]:
    """The text pieces one OpenAI-shaped frame of the pool's stream carries.

    `reasoning_content` is kept because several pool providers think out loud in
    that field, and the agent shows it separately from the answer. An `error`
    frame is the pool explaining itself, and that sentence belongs in front of
    the user rather than turned into "the pool streamed nothing".
    """
    check_error_frame(event, source or _pool_word(), exc=PoolStreamError)
    choices = event.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(first, dict):
        return []
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return []
    out: list[tuple[str, str]] = []
    content = delta.get("content")
    if isinstance(content, str) and content:
        out.append(("content", content))
    for field in ("reasoning", "reasoning_content", "thinking"):
        value = delta.get(field)
        if isinstance(value, str) and value:
            out.append(("reasoning", value))
    return out
