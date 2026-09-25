"""The diagram tool: what the model sees back has to be true about what it drew."""
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from beeagent.tools.diagram import (DiagramTool, MAX_LABEL_CHARS, MAX_LABEL_LINES,
                                    MAX_SVG_BYTES, _inside, _odd_name, _wrap)


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


# --- links: the machine decides whether it lets an unprivileged test make one --

def _drop(link):
    """Remove a link or a junction without touching whatever it points at."""
    for op in (os.unlink, os.rmdir):
        try:
            op(str(link))
            return
        except OSError:
            continue


def _link_dir(link, target) -> bool:
    """Point `link` at a directory `target`: a junction on Windows, a symlink
    elsewhere. False when this machine will let us make neither, which is the
    case `test_the_target_is_the_resolved_path` still covers without one."""
    if os.name == "nt":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True)
        if made.returncode == 0 and link.is_dir():
            return True
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        _drop(link)
        return False


def _link_file(link, target) -> bool:
    try:
        os.symlink(str(target), str(link))
        return True
    except (OSError, NotImplementedError):
        _drop(link)
        return False


def _neighbour(tmp_path, label) -> Path:
    """A directory beside the one the tool may write into, not inside it."""
    path = tmp_path.parent / f"outside-{label}-{os.getpid()}"
    path.mkdir(exist_ok=True)
    return path


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


# --- where the SVG may land: the path the filesystem will use, not the string --

def test_a_symlinked_svg_name_does_not_write_through_the_link(tmp_path, monkeypatch):
    """The audit's first HIGH: a repo that shipped `diagram.svg -> ../victim.py`
    had that file replaced by markup, with no grant involved. `..` is not the
    escape; the resolved path is, so that is what has to sit under the cwd."""
    outside = _neighbour(tmp_path, "symlink")
    victim = outside / "victim-source.py"
    victim.write_text("print('real code')\n", encoding="utf-8")
    work = tmp_path / "repo"
    work.mkdir()
    if not _link_file(work / "diagram.svg", victim):
        pytest.skip("this machine makes no symlinks without extra privilege")
    monkeypatch.chdir(work)
    try:
        result = DiagramTool().execute(blocks=[{"id": "a", "label": "one"}],
                                       file="diagram.svg")
        assert victim.read_text(encoding="utf-8") == "print('real code')\n", \
            "the drawing went through the link"
        assert result.metadata["file"] == ""
        assert any("outside the working directory" in p
                   for p in result.metadata["problems"]), result.metadata
        assert not (outside / "diagram.svg").exists()
    finally:
        _drop(work / "diagram.svg")


def test_an_svg_under_a_linked_directory_counts_as_outside(tmp_path, monkeypatch):
    """A Windows junction `out/` is a plain relative name with no `..` in it, and
    it led the audited tool's SVG into a directory outside the working one."""
    outside = _neighbour(tmp_path, "junction")
    work = tmp_path / "repo"
    work.mkdir()
    if not _link_dir(work / "out", outside):
        pytest.skip("neither a junction nor a symlink directory is allowed here")
    monkeypatch.chdir(work)
    try:
        result = DiagramTool().execute(blocks=[{"id": "a", "label": "one"}],
                                       file="out/plot.svg")
        assert not (outside / "plot.svg").exists(), "the drawing landed outside the cwd"
        assert result.metadata["file"] == ""
        assert any("outside the working directory" in p
                   for p in result.metadata["problems"]), result.metadata
    finally:
        _drop(work / "out")


def test_the_target_rule_is_about_the_resolved_path(tmp_path):
    """Pinned without a link, so it holds on a machine that will not let us make
    one -- which is exactly the junction case the audit could not close by hand."""
    base = tmp_path.resolve()
    assert _inside(base, base / "a.svg") == "a.svg"
    assert _inside(base, base / "docs" / "a.svg") == "docs/a.svg"
    assert _inside(base, base) == "", "a directory is not a file to write"
    assert _inside(base, (base / ".." / "elsewhere.svg").resolve()) == ""
    assert _inside(base, Path(str(base) + "s" + os.sep + "a.svg")) == "", \
        "a sibling whose name merely starts like the cwd is not inside it"


def test_a_name_is_judged_by_what_the_filesystem_would_make_of_it():
    """The suffix check alone passed `readme.md:evil.svg`, because a stream name
    still ends in `.svg`: the audited tool filed 650 bytes of markup *inside*
    readme.md. A control character and the other characters Windows reserves are
    the same class of trick -- the name says one file, the filesystem makes
    another."""
    assert _odd_name("diagram.svg") == ""
    assert _odd_name("docs/arch.svg") == ""
    assert "control character" in _odd_name("a\x00b.svg")
    assert "control character" in _odd_name("a\x1fb.svg")
    if os.name == "nt":
        assert "alternate data stream" in _odd_name("readme.md:evil.svg")
        assert _odd_name("C:driveskip.svg")
        assert _odd_name("notes.txt:diagram.svg")
    else:
        assert _odd_name("readme.md:evil.svg") == "", "a POSIX name may hold a colon"


@pytest.mark.skipif(os.name != "nt", reason="only Windows files bytes inside a name")
@pytest.mark.parametrize("name", ["readme.md:evil.svg", "notes.txt:diagram.svg",
                                  "C:driveskip.svg", "C:\\Windows\\temp\\escape.svg",
                                  "\\\\server\\share\\a.svg"])
def test_windows_names_that_leave_the_working_directory_or_the_filesystem(
        tmp_path, monkeypatch, name):
    """`readme.md:evil.svg` ends in `.svg` and passed the audited suffix check,
    writing 650 bytes of markup as an alternate data stream *inside* readme.md.
    `C:escape.svg` is not absolute either: it follows that drive's own cwd."""
    monkeypatch.chdir(tmp_path)
    readme = tmp_path / "readme.md"
    readme.write_text("# project\n", encoding="utf-8")
    result = DiagramTool().execute(blocks=[{"id": "a", "label": "one"}], file=name)
    assert result.metadata["file"] == "", name
    assert any("outside the working directory" in p or "alternate data stream" in p
               or "another meaning" in p for p in result.metadata["problems"]), name
    assert readme.read_text(encoding="utf-8") == "# project\n"
    listed = subprocess.run(["dir", "/r", str(readme)], capture_output=True, shell=True)
    assert b":evil" not in listed.stdout and b":diagram" not in listed.stdout
    if ":" in name:
        # The control that makes the line above mean something: a stream made by
        # hand on the same file, which the same probe does see.
        control = f"{readme}:manual"
        with open(control, "w", encoding="utf-8") as handle:
            handle.write("x" * 650)
        try:
            shown = subprocess.run(["dir", "/r", str(readme)], capture_output=True,
                                   shell=True)
            assert b":manual" in shown.stdout, "the probe cannot see a stream at all"
        finally:
            os.remove(control)


@pytest.mark.parametrize("name", ["../escape.svg", "../docs/a.svg", "~/pool-key.svg",
                                  "/tmp/escape.svg", "\x01hidden.svg", "notes.txt",
                                  "diagram.svg."])
def test_only_a_plain_relative_svg_name_is_a_target(tmp_path, monkeypatch, name):
    """Everything the model may pass as `file` has to be one plain relative name
    ending in .svg: no traversal, no home directory, no absolute path, no control
    character, no suffix Windows reads differently than it looks."""
    monkeypatch.chdir(tmp_path)
    result = DiagramTool().execute(blocks=[{"id": "a", "label": "one"}], file=name)
    assert result.metadata["file"] == "", name
    assert not (tmp_path / "diagram.svg").exists(), "a refusal may not fall back"
    assert any("nothing was saved" in p for p in result.metadata["problems"]), name


def test_a_plain_relative_name_in_a_subfolder_still_saves(tmp_path, monkeypatch):
    """The rule is about escape, not about nesting: a docs/ folder is still the
    working directory, and the tool is only any use if it keeps drawing there."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    result = DiagramTool().execute(
        blocks=[{"id": "a", "label": "first step", "x": 0, "y": 0},
                {"id": "b", "label": "second step", "x": 0, "y": 6}],
        edges=[{"from": "a", "to": "b"}], file="docs/arch.svg")
    assert result.metadata["file"] == "docs/arch.svg", result.metadata
    assert (tmp_path / "docs" / "arch.svg").is_file()
    assert _root(tmp_path / "docs", "arch.svg") is not None
    assert "file: docs/arch.svg" in result.output


# --- no picture, no file -------------------------------------------------------

def test_a_refused_drawing_writes_no_file(tmp_path, monkeypatch):
    """The audit's second HIGH, and the design error: `read` takes any path with
    no grant, one free model was talked into passing ~/.beecode/pool-key.json to
    diagram as a label, and the canvas refused the job while diagram.py wrote the
    SVG anyway -- 18 KB of key text into architecture.svg, in the project, for the
    user to commit. No picture means no file, and one honest result."""
    monkeypatch.chdir(tmp_path)
    key_text = '{"key": "' + "SECRETRIDER" * 1500 + '"}\n'
    result = DiagramTool().execute(
        blocks=[{"id": "api", "label": key_text, "x": 0, "y": 0},
                {"id": "far", "label": "far", "x": 900, "y": 90}],
        file="architecture.svg")
    assert not (tmp_path / "architecture.svg").exists(), "a refused job still wrote"
    assert not (tmp_path / "diagram.svg").exists()
    assert result.metadata["file"] == ""
    assert "(nothing could be drawn)" in result.output
    assert any("no file was written" in p for p in result.metadata["problems"]), \
        result.metadata
    assert not list(tmp_path.rglob("*.svg")), "no SVG of any size survived the refusal"


def test_a_file_over_the_size_of_a_drawing_is_refused_whole(tmp_path, monkeypatch):
    """Too big is a mistake to report, not a reason to write half a file: the SVG
    on disk is either the picture the model read back or it is not there."""
    class Oversized(DiagramTool):
        def _svg(self, boxes, arrows, scale=9):
            return "<svg xmlns='http://www.w3.org/2000/svg'>" + \
                   "z" * MAX_SVG_BYTES + "</svg>"

    monkeypatch.chdir(tmp_path)
    result = Oversized().execute(blocks=[{"id": "a", "label": "one"}], file="huge.svg")
    assert not (tmp_path / "huge.svg").exists()
    assert result.metadata["file"] == ""
    assert any("not half of it" in p for p in result.metadata["problems"]), result.metadata


# --- a label is a caption, not a smuggling route -------------------------------

def test_a_label_is_bounded_before_it_sizes_a_box():
    """18 KB arrived as one label and made a box 195007 cells wide. Both budgets
    are applied while the box is built, so neither the canvas nor the file is ever
    sized by the length of a text the tool was handed."""
    problems = []
    boxes = DiagramTool()._boxes([{"id": "a", "label": "word " * 3000}], problems)
    assert len(boxes["a"]["label"]) <= MAX_LABEL_CHARS
    assert len(boxes["a"]["lines"]) <= MAX_LABEL_LINES
    assert boxes["a"]["h"] <= MAX_LABEL_LINES + 2
    assert any("was cut" in p for p in problems), problems


def test_an_id_gets_a_budget_too_because_it_is_drawn_into_the_file(
        tmp_path, monkeypatch):
    """`data-block="{id}"` is the other door: a box with an empty label takes its
    id as its caption, and the id itself goes into the markup whatever the label
    says. It is also quoted back in every refusal, so the result is bounded."""
    monkeypatch.chdir(tmp_path)
    secret = "Q" * 5000
    result = DiagramTool().execute(
        blocks=[{"id": secret, "label": "", "x": 0, "y": 0},
                {"id": "ok", "label": "kept", "x": 10, "y": 0}], file="ids.svg")
    assert any("an id gets" in p for p in result.metadata["problems"]), result.metadata
    svg = (tmp_path / "ids.svg").read_text(encoding="utf-8")
    assert "QQQQ" not in svg, "the over-long id reached the file"
    assert "kept" in svg, "the blocks that were well-formed are still drawn"
    assert secret[:40] not in result.output, "a refusal quotes a name, not a document"


def test_a_file_name_that_long_is_not_a_name(tmp_path, monkeypatch):
    """Refusals quote the name back, so the name has to be bounded before it is
    echoed, resolved or written."""
    monkeypatch.chdir(tmp_path)
    name = "Q" * 3000 + ".svg"
    result = DiagramTool().execute(blocks=[{"id": "a", "label": "one"}], file=name)
    assert result.metadata["file"] == ""
    assert len(result.output) < 1000, \
        f"the refusal handed the {len(name)}-character name straight back"
    assert not list(tmp_path.glob("Q*.svg"))


def test_a_long_label_cannot_ride_through_into_the_file(tmp_path, monkeypatch):
    """What the file may hold of a label is what a caption needs, and no more.

    Counting the marker pairs is the honest measurement: the audited version put
    all 18 KB of a key into the SVG, and a truncated one that still wrote the
    surplus would be the same hole with a new number on it.
    """
    monkeypatch.chdir(tmp_path)
    marker = "QZ"
    secret = marker * 9000
    result = DiagramTool().execute(
        blocks=[{"id": "api", "label": secret, "x": 0, "y": 0},
                {"id": "db", "label": "db", "x": 14, "y": 0}],
        edges=[{"from": "api", "to": "db"}], file="bounded.svg")
    svg = (tmp_path / "bounded.svg").read_text(encoding="utf-8")
    carried = svg.count(marker) * len(marker)
    assert carried <= MAX_LABEL_CHARS, f"{carried} characters of the label reached the file"
    assert len(svg) < 4096, f"the file is {len(svg)} bytes, not a dump"
    assert any("characters and was cut" in p for p in result.metadata["problems"]), \
        "truncating a label silently would be a lie about the drawing"
    assert _root(tmp_path, "bounded.svg") is not None
