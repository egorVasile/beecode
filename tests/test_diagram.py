"""The diagram tool: what the model sees back has to be true about what it drew."""
import xml.etree.ElementTree as ET

import pytest

from beeagent.tools.diagram import DiagramTool, _wrap


def _root(tmp_path, name):
    """Parse the file the tool wrote: markup that was never escaped fails here."""
    return ET.fromstring((tmp_path / name).read_text(encoding="utf-8"))


def _arrows(root):
    """Every drawn arrow as ((from, to), [pixel waypoints])."""
    return [((line.get("data-from"), line.get("data-to")),
             [tuple(float(v) for v in pair.split(","))
              for pair in line.get("points").split()])
            for line in root.findall(".//{*}polyline")]


def _rects(root):
    """Every block as id -> (x, y, width, height) in pixels."""
    found = {}
    for group in root.findall(".//{*}g"):
        box = group.find("{*}rect")
        found[group.get("data-block")] = (float(box.get("x")), float(box.get("y")),
                                          float(box.get("width")), float(box.get("height")))
    return found


def _crosses(p, q, rect, inset=0.5):
    """Liang-Barsky: does segment p-q run through the inside of rect?

    Inset so an arrow that stops on the border of the block it attaches to is not
    counted as a crossing — a line through someone else's block is.
    """
    x, y, wide, high = rect
    left, right = x + inset, x + wide - inset
    top, bottom = y + inset, y + high - inset
    low, up = 0.0, 1.0
    for axis, lo, hi in ((0, left, right), (1, top, bottom)):
        delta = q[axis] - p[axis]
        if delta == 0:
            if not lo <= p[axis] <= hi:
                return False
            continue
        first, second = (lo - p[axis]) / delta, (hi - p[axis]) / delta
        if first > second:
            first, second = second, first
        low, up = max(low, first), min(up, second)
        if low > up:
            return False
    return True


def _footprint(rows, rect):
    """A block's own cells out of the (right-trimmed) ASCII picture."""
    x1, y1, x2, y2 = rect
    return [[row.ljust(x2 + 1)[x] for x in range(x1, x2 + 1)]
            for row in rows[y1:y2 + 1]]


# Two blocks set diagonally with a third sitting in between them: the plain
# centre-to-centre line, and the plain L, both run into `wall`.
DIAGONAL = [{"id": "a", "label": "start", "x": 0, "y": 0},
            {"id": "b", "label": "finish", "x": 14, "y": 8},
            {"id": "wall", "label": "wall", "x": 12, "y": 4}]


def test_the_result_shows_the_diagram_as_text_the_model_can_read(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tool = DiagramTool()
    result = tool.execute(blocks=[{"id": "a", "label": "first step", "x": 0, "y": 0},
                                  {"id": "b", "label": "second step", "x": 0, "y": 6}],
                          edges=[{"from": "a", "to": "b"}], file="out.svg")
    assert not result.error
    # the labels are inside the boxes, so the drawing is self-describing
    assert "first" in result.output and "second" in result.output
    assert "┌" in result.output and "└" in result.output
    assert "→" in result.output, "the arrow must be visible, not only implied"


def test_overlapping_blocks_are_reported_not_quietly_moved(tmp_path, monkeypatch):
    """The model chooses coordinates; a collision is its mistake to fix."""
    monkeypatch.chdir(tmp_path)
    result = DiagramTool().execute(
        blocks=[{"id": "a", "label": "one", "x": 0, "y": 0},
                {"id": "b", "label": "two", "x": 1, "y": 0}])
    assert any("overlap" in p for p in result.metadata["problems"]), result.metadata


def test_an_arrow_to_a_block_that_does_not_exist_says_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = DiagramTool().execute(blocks=[{"id": "a", "label": "one"}],
                                   edges=[{"from": "a", "to": "ghost"}])
    assert any("ghost" in p for p in result.metadata["problems"])


def test_a_label_that_does_not_fit_is_reported_with_the_room_it_needs(
        tmp_path, monkeypatch):
    """An earlier version drew "sea" where the model wrote "seat"."""
    monkeypatch.chdir(tmp_path)
    result = DiagramTool().execute(
        blocks=[{"id": "a", "label": "alpha", "x": 0, "y": 0},
                {"id": "b", "label": "beta", "x": 8, "y": 0}],
        edges=[{"from": "a", "to": "b", "label": "handshake"}])
    # once in the complaint, never in the drawing
    assert result.output.count("handshake") == 1, result.output
    complaint = [p for p in result.metadata["problems"] if "handshake" in p]
    assert complaint and "needs 9 free cells" in complaint[0], complaint
    assert "drop the label" in complaint[0], "the model needs both ways out"


def test_an_arrow_never_paints_through_a_box(tmp_path, monkeypatch):
    """The box owns its whole footprint, not just its frame."""
    monkeypatch.chdir(tmp_path)
    result = DiagramTool().execute(
        blocks=[{"id": "a", "label": "left", "x": 0, "y": 0},
                {"id": "b", "label": "right", "x": 0, "y": 5},
                {"id": "c", "label": "middle", "x": 6, "y": 2}],
        edges=[{"from": "a", "to": "b"}])
    for line in result.output.splitlines():
        assert "─middle" not in line and "middle─" not in line


def test_a_duplicate_id_is_ignored_with_a_word(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = DiagramTool().execute(
        blocks=[{"id": "a", "label": "one", "x": 0, "y": 0},
                {"id": "a", "label": "two", "x": 0, "y": 5}])
    assert any("duplicate" in p.lower() for p in result.metadata["problems"])


def test_no_blocks_is_an_error_not_an_empty_picture():
    result = DiagramTool().execute(blocks=[])
    assert result.error


def test_a_long_label_wraps_instead_of_running_off_the_box():
    words = "an extremely long label that cannot fit one line".split()
    lines = _wrap(" ".join(words), 10)
    assert all(len(line) <= 10 for line in lines)
    assert " ".join(lines).split() == words, "wrapping must not lose or merge words"


def test_the_svg_is_written_and_escapes_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = DiagramTool().execute(
        blocks=[{"id": "a", "label": "x < 1 & y > 2", "x": 0, "y": 0}], file="d.svg")
    svg = (tmp_path / "d.svg").read_text(encoding="utf-8")
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "&lt;" in svg and "&amp;" in svg, "raw < and & would corrupt the file"
    assert result.metadata["file"] == "d.svg"


def test_a_canvas_that_bursts_is_refused_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = DiagramTool().execute(
        blocks=[{"id": "a", "label": "far", "x": 0, "y": 0},
                {"id": "b", "label": "further", "x": 400, "y": 200}])
    assert any("canvas" in p for p in result.metadata["problems"])


def test_the_model_cannot_write_outside_the_working_directory(tmp_path, monkeypatch):
    """`file` is the model's string; a drawing tool must not become a file clobber."""
    source = tmp_path / "app.py"
    source.write_text("print('real code')\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    result = DiagramTool().execute(blocks=[{"id": "a", "label": "one"}], file="../app.py")
    assert source.read_text(encoding="utf-8") == "print('real code')\n"
    assert result.metadata["file"] == ""
    assert any("outside the working directory" in p
               for p in result.metadata["problems"]), result.metadata


def test_only_drawings_are_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = DiagramTool().execute(blocks=[{"id": "a", "label": "one"}], file="notes.txt")
    assert not (tmp_path / "notes.txt").exists()
    assert any("not an .svg" in p for p in result.metadata["problems"]), result.metadata


def test_an_arrow_with_no_room_is_reported_rather_than_left_invisible(tmp_path, monkeypatch):
    """Kimi's first real attempt: three touching boxes, and one arrow drawn nowhere.

    The picture then read as two unrelated blocks, and the model had no way to
    find out that the line it asked for was not on the page.
    """
    monkeypatch.chdir(tmp_path)
    result = DiagramTool().execute(
        blocks=[{"id": "beecode", "label": "BeeCode", "x": 1, "y": 1},
                {"id": "pool", "label": "pool", "x": 10, "y": 1},
                {"id": "crax", "label": "crax", "x": 17, "y": 1}],
        edges=[{"from": "beecode", "to": "pool", "label": "seat"},
               {"from": "pool", "to": "crax", "label": "key"}])
    invisible = [p for p in result.metadata["problems"] if "invisible" in p]
    assert len(invisible) == 1, result.metadata
    assert "beecode->pool" in invisible[0], invisible


def test_diagram_needs_no_grant_but_readonly_still_refuses_it():
    """Safe to run (it writes one SVG the model just composed), not free of the disk.

    Without `writes_files` the readonly ceiling let it through, and `/permissions
    readonly` promises nothing on the machine changes.
    """
    from beeagent.core.permissions import Permissions

    tool = DiagramTool()
    assert tool.is_safe() and tool.writes_files
    assert Permissions("ask").allows(tool)
    assert not Permissions("readonly").allows(tool)


def test_the_svg_is_valid_xml_holding_every_block_and_arrow(tmp_path, monkeypatch):
    """The file a human opens is markup: it has to carry all three blocks, all
    three arrows, and each arrow's word exactly once."""
    monkeypatch.chdir(tmp_path)
    result = DiagramTool().execute(
        blocks=[{"id": "api", "label": "api gateway", "x": 0, "y": 0},
                {"id": "db", "label": "db & cache", "x": 16, "y": 0},
                {"id": "ui", "label": "web ui", "x": 8, "y": 10}],
        edges=[{"from": "api", "to": "db", "label": "go"},
               {"from": "api", "to": "ui", "label": "ok"},
               {"from": "db", "to": "ui", "label": "use"}],
        file="arch.svg")
    assert not result.metadata["problems"], result.metadata
    root = _root(tmp_path, "arch.svg")

    assert {pair for pair, _ in _arrows(root)} == {("api", "db"), ("api", "ui"),
                                                   ("db", "ui")}
    assert set(_rects(root)) == {"api", "db", "ui"}
    words = [text.text or "" for text in root.findall(".//{*}text")]
    for label in ("api", "gateway", "db", "cache", "web", "ui"):
        assert any(label in word for word in words), f"{label} is not in the file"
    for word in ("go", "ok", "use"):
        assert words.count(word) == 1, f"{word} belongs on one arrow, once"
    heads = [marker.get("id") for marker in root.findall(".//{*}marker")]
    assert len(heads) == len(set(heads)) == 3, "one arrowhead each; a shared id collides"
    for _, points in _arrows(root):
        for p, q in zip(points, points[1:]):
            assert p[0] == q[0] or p[1] == q[1], f"{p}-{q} is not orthogonal"


def test_an_arrow_that_would_cross_a_block_goes_around_it(tmp_path, monkeypatch):
    """The line used to be a straight `line` from centre to centre, so a third
    block sitting between two diagonal ones simply had a stroke through it."""
    monkeypatch.chdir(tmp_path)
    result = DiagramTool().execute(blocks=DIAGONAL,
                                   edges=[{"from": "a", "to": "b", "label": "go"}],
                                   file="round.svg")
    assert not result.metadata["problems"], result.metadata
    boxes = _rects(_root(tmp_path, "round.svg"))
    (_, points), = _arrows(_root(tmp_path, "round.svg"))
    assert len(points) > 2, "the route has to bend around the block in between"
    for p, q in zip(points, points[1:]):
        for bid, rect in boxes.items():
            assert not _crosses(p, q, rect), f"{bid} is crossed by the a->b line"


def test_the_svg_and_the_ascii_draw_the_same_route(tmp_path, monkeypatch):
    """One router answers both renderings, so the picture the model reads back
    cannot disagree with the file the user opens."""
    monkeypatch.chdir(tmp_path)
    tool = DiagramTool()
    boxes = tool._boxes(DIAGONAL, [])
    width, height = tool._extent(boxes)
    owner = tool._ownership(boxes, width, height)

    bare, _ = tool._canvas(boxes, [])
    drawn, problems = tool._canvas(boxes, [{"from": "a", "to": "b"}])
    assert not problems, problems
    assert _footprint(drawn, tool._rect(boxes["wall"])) == \
        _footprint(bare, tool._rect(boxes["wall"])), "the wall owns its cells"

    start, end, kind = tool._anchors(boxes["a"], boxes["b"])
    plain = tool._polyline([start, ((end[0], start[1]) if kind == "h"
                                    else (start[0], end[1])), end])
    assert not tool._clear(owner, width, height, plain), "the test needs a real obstacle"
    route = tool._route(boxes["a"], boxes["b"], boxes, owner, width, height)
    assert tool._clear(owner, width, height, route), "the route goes around it"

    DiagramTool().execute(blocks=DIAGONAL, edges=[{"from": "a", "to": "b"}],
                          file="same.svg")
    (pair, points), = _arrows(_root(tmp_path, "same.svg"))
    assert pair == ("a", "b")
    scale, pad = 9, 20
    walked = set(tool._walk(route))
    for x, y in points:
        cx, cy = int((x - pad) // scale), int((y - pad) // scale)
        assert 0 <= cx < width and 0 <= cy < height, f"{x},{y} is off the page"
        assert (cx, cy) in walked or owner[cy][cx] in ("a", "b"), \
            f"{x},{y} leaves the route the ASCII painted"


def test_an_arrow_ends_on_a_blocks_edge_not_under_its_label(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    DiagramTool().execute(blocks=DIAGONAL, edges=[{"from": "a", "to": "b"}],
                          file="edge.svg")
    boxes = _rects(_root(tmp_path, "edge.svg"))
    for (src, dst), points in _arrows(_root(tmp_path, "edge.svg")):
        for bid, point in ((src, points[0]), (dst, points[-1])):
            x, y, wide, high = boxes[bid]
            on_border = (abs(point[0] - x) < 0.01 or abs(point[0] - (x + wide)) < 0.01
                         or abs(point[1] - y) < 0.01 or abs(point[1] - (y + high)) < 0.01)
            assert on_border, f"{src}->{dst} ends at {point}, off {bid}'s edge"
            assert point != (x + wide / 2, y + high / 2), "the middle is where words are"


def test_a_wrapped_label_stays_inside_its_block(tmp_path, monkeypatch):
    """Fixed 18- and 13-pixel offsets walked the third line out through the
    bottom border of a box that was only three cells tall."""
    monkeypatch.chdir(tmp_path)
    DiagramTool().execute(blocks=[{"id": "a", "label": "aa bb cc dd", "x": 0, "y": 0}],
                          file="fit.svg")
    root = _root(tmp_path, "fit.svg")
    lines = []
    for group in root.findall(".//{*}g"):
        box = group.find("{*}rect")
        x, y = float(box.get("x")), float(box.get("y"))
        wide, high = float(box.get("width")), float(box.get("height"))
        for text in group.findall("{*}text"):
            lines.append(text.text)
            size = float(text.get("font-size"))
            half = 0.62 * size * len(text.text) / 2
            centre, baseline = float(text.get("x")), float(text.get("y"))
            assert x <= centre - half and centre + half <= x + wide, text.text
            assert y <= baseline - 0.75 * size and baseline + 0.25 * size <= y + high
    assert len(lines) >= 3, "four lines is the case that used to overflow"


def test_markup_and_unprintable_characters_cannot_break_the_file(tmp_path, monkeypatch):
    """Whatever the ASCII accepts has to leave a parseable file. XML 1.0 has no
    encoding for a control character and UTF-8 none for a lone surrogate, so both
    used to raise in `write_text` and take the whole drawing down."""
    monkeypatch.chdir(tmp_path)
    DiagramTool().execute(
        blocks=[{"id": "a", "label": 'x < 1 & "y" > 2 \x07\ud800', "x": 0, "y": 0},
                {"id": "b", "label": "next", "x": 0, "y": 9}],
        edges=[{"from": "a", "to": "b", "label": "if & only <"}], file="odd.svg")
    root = _root(tmp_path, "odd.svg")
    words = [text.text or "" for text in root.findall(".//{*}text")]
    assert "if & only <" in words, "escaped on the way out, kept on the way in"
    joined = "".join(words)
    for ch in '<&">':
        assert ch in joined, f"{ch!r} was deleted from the file instead of escaped"
    assert "\ufffd\ufffd" in joined, "the unprintable pair is still marked as odd"
    assert "\x07" not in joined and "\ud800" not in joined
