"""Graphics: decode PNGs and show them to a human terminal or a text model.

Stdlib only (``zlib``, ``struct``, ``base64``, ``os``, ``re``).  Nothing here
ever raises for a bad file: an unsupported or corrupt image comes back as a
plain sentence saying why.

Public contract (a browser plugin is coded against these exact names):

    capabilities() -> dict    {"kitty": bool, "iterm2": bool, "sixel": bool,
                               "truecolor": bool, "why": str}
    render(path, max_rows=24) -> str   what the HUMAN sees
    describe(path) -> str              what the MODEL sees (text, no pixels)
"""

from __future__ import annotations

import base64
import os
import re
import struct
import sys
import zlib

from beeagent.i18n import L

# --------------------------------------------------------------------------
# The honest disclaimer, kept as an inline English/Russian pair like every
# other user-facing string in this repository.
# --------------------------------------------------------------------------

_EN_NO_VISION = (
    "This tool does no OCR and the keyless models BeeCode reaches have no "
    "vision, so a description is not a transcription."
)
_RU_NO_VISION = (
    "Этот инструмент не распознаёт текст, а бесплатные модели, до которых "
    "достает BeeCode, не имеют зрения, поэтому описание — это не транскрипция."
)


# --------------------------------------------------------------------------
# Constants: every escape sequence is built from these, never from a path
# --------------------------------------------------------------------------

ESC = "\x1b"
ST = ESC + "\\"
BEL = "\x07"

KITTY_CHUNK_BYTES = 1024  # keep each OSC payload short

# The text fallback decodes every pixel in pure Python, which costs about 5 s per
# megapixel here; more than this is a frozen terminal, so it is refused by name.
MAX_DECODE_PIXELS = 1_500_000
DEFAULT_COLS = 80
LUMA_RAMP = " .:-=+*#%@"
SGR_TRUECOLOR = ESC + "[38;2;%d;%d;%dm"
SGR_RESET = ESC + "[0m"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

COLOR_TYPE_NAMES = {
    0: "greyscale",
    2: "RGB",
    3: "palette",
    4: "greyscale+alpha",
    6: "RGBA",
}

# colour types we can genuinely reconstruct at bit depth 8
_SUPPORTED_COLOR_TYPES = {0, 2, 6}

_UNSAFE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def quote_path(path) -> str:
    """A path safe to echo into a terminal: control bytes never survive."""
    text = os.fspath(path) if hasattr(path, "__fspath__") else str(path)
    if ("\n" in text) or ("\r" in text) or _UNSAFE_RE.search(text):
        return repr(text)
    return text


def _size_of(path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------

def _env(name: str) -> str:
    try:
        value = os.environ.get(name)
    except Exception:  # pragma: no cover
        return ""
    return value if isinstance(value, str) else ""


def capabilities() -> dict:
    """What the terminal can draw, decided purely from the environment.

    Exactly the five keys the browser plugin reads; `why` names the variable
    that decided them.
    """
    kitty = False
    iterm2 = False
    sixel = False
    truecolor = False
    why_parts = []

    term = _env("TERM")
    term_program = _env("TERM_PROGRAM")
    lc_terminal = _env("LC_TERMINAL")
    color_term = _env("COLORTERM")
    kitty_window = _env("KITTY_WINDOW_ID")

    if kitty_window:
        kitty = True
        why_parts.append("KITTY_WINDOW_ID is set")
    elif "kitty" in term:
        kitty = True
        why_parts.append("TERM contains 'kitty'")

    if term_program == "iTerm.app" or lc_terminal == "iTerm2":
        iterm2 = True
        if term_program == "iTerm.app":
            why_parts.append("TERM_PROGRAM=iTerm.app")
        else:
            why_parts.append("LC_TERMINAL=iTerm2")

    if "sixel" in term:
        sixel = True
        why_parts.append("TERM contains 'sixel'")

    if color_term in ("truecolor", "24bit"):
        truecolor = True
        why_parts.append("COLORTERM=%s" % color_term)
    elif term.endswith("-256color"):
        why_parts.append("TERM ends with '-256color' (256 colours, not 24-bit)")
    else:
        why_parts.append("COLORTERM is not truecolor/24bit and TERM does not "
                         "end with '-256color'")

    if not _stdout_isatty():
        why_parts.append("stdout is not a TTY, so nothing will be drawn")

    return {
        "kitty": kitty,
        "iterm2": iterm2,
        "sixel": sixel,
        "truecolor": truecolor,
        "why": "; ".join(why_parts),
    }


def _stdout_isatty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _terminal_columns() -> int:
    raw = _env("COLUMNS")
    if raw:
        try:
            value = int(raw)
            if 8 <= value <= 1000:
                return value
        except ValueError:
            pass
    try:
        size = os.get_terminal_size()
        if size and 8 <= size.columns <= 1000:
            return size.columns
    except (OSError, ValueError):
        pass
    return DEFAULT_COLS


# --------------------------------------------------------------------------
# PNG decoding
# --------------------------------------------------------------------------

class Unsupported(Exception):
    """Raised internally, converted into a sentence at the public boundary."""


class Decoded(object):
    def __init__(self, width, height, color_type, depth, pixels, interlaced):
        self.width = width
        self.height = height
        self.color_type = color_type
        self.depth = depth
        self.pixels = pixels  # bytearray, row-major, bpp bytes per pixel
        self.interlaced = interlaced

    @property
    def channels(self):
        return {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[self.color_type]

    def rgb_at(self, x, y):
        """Pixel as (r, g, b) in 0..255, alpha composited over white."""
        ch = self.channels
        i = (y * self.width + x) * ch
        if self.color_type in (2, 6):
            r = self.pixels[i]
            g = self.pixels[i + 1]
            b = self.pixels[i + 2]
            if self.color_type == 6:
                a = self.pixels[i + 3] / 255.0
                r = int(r * a + 255.0 * (1.0 - a))
                g = int(g * a + 255.0 * (1.0 - a))
                b = int(b * a + 255.0 * (1.0 - a))
        else:  # greyscale
            r = g = b = self.pixels[i]
        return r, g, b

    def rgba_at(self, x, y):
        ch = self.channels
        i = (y * self.width + x) * ch
        if self.color_type == 6:
            return (self.pixels[i], self.pixels[i + 1], self.pixels[i + 2],
                    self.pixels[i + 3])
        if self.color_type == 2:
            return (self.pixels[i], self.pixels[i + 1], self.pixels[i + 2], 255)
        g = self.pixels[i]
        return (g, g, g, 255)


def _defilter(raw: bytes, width: int, height: int, channels: int) -> bytearray:
    """Undo the per-scanline PNG filters.  Raises Unsupported on bad data."""
    stride = width * channels
    bpp = channels
    expected = height * (stride + 1)
    if len(raw) < expected:
        raise Unsupported(
            "truncated image data: got %d bytes, need at least %d"
            % (len(raw), expected))

    out = bytearray(height * stride)
    prev = bytearray(stride)

    for y in range(height):
        base = y * (stride + 1)
        ftype = raw[base]
        start = base + 1
        cur = bytearray(raw[start:start + stride])

        if ftype == 0:  # None
            pass
        elif ftype == 1:  # Sub
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                cur[i] = (cur[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                if i >= bpp:
                    a, c = cur[i - bpp], prev[i - bpp]
                else:
                    a = c = 0
                b = prev[i]
                p = a + b - c
                pa, pb, pc = p - a, p - b, p - c
                if pa < 0:
                    pa = -pa
                if pb < 0:
                    pb = -pb
                if pc < 0:
                    pc = -pc
                if pa <= pb and pa <= pc:
                    pred = a
                elif pb <= pc:
                    pred = b
                else:
                    pred = c
                cur[i] = (cur[i] + pred) & 0xFF
        else:
            raise Unsupported("unknown scanline filter type %d" % ftype)

        out[y * stride:(y + 1) * stride] = cur
        prev = cur

    return out


def decode_png(data: bytes) -> Decoded:
    """Decode a PNG.  Raises :class:`Unsupported` for anything we can't do."""
    if not isinstance(data, (bytes, bytearray)):
        raise Unsupported("not a byte string")
    data = bytes(data)
    if len(data) < 8 or data[:8] != PNG_SIGNATURE:
        raise Unsupported("not a PNG (bad or missing signature)")

    pos = 8
    width = height = depth = color_type = compression = filter_method = None
    interlaced = 0
    idat = bytearray()
    has_plte = False
    has_trns = False
    seen_iend = False
    expected_chunks = 0

    while pos + 8 <= len(data):
        length = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if length > (1 << 26):
            raise Unsupported("absurd chunk length %d" % length)
        if pos + 4 + length + 4 > len(data):
            raise Unsupported("truncated chunk in the file")
        ctype = data[pos:pos + 4]
        pos += 4
        body = data[pos:pos + length]
        pos += length
        crc = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:
            raise Unsupported("CRC mismatch in the %r chunk"
                              % ctype.decode("latin-1"))
        expected_chunks += 1

        key = ctype.decode("latin-1", "replace")
        if key == "IHDR":
            if len(body) < 13:
                raise Unsupported("short IHDR")
            (width, height, depth, color_type, compression,
             filter_method, interlaced) = struct.unpack_from(
                ">IIBBBBB", body, 0)
        elif key == "IDAT":
            idat += body
        elif key == "PLTE":
            has_plte = True
        elif key == "tRNS":
            has_trns = True
        elif key == "IEND":
            seen_iend = True
            break
        # ancillary chunks are simply skipped

    if not seen_iend:
        raise Unsupported("file ends without an IEND chunk")
    if width is None:
        raise Unsupported("no IHDR chunk")
    if expected_chunks < 2:
        raise Unsupported("no chunks after the signature")
    if width == 0 or height == 0:
        raise Unsupported("image has a zero dimension")

    if interlaced:
        raise Unsupported(
            "interlaced (Adam7) PNG is not supported, only single-pass images")
    if compression != 0 or filter_method != 0:
        raise Unsupported("non-deflate compression or a non-standard filter")
    if depth != 8:
        raise Unsupported(
            "bit depth %d is not supported, only 8-bit channels" % depth)
    if color_type not in _SUPPORTED_COLOR_TYPES:
        name = COLOR_TYPE_NAMES.get(color_type, "type %d" % color_type)
        raise Unsupported("colour type %s is not supported" % name)
    if color_type == 3 or has_plte:
        raise Unsupported("palette PNGs are not supported")
    if has_trns:
        raise Unsupported("palette/tRNS transparency is not supported")
    if not idat:
        raise Unsupported("no IDAT chunks, the image data is missing")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise Unsupported("corrupt image data (%s)" % exc)

    channels = {0: 1, 2: 3, 6: 4}[color_type]
    try:
        pixels = _defilter(raw, width, height, channels)
    except (IndexError, ValueError) as exc:  # defensive, never propagate
        raise Unsupported("corrupt image data (%s)" % exc)

    return Decoded(width, height, color_type, depth, pixels, interlaced)


def _load(path) -> Decoded:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise Unsupported("cannot be read (%s)" % (exc.strerror or exc))
    return decode_png(data)


def _raw_png_bytes(path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


# --------------------------------------------------------------------------
# Human-facing rendering
# --------------------------------------------------------------------------

def _luma(r: int, g: int, b: int) -> float:
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _sample_grid(img: Decoded, cols: int, rows: int):
    """Average-pool the image into rows x cols cells of (lum, r, g, b).

    The sums are taken over whole slices rather than pixel by pixel, which is
    what keeps a 1024x768 image downsample inside a tenth of a second.
    """
    pixels = img.pixels
    ch = img.channels
    width = img.width
    cell_h = img.height / float(rows)
    cell_w = width / float(cols)
    grid = []
    for ry in range(rows):
        y0 = int(ry * cell_h)
        y1 = min(max(y0 + 1, int((ry + 1) * cell_h)), img.height)
        line = []
        for rx in range(cols):
            x0 = int(rx * cell_w)
            x1 = min(max(x0 + 1, int((rx + 1) * cell_w)), width)
            count = (y1 - y0) * (x1 - x0)
            span = (x1 - x0) * ch
            rs = gs = bs = al = 0
            for y in range(y0, y1):
                start = (y * width + x0) * ch
                block = pixels[start:start + span]
                if ch == 1:  # greyscale: one sum feeds all three channels
                    total = sum(block)
                    rs += total
                    gs += total
                    bs += total
                elif ch == 3:
                    rs += sum(block[0::3])
                    gs += sum(block[1::3])
                    bs += sum(block[2::3])
                else:
                    rs += sum(block[0::4])
                    gs += sum(block[1::4])
                    bs += sum(block[2::4])
                    al += sum(block[3::4])
            if not count:
                line.append((0.0, 0.0, 0.0, 0.0))
                continue
            r = rs / count
            g = gs / count
            b = bs / count
            if ch == 4:  # composite the averaged colour over white
                alpha = al / count / 255.0
                r = r * alpha + 255.0 * (1.0 - alpha)
                g = g * alpha + 255.0 * (1.0 - alpha)
                b = b * alpha + 255.0 * (1.0 - alpha)
            line.append((_luma(r, g, b), r, g, b))
        grid.append(line)
    return grid


def _ascii_art(img: Decoded, cols: int, rows: int, truecolor: bool):
    """Return (text, scale_description).  Never more than `rows` lines."""
    grid = _sample_grid(img, cols, rows)
    ramp_last = len(LUMA_RAMP) - 1
    lines = []
    for line in grid:
        buf = []
        for lum, r, g, b in line:
            idx = int(round(lum / 255.0 * ramp_last))
            idx = max(0, min(ramp_last, idx))
            ch = LUMA_RAMP[idx]
            if ch == " ":
                ch = "\u00b7" if truecolor else "."
            if truecolor:
                buf.append(SGR_TRUECOLOR % (int(r), int(g), int(b)) + ch
                           + SGR_RESET)
            else:
                buf.append(ch)
        lines.append("".join(buf))
    scale_x = img.width / float(cols) if cols else 1.0
    scale_y = img.height / float(rows) if rows else 1.0
    scale = ("%.1fx%.1f downsample of %dx%d into %dx%d cells"
             % (scale_x, scale_y, img.width, img.height, cols, rows))
    return "\n".join(lines), scale


def _kitty_frame(payload: str, rows: int, cols: int, more: int) -> str:
    """One kitty graphics frame: constants and integers only, no user text."""
    return "%s]G;a=1;c=%d;r=%d;f=100;t=%d;z=20;m=%d;%s%s" % (
        ESC, max(1, cols), max(1, rows), 0 if more == 0 else 1, more,
        payload, ST)


class _Header:
    """Width and height read straight from IHDR, without touching the pixels."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height


def _header(path) -> _Header:
    with open(path, "rb") as handle:
        signature = handle.read(8)
        if signature != PNG_SIGNATURE:
            raise Unsupported("not a PNG")
        head = handle.read(8 + 13)
    if len(head) < 21 or head[4:8] != b"IHDR":
        raise Unsupported("no IHDR chunk")
    width, height = struct.unpack(">II", head[8:16])
    if not width or not height:
        raise Unsupported("zero dimensions")
    return _Header(width, height)


def _load_capped(path):
    """Decode, but refuse a picture too big to draw in text before starting."""
    head = _header(path)
    pixels = head.width * head.height
    if pixels > MAX_DECODE_PIXELS:
        raise Unsupported(
            "%dx%d (%.1f megapixels) is more than a text fallback can draw in time "
            "(limit %.1f MP). A terminal with the kitty or iTerm2 protocol shows the "
            "same file without decoding it." % (head.width, head.height,
                                                pixels / 1e6,
                                                MAX_DECODE_PIXELS / 1e6))
    return _load(path)


def _ascii_fallback(path, rows, cols) -> str:
    """The last resort for an image protocol that could not read the file."""
    try:
        img = _load_capped(path)
    except Unsupported as exc:
        return "the file could not be embedded and its text fallback is off: %s." % exc
    return _render_ascii(quote_path(path), img, max(1, min(rows, img.height)), cols)


def _render_kitty(path: str, img: Decoded, rows: int, cols: int) -> str:
    try:
        data = _raw_png_bytes(path)
    except OSError:
        return _ascii_fallback(path, rows, cols)
    b64 = base64.b64encode(data).decode("ascii")
    step = KITTY_CHUNK_BYTES * 4  # base64 grows 4 chars per 3 bytes
    pieces = [b64[i:i + step] for i in range(0, len(b64), step)]
    cell_rows = max(1, min(rows, len(pieces)))
    cell_h = max(1, int(round(img.height / float(cell_rows))))
    out = []
    for index, piece in enumerate(pieces):
        more = 2 if index < len(pieces) - 1 else 0
        out.append(_kitty_frame(piece, cell_h if index == 0 else 0,
                                cols, more))
    return "".join(out)


def _render_iterm2(path: str, img: Decoded, rows: int, cols: int) -> str:
    try:
        data = _raw_png_bytes(path)
    except OSError:
        return _ascii_fallback(path, rows, cols)
    name = base64.b64encode(
        os.path.basename(path).encode("utf-8")).decode("ascii")
    height = max(1, min(rows, img.height))
    return "%s]1337;File=inline=1;width=%ds;height=%ds;" \
        "preserveAspectRatio=1;size=%d;name=%s:%s%s" % (
            ESC, max(1, cols), height, len(data), name,
            base64.b64encode(data).decode("ascii"), BEL)


def _render_sixel(path: str, img: Decoded, rows: int, cols: int) -> str:
    """A small grayscale sixel encoder: bands of 6 pixel rows, one colour."""
    width = max(1, min(cols, img.width))
    step_x = max(1, img.width // width)
    band_height = 6
    bands = max(1, min(rows, (img.height + band_height - 1) // band_height))
    out = [ESC + "Pq", "0;0;0", "%d;%d;%d" % (0, 0, 0)]
    for band in range(bands):
        y0 = int(band * img.height / float(bands))
        bits = []
        for x in range(0, img.width, step_x):
            mask = 0
            for dy in range(band_height):
                y = min(img.height - 1, y0 + dy)
                r, g, b = img.rgb_at(x, y)
                if _luma(r, g, b) > 127:
                    mask |= 1 << dy
            bits.append(mask)
        if not bits:
            continue
        out.append("#0;1;1;1")
        index = 0
        while index < len(bits):
            value = bits[index]
            run = 1
            while (index + run < len(bits) and bits[index + run] == value):
                run += 1
            char = chr(63 + value)
            out.append(("!%d%s" % (run, char)) if run > 1 else char * run)
            index += run
        out.append("$")
        if band != bands - 1:
            out.append("-")
    out.append(ESC + "\\")
    return "".join(out)


def _render_ascii(path: str, img: Decoded, rows: int, cols: int) -> str:
    caps = capabilities()
    truecolor = bool(caps.get("truecolor")) and _stdout_isatty()
    art, scale = _ascii_art(img, cols, rows, truecolor)
    header = "%s (%dx%d)" % (quote_path(path), img.width, img.height)
    return "%s\n%s\n[%s]" % (header, art, scale)


def render(path, max_rows: int = 24) -> str:
    """What the HUMAN sees: kitty -> iterm2 -> sixel -> ASCII -> sentence."""
    try:
        max_rows = int(max_rows)
    except (TypeError, ValueError):
        max_rows = 24
    max_rows = max(1, min(200, max_rows))

    caps = capabilities()
    cols = _terminal_columns()

    if not _stdout_isatty():
        size = _size_of(path)
        if size < 0:
            return "%s cannot be shown because it cannot be read." % quote_path(path)
        return "%s (%d bytes) is not drawn because stdout is not a terminal." % (
            quote_path(path), size)

    # kitty and iTerm2 send the file's own bytes: they need the dimensions out of
    # the header and nothing else, so decoding here would be seconds of CPU for a
    # picture the terminal was going to draw from the same bytes anyway.
    embeds_the_file = caps.get("kitty") or caps.get("iterm2")
    try:
        img = _header(path) if embeds_the_file else _load_capped(path)
    except OSError as exc:
        return "%s cannot be read (%s)." % (quote_path(path), exc.strerror or exc)
    except Unsupported as exc:
        return "%s is unsupported: %s." % (quote_path(path), exc)
    except Exception as exc:  # pragma: no cover - absolute last resort
        return "%s could not be rendered (%s)." % (quote_path(path), exc)

    if caps.get("kitty"):
        return _render_kitty(quote_path(path), img, max_rows, cols)
    if caps.get("iterm2"):
        return _render_iterm2(quote_path(path), img, max_rows, cols)
    if caps.get("sixel"):
        return _render_sixel(quote_path(path), img, max_rows, cols)

    rows = max(1, min(max_rows, img.height))
    return _render_ascii(quote_path(path), img, rows, cols)


# --------------------------------------------------------------------------
# Model-facing description
# --------------------------------------------------------------------------

def _luminance_profile(img: Decoded, max_bands: int = 12) -> str:
    step = max(1, img.height // max_bands)
    parts = []
    for y in range(0, img.height, step):
        total = 0.0
        sample_cols = max(1, min(img.width, 32))
        stride = max(1, img.width // sample_cols)
        count = 0
        for x in range(0, img.width, stride):
            r, g, b = img.rgb_at(x, y)
            total += _luma(r, g, b)
            count += 1
        avg = total / max(1, count)
        level = int(round(avg / 255.0 * 9))
        level = max(0, min(9, level))
        parts.append("row%d:%d" % (y, level))
    return " ".join(parts[:max_bands * 2])


def describe(path) -> str:
    """What the MODEL sees: text about the image, never pixels."""
    quoted = quote_path(path)
    size = _size_of(path)
    try:
        img = _load(path)
    except Unsupported as exc:
        return ("%s (%s bytes) is unsupported: %s. "
                "No pixel data is available, so it cannot be described."
                % (quoted, size if size >= 0 else "unknown", exc))
    except Exception as exc:  # pragma: no cover
        return "%s (%s bytes) could not be read: %s." % (
            quoted, size if size >= 0 else "unknown", exc)

    color_name = COLOR_TYPE_NAMES.get(img.color_type, str(img.color_type))
    profile = _luminance_profile(img)
    lines = [
        "image: %s" % quoted,
        "bytes: %d" % size,
        "dimensions: %dx%d (width x height)" % (img.width, img.height),
        "colour type: %s, bit depth %d, %d row(s) of pixel data"
        % (color_name, img.depth, img.height),
        "luminance profile (0 dark .. 9 bright), top to bottom: %s" % profile,
    ]
    dark = [token.split(":")[0] for token in profile.split(" ")
            if token.endswith(":0") or token.endswith(":1")]
    if dark:
        lines.append("dark bands near: %s" % ", ".join(dark[:6]))
    # The disclaimer is model-facing text, so both languages ship in every
    # description: the active one first (L picks it), the other one after.
    lines.append(L(_EN_NO_VISION, _RU_NO_VISION))
    lines.append(L(_RU_NO_VISION, _EN_NO_VISION))
    return "\n".join(lines)


__all__ = ["capabilities", "render", "describe", "decode_png", "quote_path"]
