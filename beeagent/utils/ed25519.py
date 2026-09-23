"""Ed25519 signatures, in plain Python.

Why this file exists: a BeeCode seat should prove *who it is* without ever
sending the thing that proves it. A shared secret does not do that — the pool has
to know it, so it crosses the network once at enrolment and sits in the pool's
database afterwards, where a leaked database hands every seat to the reader. With
a signature keypair the private half is generated on the user's machine, never
leaves it, and the pool stores only what cannot be used to sign anything.

Why hand-written rather than `cryptography`: that package is a Rust extension.
PyPI publishes no wheel for Android, which is a platform BeeCode runs on, and a
login path that only works where a compiler exists is not a login path. The cost
of the trade is that this is math someone has to read, so it is the RFC 8032
reference transcription — kept short, kept literal, and checked against a real
implementation in tests plus the RFC's own vectors.

Not constant-time. Signatures here answer "did this install send this", not "how
long did the password take", and the pool never compares a secret byte-by-byte
against attacker-chosen input.
"""
from __future__ import annotations

import hashlib

q = 2 ** 255 - 19                      # the field
l = 2 ** 252 + 27742317777372353535851937790883648493   # the order of the group
d = -121665 * pow(121666, q - 2, q) % q
i_sqrt = pow(2, (q - 1) // 4, q)


def _hint(message: bytes) -> int:
    return int.from_bytes(hashlib.sha512(message).digest(), "little")


def _recover_x(y: int) -> int:
    x2 = (y * y - 1) * pow(d * y * y + 1, q - 2, q) % q
    x = pow(x2, (q + 3) // 8, q)
    if (x * x - x2) % q != 0:
        x = x * i_sqrt % q
    if (x * x - x2) % q != 0:
        raise ValueError("not a point on the curve")
    return x


def _edwards(p, other):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = other
    a = (y1 - x1) * (y2 - x2) % q
    b = (y1 + x1) * (y2 + x2) % q
    c = t1 * 2 * d * t2 % q
    dd = z1 * 2 * z2 % q
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return e * f % q, g * h % q, f * g % q, e * h % q


def _scalarmult(p, e: int):
    if e == 0:
        return 0, 1, 1, 0
    doubled = _scalarmult(_edwards(p, p), e // 2)
    return _edwards(p, doubled) if e & 1 else doubled


def _encode(point) -> bytes:
    x, y, z, _t = point
    zi = pow(z, q - 2, q)
    x = x * zi % q
    y = y * zi % q
    bits = [(y >> k) & 1 for k in range(255)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(32))


def _decode(data: bytes):
    if len(data) != 32:
        raise ValueError("a point is 32 bytes")
    y = int.from_bytes(data[:31] + bytes([data[31] & 0x7F]), "little")
    if y >= q:
        raise ValueError("not a point on the curve")
    x = _recover_x(y)
    if (x & 1) != (data[31] >> 7):
        x = q - x if x else 0
    if x == 0 and data[31] >> 7:
        raise ValueError("not a point on the curve")
    return x, y, 1, x * y % q


_BASE_GY = 4 * pow(5, q - 2, q) % q
_BASE_GX = _recover_x(_BASE_GY)
BASEPOINT = (q - _BASE_GX if _BASE_GX & 1 else _BASE_GX, _BASE_GY, 1,
             (_BASE_GX if not (_BASE_GX & 1) else q - _BASE_GX) * _BASE_GY % q)


def _expand(seed: bytes):
    """(the scalar, the 32 bytes the public key is made from) for one seed."""
    if len(seed) != 32:
        raise ValueError("an Ed25519 seed is 32 bytes")
    h = hashlib.sha512(seed).digest()
    # RFC 8032's clamp, not X25519's: clear the low three bits and bit 255, and
    # set bit 254. Setting 252 instead — the other convention, one character away
    # — produces keys that sign and verify perfectly against themselves and
    # against nothing else.
    scalar = (int.from_bytes(h[:32], "little") & (2 ** 255 - 8)) | 2 ** 254
    return scalar, h[32:]


def public_key(seed: bytes) -> bytes:
    scalar, prefix = _expand(seed)
    return _encode(_scalarmult(BASEPOINT, scalar))


def sign(seed: bytes, message: bytes) -> bytes:
    """64 bytes: the point R, then S. Deterministic — no random nonce to lose."""
    scalar, prefix = _expand(seed)
    point_r = _scalarmult(BASEPOINT, _hint(prefix + message))
    encoded_r = _encode(point_r)
    s = (_hint(encoded_r + public_key(seed) + message) * scalar
         + _hint(prefix + message)) % l
    return encoded_r + s.to_bytes(32, "little")


def verify(signature: bytes, message: bytes, public: bytes) -> bool:
    """True only for a 64-byte signature from a real key over this exact message."""
    if len(signature) != 64 or len(public) != 32:
        return False
    try:
        point_r = _decode(signature[:32])
        point_a = _decode(public)
        s = int.from_bytes(signature[32:], "little")
        x_check, y_check, z_check, t_check = _scalarmult(BASEPOINT, s)
        combined = _edwards(point_r, _scalarmult(point_a, _hint(signature[:32] + public + message)))
        x2, y2, z2, _t2 = combined
    except (ValueError, IndexError):
        return False
    if s >= 2 ** 255 - 1:          # the scalar must be reduced, or it is malleable
        return False
    return (x_check * z2 - x2 * z_check) % q == 0 and (y_check * z2 - y2 * z_check) % q == 0
