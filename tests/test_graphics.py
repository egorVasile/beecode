"""Tests for beeagent.ui.graphics — PNGs are written by hand, no fixtures."""

from __future__ import annotations

import base64
import struct
import time
import zlib

import pytest

from beeagent.ui import graphics


# --------------------------------------------------------------------------
# Hand-built PNGs (stdlib zlib + struct only)
# --------------------------------------------------------------------------

def chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return (struct.pack(">I", len(data)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))


def png(width, height, color_type, raw_pixels, bit_depth=8, interlace=0,
        extra_chunks=()):
    """Assemble a PNG whose scanlines all use the None filter."""
    channels = {0: 1, 2: 3, 3: 1, 6: 4}[color_type]
    stride = width * channels
    rows = b"".join(
        b"\x00" + bytes(raw_pixels[y * stride:(y + 1) * stride])
        for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type,
                       0, 0, interlace)
    data = graphics.PNG_SIGNATURE + chunk(b"IHDR", ihdr)
    for tag, payload in extra_chunks:
        data += chunk(tag, payload)
    return (data + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def encode_with_filter(raw_pixels, width, height, color_type, filter_type):
    """Re-encode raw pixels using one scanline filter, returning PNG bytes."""
    channels = {0: 1, 2: 3, 6: 4}[color_type]
    stride = width * channels
    out = bytearray()
    prev = bytearray(stride)
    for y in range(height):
        cur = bytearray(raw_pixels[y * stride:(y + 1) * stride])
        line = bytearray(stride)
        for i in range(stride):
            a = cur[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if filter_type == 0:
                predicted = 0
            elif filter_type == 1:
                predicted = a
            elif filter_type == 2:
                predicted = b
            elif filter_type == 3:
                predicted = (a + b) >> 1
            elif filter_type == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predicted = a if (pa <= pb and pa <= pc) else (
                    b if pb <= pc else c)
            else:  # pragma: no cover - guarded by parametrise
                raise AssertionError(filter_type)
            line[i] = (cur[i] - predicted) & 0xFF
        out += bytes([filter_type]) + line
        prev = cur
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (graphics.PNG_SIGNATURE + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(out))) + chunk(b"IEND", b""))


RGB_4X4 = bytes([
    10, 10, 10, 20, 20, 20, 30, 30, 30, 40, 40, 40,
    200, 0, 0, 0, 200, 0, 0, 0, 200, 255, 255, 255,
    50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 160,
    255, 255, 255, 128, 128, 128, 64, 64, 64, 0, 0, 0,
])

RGBA_4X4 = bytes([
    10, 10, 10, 255, 20, 20, 20, 255, 30, 30, 30, 255, 40, 40, 40, 255,
    200, 0, 0, 255, 0, 200, 0, 255, 0, 0, 200, 255, 255, 255, 255, 0,
    50, 60, 70, 12, 80, 90, 100, 34, 110, 120, 130, 56, 140, 150, 160, 78,
    255, 255, 255, 255, 128, 128, 128, 200, 64, 64, 64, 150, 0, 0, 0, 100,
])

EN_NO_VISION = (
    "This tool does no OCR and the keyless models BeeCode reaches have no "
    "vision, so a description is not a transcription."
)
RU_NO_VISION_FRAGMENT = "не имеют зрения"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

CAPABILITY_VARS = ("TERM", "TERM_PROGRAM", "LC_TERMINAL", "COLORTERM",
                   "KITTY_WINDOW_ID", "COLUMNS")


def plain_env(monkeypatch, **overrides):
    """Blank every capability variable, then set only what the test names."""
    for name in CAPABILITY_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def tty(monkeypatch):
    plain_env(monkeypatch, COLUMNS="80")
    monkeypatch.setattr(graphics, "_stdout_isatty", lambda: True)
    return monkeypatch


@pytest.fixture
def no_tty(monkeypatch):
    plain_env(monkeypatch)
    monkeypatch.setattr(graphics, "_stdout_isatty", lambda: False)
    return monkeypatch


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


# --------------------------------------------------------------------------
# Decoding: RGB, RGBA, filters
# --------------------------------------------------------------------------

def test_rgb_4x4_decodes(tmp_path):
    path = write(tmp_path, "rgb.png", png(4, 4, 2, RGB_4X4))
    img = graphics.decode_png(open(path, "rb").read())
    assert (img.width, img.height) == (4, 4)
    assert img.color_type == 2 and img.channels == 3
    assert img.rgb_at(0, 0) == (10, 10, 10)
    assert img.rgb_at(3, 0) == (40, 40, 40)
    assert img.rgb_at(0, 1) == (200, 0, 0)
    assert img.rgb_at(3, 3) == (0, 0, 0)


def test_rgba_4x4_decodes(tmp_path):
    path = write(tmp_path, "rgba.png", png(4, 4, 6, RGBA_4X4))
    img = graphics.decode_png(open(path, "rb").read())
    assert img.channels == 4
    assert img.rgba_at(0, 0) == (10, 10, 10, 255)
    assert img.rgba_at(3, 1) == (255, 255, 255, 0)


def test_greyscale_decodes():
    img = graphics.decode_png(png(4, 2, 0, bytes([0, 85, 170, 255,
                                                  255, 170, 85, 0])))
    assert img.rgb_at(1, 0) == (85, 85, 85)
    assert img.rgb_at(3, 1) == (0, 0, 0)


def test_ascii_view_has_expected_row_count(tmp_path):
    path = write(tmp_path, "rgb.png", png(4, 4, 2, RGB_4X4))
    img = graphics.decode_png(open(path, "rb").read())
    art, scale = graphics._ascii_art(img, 80, 4, False)
    assert len(art.split("\n")) == 4
    art, _ = graphics._ascii_art(img, 80, 2, False)
    assert len(art.split("\n")) == 2
    assert "4x4" in scale


@pytest.mark.parametrize("filter_type", [1, 2, 3, 4])
def test_filters_undone_to_same_pixels(filter_type):
    plain = graphics.decode_png(png(4, 4, 2, RGB_4X4))
    data = encode_with_filter(RGB_4X4, 4, 4, 2, filter_type)
    decoded = graphics.decode_png(data)
    for y in range(4):
        for x in range(4):
            assert decoded.rgb_at(x, y) == plain.rgb_at(x, y), (x, y)


@pytest.mark.parametrize("filter_type", [1, 2, 3, 4])
def test_filters_undone_for_rgba(filter_type):
    data = encode_with_filter(RGBA_4X4, 4, 4, 6, filter_type)
    decoded = graphics.decode_png(data)
    for y in range(4):
        for x in range(4):
            offset = (y * 4 + x) * 4
            assert decoded.rgba_at(x, y) == tuple(RGBA_4X4[offset:offset + 4])


def test_unknown_filter_type_refused(tmp_path, tty):
    """A scanline filter byte outside 0..4 must be refused, not decoded."""
    rows = bytearray()
    for y in range(4):
        rows += b"\x00" + RGB_4X4[y * 12:(y + 1) * 12]
    rows[0] = 5
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
    data = (graphics.PNG_SIGNATURE + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))
    with pytest.raises(graphics.Unsupported) as excinfo:
        graphics.decode_png(data)
    assert "filter" in str(excinfo.value).lower()
    path = write(tmp_path, "f5.png", data)
    assert "unsupported" in graphics.render(path).lower()


# --------------------------------------------------------------------------
# Refusals: interlaced, 16-bit, palette+tRNS, corrupt, missing
# --------------------------------------------------------------------------

def test_interlaced_refused_with_reason(tmp_path, tty):
    path = write(tmp_path, "int.png", png(4, 4, 2, RGB_4X4, interlace=1))
    with pytest.raises(graphics.Unsupported) as excinfo:
        graphics.decode_png(open(path, "rb").read())
    assert "interlac" in str(excinfo.value).lower()
    out = graphics.render(path, max_rows=8)
    assert "unsupported" in out.lower()
    assert "interlac" in out.lower()
    assert "\x1b" not in out and "\x07" not in out


def test_sixteen_bit_refused_with_reason(tmp_path, tty):
    path = write(tmp_path, "deep.png", png(4, 4, 2, RGB_4X4, bit_depth=16))
    with pytest.raises(graphics.Unsupported) as excinfo:
        graphics.decode_png(open(path, "rb").read())
    assert "16" in str(excinfo.value)
    assert "bit depth" in graphics.render(path).lower()


def test_palette_with_trns_refused(tmp_path, tty):
    data = png(4, 4, 3, bytes([0, 1, 0, 1] * 4),
               extra_chunks=[(b"PLTE", bytes([0, 0, 0, 255, 255, 255])),
                             (b"tRNS", b"\x00\x80\xff")])
    path = write(tmp_path, "pal.png", data)
    with pytest.raises(graphics.Unsupported):
        graphics.decode_png(data)
    assert "unsupported" in graphics.render(path).lower()


def test_truncated_chunk_refused_not_raised(tmp_path, tty):
    good = png(4, 4, 2, RGB_4X4)
    path = write(tmp_path, "trunc.png", good[:len(good) // 2])
    with pytest.raises(graphics.Unsupported):
        graphics.decode_png(open(path, "rb").read())
    out = graphics.render(path)
    assert isinstance(out, str) and out
    assert "unsupported" in out.lower()
    assert "\x1b" not in out


def test_truncated_scanline_data_refused(tmp_path, tty):
    """Valid CRCs everywhere, but the inflated IDAT is too short for height."""
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
    data = (graphics.PNG_SIGNATURE + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00" * 20))
            + chunk(b"IEND", b""))
    path = write(tmp_path, "short.png", data)
    with pytest.raises(graphics.Unsupported) as excinfo:
        graphics.decode_png(data)
    assert "truncated" in str(excinfo.value)
    assert "unsupported" in graphics.render(path).lower()


def test_not_a_png_and_empty_file(tmp_path, tty):
    path = write(tmp_path, "junk.png", b"GIF89a and then nothing")
    with pytest.raises(graphics.Unsupported) as excinfo:
        graphics.decode_png(b"GIF89a and then nothing")
    assert "signature" in str(excinfo.value).lower()
    assert "unsupported" in graphics.render(path).lower()
    empty = write(tmp_path, "empty.png", b"")
    assert "unsupported" in graphics.render(empty).lower()


def test_crc_corruption_refused(tmp_path, tty):
    good = bytearray(png(4, 4, 2, RGB_4X4))
    good[-8] ^= 0xFF  # break the IEND CRC
    path = write(tmp_path, "crc.png", bytes(good))
    with pytest.raises(graphics.Unsupported) as excinfo:
        graphics.decode_png(bytes(good))
    assert "crc" in str(excinfo.value).lower()
    assert "unsupported" in graphics.render(path).lower()


def test_missing_file_is_a_sentence(tmp_path, tty):
    out = graphics.render(str(tmp_path / "absent.png"))
    assert "\x1b" not in out
    assert "cannot" in out.lower()


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------

def test_capabilities_contract(monkeypatch):
    plain_env(monkeypatch)
    caps = graphics.capabilities()
    assert set(caps) == {"kitty", "iterm2", "sixel", "truecolor", "why"}
    for key in ("kitty", "iterm2", "sixel", "truecolor"):
        assert isinstance(caps[key], bool)
    assert isinstance(caps["why"], str) and caps["why"]


def test_kitty_flips_from_env(monkeypatch):
    plain_env(monkeypatch, KITTY_WINDOW_ID="12")
    caps = graphics.capabilities()
    assert caps["kitty"] is True
    assert caps["why"].startswith("KITTY_WINDOW_ID")

    plain_env(monkeypatch, TERM="xterm-kitty")
    caps = graphics.capabilities()
    assert caps["kitty"] is True
    assert "TERM" in caps["why"]

    plain_env(monkeypatch, TERM="xterm")
    assert graphics.capabilities()["kitty"] is False


def test_iterm_flips_from_env(monkeypatch):
    plain_env(monkeypatch, TERM_PROGRAM="iTerm.app")
    caps = graphics.capabilities()
    assert caps["iterm2"] is True and "TERM_PROGRAM" in caps["why"]

    plain_env(monkeypatch, LC_TERMINAL="iTerm2")
    caps = graphics.capabilities()
    assert caps["iterm2"] is True and "LC_TERMINAL" in caps["why"]

    plain_env(monkeypatch, TERM_PROGRAM="Apple_Terminal")
    assert graphics.capabilities()["iterm2"] is False


def test_sixel_and_colour_depth(monkeypatch):
    plain_env(monkeypatch, TERM="sixel")
    caps = graphics.capabilities()
    assert caps["sixel"] is True and "TERM" in caps["why"]

    plain_env(monkeypatch, COLORTERM="truecolor")
    assert graphics.capabilities()["truecolor"] is True
    plain_env(monkeypatch, COLORTERM="24bit")
    assert graphics.capabilities()["truecolor"] is True

    plain_env(monkeypatch, TERM="xterm-256color")
    caps = graphics.capabilities()
    assert caps["truecolor"] is False and "256" in caps["why"]

    plain_env(monkeypatch, COLORTERM="other")
    assert graphics.capabilities()["truecolor"] is False


def test_non_tty_is_named_in_why(monkeypatch):
    plain_env(monkeypatch, TERM="xterm-256color")
    monkeypatch.setattr(graphics, "_stdout_isatty", lambda: False)
    caps = graphics.capabilities()
    assert "TTY" in caps["why"] and "stdout" in caps["why"]
    assert "TERM" in caps["why"]  # the colour decision is named too


# --------------------------------------------------------------------------
# render(): non-TTY, escape order, row budget
# --------------------------------------------------------------------------

def test_non_tty_draws_nothing_and_names_size(tmp_path, no_tty):
    path = write(tmp_path, "rgb.png", png(4, 4, 2, RGB_4X4))
    out = graphics.render(path)
    assert "\x1b" not in out
    assert "\x07" not in out
    for char in out:
        assert ord(char) >= 32 or char == "\n"
    assert out.count("\n") == 0
    assert path in out
    assert "%d bytes" % len(png(4, 4, 2, RGB_4X4)) in out


def test_kitty_sequence_is_preferred(tmp_path, tty):
    data = png(4, 4, 2, RGB_4X4)
    path = write(tmp_path, "k.png", data)
    tty.setenv("KITTY_WINDOW_ID", "3")
    out = graphics.render(path, max_rows=6)
    assert out.startswith("\x1b]G;")
    assert "f=100" in out
    payloads = "".join(frame.split(";")[-1].split("\x1b")[0]
                       for frame in out.split("\x1b]G;")[1:])
    assert payloads == base64.b64encode(data).decode("ascii")


def test_kitty_payload_is_chunked(tmp_path, tty):
    """A PNG larger than the chunk budget ships as several frames."""
    noisy = bytearray()
    for y in range(64):
        for x in range(64):
            noisy += bytes([(x * 7 + y * 13) & 0xFF,
                            (x * 31 + y) & 0xFF, (x + y * 3) & 0xFF])
    data = png(64, 64, 2, bytes(noisy))
    path = write(tmp_path, "noise.png", data)
    tty.setenv("KITTY_WINDOW_ID", "1")
    out = graphics.render(path, max_rows=8)
    expected = base64.b64encode(data).decode("ascii")
    frames = out.split("\x1b]G;")[1:]
    chunk_len = graphics.KITTY_CHUNK_BYTES * 4
    assert len(frames) == -(-len(expected) // chunk_len) > 1
    assert "".join(f.split(";")[-1].split("\x1b")[0]
                   for f in frames) == expected
    assert "m=2;" in out


def test_iterm2_inline_is_next(tmp_path, tty):
    data = png(4, 4, 2, RGB_4X4)
    path = write(tmp_path, "i.png", data)
    tty.setenv("TERM_PROGRAM", "iTerm.app")
    out = graphics.render(path, max_rows=4)
    assert "\x1b]1337;File=inline=1" in out
    assert out.endswith(base64.b64encode(data).decode("ascii") + "\x07")
    assert "width=80s" in out and "height=4s" in out


def test_sixel_after_iterm(tmp_path, tty):
    path = write(tmp_path, "s.png", png(4, 4, 2, RGB_4X4))
    tty.setenv("TERM", "sixel")
    out = graphics.render(path, max_rows=3)
    assert out.startswith("\x1bPq")
    assert out.endswith("\x1b\\")


def test_ascii_fallback_never_exceeds_max_rows(tmp_path, tty):
    rows = bytearray()
    for y in range(40):
        rows += bytes([y * 6, y * 6, y * 6]) * 20
    path = write(tmp_path, "a.png", png(20, 40, 2, bytes(rows)))
    out = graphics.render(path, max_rows=7)
    lines = out.split("\n")
    assert lines[0].endswith("20x40)")  # header names the file and size
    assert len(lines) - 2 == 7  # exactly max_rows art lines
    assert "downsample" in lines[-1] and "20x40" in lines[-1]
    assert not any(ord(char) < 32 for char in "".join(lines))


def test_ascii_uses_ramp_and_states_scale(tmp_path, tty):
    rows = bytearray()
    for y in range(12):
        value = 0 if y < 6 else 255
        rows += bytes([value, value, value]) * 24
    path = write(tmp_path, "bands.png", png(24, 12, 2, bytes(rows)))
    out = graphics.render(path, max_rows=6)
    lines = out.split("\n")
    art = lines[1:1 + 6]
    assert len(art) == 6
    assert set("".join(art)) <= set(graphics.LUMA_RAMP)
    assert set(art[0]) <= set(" .")  # dark rows map to the dim end
    assert set(art[-1]) == {"@"}  # bright rows map to the top of the ramp
    assert "downsample" in out and "24x12" in out


def test_ascii_colour_only_with_truecolor(tmp_path, tty):
    data = png(4, 4, 2, RGB_4X4)
    path = write(tmp_path, "c.png", data)
    assert "\x1b[38;2;" not in graphics.render(path, max_rows=4)
    tty.setenv("COLORTERM", "truecolor")
    assert "\x1b[38;2;" in graphics.render(path, max_rows=4)


def test_render_rejects_bad_max_rows_without_crash(tmp_path, tty):
    path = write(tmp_path, "z.png", png(4, 4, 2, RGB_4X4))
    assert graphics.render(path, max_rows=0)
    assert graphics.render(path, max_rows=-5)


# --------------------------------------------------------------------------
# Safety: control characters in a path
# --------------------------------------------------------------------------

def test_escape_in_filename_cannot_reach_the_screen(tmp_path, no_tty):
    nasty = str(tmp_path / "ev\x1b[2;37;1337m\x07\nthen.png")
    quoted = graphics.quote_path(nasty)
    assert "\x1b" not in quoted and "\x07" not in quoted
    assert "\n" not in quoted

    described = graphics.describe(nasty)
    assert "\x1b" not in described and "\x07" not in described
    assert "\\x1b" in described  # repr-style, never raw

    rendered = graphics.render(nasty)
    assert "\x1b" not in rendered and "\x07" not in rendered


def test_bell_filename_render_non_tty(tmp_path, no_tty):
    nasty = str(tmp_path / "be\x07llo.png")
    out = graphics.render(nasty)
    assert "\x07" not in out
    assert "be\\x07llo.png" in out


def test_escape_never_built_from_path_bytes(tmp_path, tty):
    """A kitty-capable terminal must still not leak raw path bytes."""
    nasty = str(tmp_path / "k\x1b]G;evil\x07.png")
    out = graphics.render(nasty, max_rows=4)
    # the file does not exist / cannot decode, so no sequence at all
    assert "\x1b" not in out
    assert "unsupported" in out.lower() or "cannot" in out.lower()


# --------------------------------------------------------------------------
# describe()
# --------------------------------------------------------------------------

def test_describe_mentions_dimensions_and_both_languages(tmp_path):
    path = write(tmp_path, "d.png", png(4, 4, 2, RGB_4X4))
    out = graphics.describe(path)
    assert "4x4" in out
    assert "%d bytes" % len(png(4, 4, 2, RGB_4X4)) in out or "bytes:" in out
    assert "RGB" in out
    assert EN_NO_VISION in out
    assert RU_NO_VISION_FRAGMENT in out
    assert "does no OCR" in out and "no vision" in out


def test_describe_luminance_profile_expresses_dark_band_on_top(tmp_path):
    rows = bytearray()
    rows += bytes([4, 4, 4]) * 24 * 6
    rows += bytes([250, 250, 250]) * 24 * 6
    path = write(tmp_path, "bands.png", png(24, 12, 2, bytes(rows)))
    out = graphics.describe(path)
    assert "luminance profile" in out
    levels = [int(token.split(":")[1]) for token in out.split()
              if token.startswith("row") and ":" in token]
    assert levels and levels[0] < levels[-1]
    assert "dark band" in out.lower()


def test_describe_of_rgba_reports_colour_type(tmp_path):
    path = write(tmp_path, "r.png", png(4, 4, 6, RGBA_4X4))
    out = graphics.describe(path)
    assert "RGBA" in out and "4x4" in out


def test_describe_of_unsupported_is_a_sentence(tmp_path):
    path = write(tmp_path, "bad.png", b"\x89PNG\r\n\x1a\nbroken")
    out = graphics.describe(path)
    assert "unsupported" in out.lower()
    assert "\x1b" not in out


def test_describe_of_missing_file(tmp_path):
    out = graphics.describe(str(tmp_path / "ghost.png"))
    assert isinstance(out, str) and out
    assert "cannot" in out.lower() or "unsupported" in out.lower()


# --------------------------------------------------------------------------
# Timing for a real-sized image (reported, not asserted tightly)
# --------------------------------------------------------------------------

def test_render_1024x768_timing(tmp_path, tty, capsys):
    width, height = 1024, 768
    rows = bytearray()
    dark = bytes([8, 8, 8]) * width
    middle = bytes([200, 30, 30]) * width
    light = bytes([250, 250, 250]) * width
    for y in range(height):
        rows += dark if y < height // 3 else (
            middle if y < 2 * height // 3 else light)
    data = png(width, height, 2, bytes(rows))
    path = write(tmp_path, "big.png", data)

    start = time.time()
    out = graphics.render(path, max_rows=24)
    elapsed = time.time() - start
    with capsys.disabled():
        print("\n[graphics] 1024x768 render: %.2fs (%d bytes PNG)"
              % (elapsed, len(data)))
    assert out.count("\n") <= 24 + 2
    assert "downsample" in out
    assert elapsed < 20.0
