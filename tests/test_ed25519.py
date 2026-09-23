"""The signing arithmetic, checked against things it cannot argue with.

Two of the vectors below are the published RFC 8032 test vectors (TEST 2 and
TEST 3); a third comes from a completely independent implementation, checked live
when it is installed. A hand-written curve that signs and verifies only against
itself is the failure mode this file exists to catch — the first version of it
did exactly that, using X25519's bit-clamp instead of Ed25519's, and every
self-check passed.
"""
import os

import pytest

from beeagent.utils import ed25519

RFC_8032 = [
    # (seed, message, public key, signature) — hex, as in the RFC's own appendix
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb", "72",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7", "af82",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


@pytest.mark.parametrize("seed, message, public, signature", RFC_8032)
def test_the_published_vectors_of_the_standard(seed, message, public, signature):
    seed, message = bytes.fromhex(seed), bytes.fromhex(message)
    assert ed25519.public_key(seed).hex() == public
    assert ed25519.sign(seed, message).hex() == signature
    assert ed25519.verify(bytes.fromhex(signature), message, bytes.fromhex(public))


def test_another_implementation_agrees_byte_for_byte():
    """Not required to be installed; when it is, both sides must accept each
    other's output, which is the only test that catches a private convention."""
    ECC = pytest.importorskip("Crypto.PublicKey.ECC")
    eddsa = pytest.importorskip("Crypto.Signature.eddsa")
    for _ in range(3):
        seed = os.urandom(32)
        message = os.urandom(50)
        key = ECC.import_key(bytes.fromhex("302e020100300506032b657004220420") + seed)
        reference = eddsa.new(key, "rfc8032")
        assert key.public_key().export_key(format="raw") == ed25519.public_key(seed)
        assert reference.sign(message) == ed25519.sign(seed, message)
        assert ed25519.verify(reference.sign(message), message, ed25519.public_key(seed))
        reference.verify(message, ed25519.sign(seed, message))       # raises on failure


def test_a_signature_cares_about_every_byte_of_what_it_covered():
    seed = os.urandom(32)
    public = ed25519.public_key(seed)
    message = b"146.120.36.40 and one question"
    signature = ed25519.sign(seed, message)
    assert ed25519.verify(signature, message, public)
    assert not ed25519.verify(signature, message + b"?", public)
    assert not ed25519.verify(signature, b"", public)
    flipped = bytearray(signature)
    flipped[-1] ^= 1
    assert not ed25519.verify(bytes(flipped), message, public)
    assert not ed25519.verify(signature, message, ed25519.public_key(os.urandom(32)))


def test_junk_is_refused_rather_than_raising():
    """The pool calls this with anything off the network; a traceback in a handler
    thread is a dropped connection, and a dropped connection is a mystery."""
    seed = os.urandom(32)
    good = ed25519.sign(seed, b"x")
    short = ed25519.public_key(seed)[:-1]
    for signature, public in ((good[:-1], ed25519.public_key(seed)),
                              (good, short),
                              (good, bytes(32)),
                              (bytes(64), ed25519.public_key(seed)),
                              (b"\xff" * 64, ed25519.public_key(seed))):
        assert ed25519.verify(signature, b"x", public) is False


def test_a_lost_key_file_only_costs_its_owner_a_seat():
    """A new key means a new enrolment, and the caps are what stop that becoming
    a way to collect seats."""
    assert ed25519.public_key(os.urandom(32)) != ed25519.public_key(os.urandom(32))
    with pytest.raises(ValueError):
        ed25519.public_key(b"too short")
