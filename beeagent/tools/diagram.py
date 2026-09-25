"""Block diagrams the model can see.

A model describing an architecture in prose cannot check what it described. This
tool takes boxes with labels and arrows between them, draws them, and returns the
drawing as text -- so the model reads back its own diagram, notices that two
boxes landed on each other or that an arrow has nowhere to go, and fixes it.

The picture is the point; the file is a courtesy. The keyless models BeeCode
usually reaches have no vision at all, so an image would be something the user
sees and the agent never does. ASCII in the tool result is the one form both can
read, and it needs no third-party library, which keeps this installable on
Termux.

The courtesy is why two rules exist here. The file name is resolved, not
scrutinised: `..` is easy to spot in a string, but a link or a junction sitting
under the working directory points anywhere the author of the repo liked, and
`readme.md:evil.svg` ends in `.svg` while filing its bytes inside another file's
alternate data stream. And nothing is written for a drawing that was refused --
the ASCII answer is the deliverable, and a file the picture did not earn is a
file nobody asked for.

Coordinates are character cells, and the model chooses them. Overlaps and arrows
that leave the page are reported rather than silently moved: a diagram the agent
did not choose to change is not its diagram. What is not reported is a bend -- an
arrow that has to step around someone else's block does so in the text and in the
SVG alike, off the same route, because a line drawn through a box is a lie in both.
"""
import os
from pathlib import Path

from .base import BaseTool, ToolResult, write_text_preserving

MIN_BOX_W = 5
MAX_BOXES = 40
MAX_EDGES = 80
CANVAS_LIMIT = 2000        # cells; a wider canvas than this is a mistake, not a drawing
DEFAULT_FILE = "diagram.svg"

# A label is the caption of a box, not a place to put a file. Live measurement
# 2026-09-24: `read` takes any path with no grant, and one free model was talked
# into handing ~/.beecode/pool-key.json to `diagram` as a label -- 18 KB of key
# that the canvas refused for size while the SVG went to disk anyway. So a label
# gets a character and a line budget here, and the file a whole-file budget
# below: enough for a real caption on 40 boxes, and nowhere near enough for a
# secret to ride through, whatever name it arrives under. The id is budgeted for
# the same reason: it is written into `data-block` and `data-from` attributes.
MAX_LABEL_CHARS = 200
MAX_LABEL_LINES = 6
MAX_ID_CHARS = 40
MAX_NAME_CHARS = 120
MAX_SVG_BYTES = 250_000    # 40 boxes and 80 arrows never reach this; past it the
                           # drawing is a dump, so it is refused, not written half


def _sign(value):
    return (value > 0) - (value < 0)


# (direction arriving, direction leaving) -> the corner that joins them. Written
# as escapes because these glyphs survive a copy-paste badly.
_CORNERS = {
    ((1, 0), (0, 1)): "┐",   ((1, 0), (0, -1)): "┘",   # arriving rightward
    ((-1, 0), (0, 1)): "┌",  ((-1, 0), (0, -1)): "└",  # arriving leftward
    ((0, 1), (1, 0)): "└",   ((0, 1), (-1, 0)): "┘",   # arriving downward
    ((0, -1), (1, 0)): "┌",  ((0, -1), (-1, 0)): "┐",  # arriving upward
}


def _wrap(label: str, width: int) -> list:
    """Split a label into lines no longer than `width`, words kept whole."""
    words = str(label).split()
    if not words:
        return [""]
    lines, current = [], ""
    for word in words:
        while len(word) > width:                    # a single long token
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


# Characters Windows gives a meaning beyond "a byte in a name". `:` is the one
# that matters here -- `readme.md:evil.svg` is an alternate data stream filed
# *inside* readme.md, and it ends in `.svg` so a suffix check never sees it.
_NT_SPECIAL = set('<>:"|?*')


def _odd_name(name: str) -> str:
    """Why this string is not a plain file name, or "" when it is.

    This is about the shape of the name, not about the working directory: what
    `readme.md:evil.svg` becomes is the filesystem's doing, and it still ends in
    `.svg`, so no suffix check can see it. Drive-relative and rooted names are the
    `_target` rule's, and on POSIX a colon is just a character in a name, which
    the resolution check there then keeps where it belongs.
    """
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in name):
        return "holds a control character"
    if os.name == "nt":
        if ":" in name:
            return ("holds a “:” — on Windows that is an alternate data stream filed "
                    "inside another file, not a new file")
        odd = sorted(set(name) & _NT_SPECIAL)
        if odd:
            return f"holds {odd[0]!r}, which Windows gives another meaning to"
    return ""


def _inside(base, target) -> str:
    """The path `target` has under `base`, or "" when it is not under it.

    Both sides arrive already resolved, so this compares the paths the filesystem
    will use, and `normcase` because Windows matches names case-insensitively.
    """
    real, here = str(target), str(base).rstrip(os.sep) or os.sep
    n_real, n_here = os.path.normcase(real), os.path.normcase(here)
    if not n_real.startswith(n_here + os.sep):
        return ""
    return real[len(here) + 1:].replace(os.sep, "/")


def _refused(name: str, why: str) -> str:
    """One sentence telling the model its name earned no file, and what to pass."""
    return (f"“{name}” {why} — nothing was saved; pass a plain name such as "
            f"{DEFAULT_FILE}")


class DiagramTool(BaseTool):
    name = "diagram"
    description = (
        "Draw boxes with arrows and return the picture as text, so you can see what you "
        "made and fix it. You place the boxes: give each an id, label and cell "
        "coordinates, and each arrow a from/to. Overlaps, unplaceable labels and "
        "unknown ids are reported. The same lines go into an SVG for the user — only "
        "when there is a picture to save, and only under a plain relative .svg name "
        "inside the working directory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "blocks": {
                "type": "array",
                "description": "Boxes: {id, label, x, y}. x,y are the top-left cell. "
                               f"A label is a caption: {MAX_LABEL_CHARS} characters, "
                               f"{MAX_LABEL_LINES} lines, the rest is not drawn.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                    },
                    "required": ["id", "label"],
                },
            },
            "edges": {
                "type": "array",
                "description": "Arrows: {from, to, label?}. Both must be block ids.",
                "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                        "label": {"type": "string"},
                    },
                    "required": ["from", "to"],
                },
            },
            "file": {"type": "string",
                     "description": "Name for the SVG, default diagram.svg. A plain "
                                    "relative name ending in .svg; absolute paths, .., "
                                    "~, a drive letter and a : are refused, and so is a "
                                    "name a link or junction under the working directory "
                                    "leads out of it."},
        },
        "required": ["blocks"],
    }

    # The drawing is the result; the file it writes beside it is a plain
    # derivative artifact, so the tool is safe to run without a grant — but it
    # does change the disk, which is what /permissions readonly has to refuse.
    writes_files = True

    def is_safe(self) -> bool:
        return True

    # --- input --------------------------------------------------------------

    def _target(self, requested, problems: list):
        """Where the SVG may land: the real path of a plain relative `.svg`.

        The model supplies this string, so it is checked here rather than trusted.
        A lexical check is not a check: it stops `../../src/app.py` and lets a
        symlinked `diagram.svg`, a junction `out/`, and `readme.md:evil.svg`
        through, all of which the audited version wrote -- outside the working
        directory, or inside another file. So the name is resolved to the path the
        filesystem will use, `Path.resolve` following every link and reparse point
        in it, and that is what has to sit under the resolved working directory.
        A `.svg` under a linked directory is therefore *not* inside it, which is
        the only reading of "inside the working directory" that means anything.

        Returns the resolved path and the name to show the model, or None.
        """
        name = str(requested or "").strip() or DEFAULT_FILE
        if len(name) > MAX_NAME_CHARS:
            # The refusals below quote the name back, and a name the length of a
            # file would make the refusal one, so it is answered by its first
            # characters and its real length.
            problems.append(_refused(name[:32] + "…", f"is {len(name)} characters, which "
                                                      f"is not a file name"))
            return None
        candidate = Path(name)
        # `drive or root` is the anchor: absolute, rooted (`\temp\a.svg`),
        # drive-relative (`C:escape.svg` follows that drive's own cwd), UNC --
        # spelled that way because `PurePath.anchor` is 3.12 and this installs on
        # Termux.
        if candidate.drive or candidate.root or ".." in candidate.parts:
            problems.append(_refused(name, "points outside the working directory"))
            return None
        if candidate.parts[:1] == ("~",):
            problems.append(_refused(name, "starts in the home directory, where every "
                                          "other tool here reads"))
            return None
        odd = _odd_name(name)
        if odd:
            problems.append(_refused(name, odd))
            return None
        if candidate.suffix.lower() != ".svg":
            problems.append(f"“{name}” is not an .svg — nothing was saved; this tool "
                            f"only writes drawings")
            return None
        try:
            base = Path.cwd().resolve()
            target = (base / candidate).resolve()
        except OSError as exc:
            problems.append(_refused(name, f"cannot be placed (the working directory is "
                                           f"{exc.strerror or exc})"))
            return None
        relative = _inside(base, target)
        if not relative:
            problems.append(_refused(name, "points outside the working directory — a link "
                                           "or junction under it leads elsewhere"))
            return None
        return target, relative

    def _boxes(self, blocks, problems):
        """Normalised boxes, sized by their own labels."""
        boxes = {}
        for raw in list(blocks or [])[:MAX_BOXES]:
            if not isinstance(raw, dict):
                problems.append(f"block {raw!r} is not an object")
                continue
            bid = str(raw.get("id") or "").strip()
            if not bid:
                problems.append("a block has no id")
                continue
            if len(bid) > MAX_ID_CHARS:
                # The id is drawn into the file as `data-block`, and every refusal
                # quotes it back, so it gets a budget of its own. Dropping the box
                # is the honest half: its arrows then report as naming an unknown
                # block, which is exactly what they do.
                problems.append(f"block id {bid[:24]!r}… is {len(bid)} characters, over the "
                                f"{MAX_ID_CHARS} an id gets — it is drawn into the file, "
                                f"not just read by it; that block was not drawn")
                continue
            if bid in boxes:
                problems.append(f"duplicate block id {bid!r} — the second one was ignored")
                continue
            label = " ".join(str(raw.get("label") or bid).split())
            if len(label) > MAX_LABEL_CHARS:
                problems.append(f"block {bid!r}: label is {len(label)} characters and was "
                                f"cut to {MAX_LABEL_CHARS} — a label names a box, it does "
                                f"not carry a file")
                label = label[:MAX_LABEL_CHARS].rstrip()
            words = max((len(w) for w in label.split()), default=4)
            width = min(28, max(MIN_BOX_W, min(len(label), words) + 2))
            lines = _wrap(label, width - 2)
            if len(lines) > MAX_LABEL_LINES:
                problems.append(f"block {bid!r}: label wraps to {len(lines)} lines and was "
                                f"cut to {MAX_LABEL_LINES} — fewer words, or a wider box")
                lines = lines[:MAX_LABEL_LINES]
            try:
                x, y = int(raw.get("x", 0)), int(raw.get("y", 0))
            except (TypeError, ValueError):
                problems.append(f"block {bid!r} has non-numeric x/y — placed at 0,0")
                x = y = 0
            boxes[bid] = {"id": bid, "label": label, "x": max(0, x), "y": max(0, y),
                          "w": width, "h": len(lines) + 2, "lines": lines}
        return boxes

    # --- geometry -----------------------------------------------------------

    @staticmethod
    def _rect(box):
        return (box["x"], box["y"], box["x"] + box["w"] - 1, box["y"] + box["h"] - 1)

    @classmethod
    def _overlap(cls, a, b):
        ax1, ay1, ax2, ay2 = cls._rect(a)
        bx1, by1, bx2, by2 = cls._rect(b)
        return not (ax2 < bx1 or bx2 < ax1 or ay2 < by1 or by2 < ay1)

    def _anchors(self, a, b):
        """Where the arrow leaves a and enters b, from whichever sides face each other."""
        acx, acy = a["x"] + a["w"] / 2, a["y"] + a["h"] / 2
        bcx, bcy = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
        dx, dy = bcx - acx, bcy - acy
        if abs(dx) >= abs(dy):
            if dx >= 0:
                return (a["x"] + a["w"], int(acy)), (b["x"] - 1, int(bcy)), "h"
            return (a["x"] - 1, int(acy)), (b["x"] + b["w"], int(bcy)), "h"
        if dy >= 0:
            return (int(acx), a["y"] + a["h"]), (int(bcx), b["y"] - 1), "v"
        return (int(acx), a["y"] - 1), (int(bcx), b["y"] + b["h"]), "v"

    # --- routing ------------------------------------------------------------

    # The ASCII canvas refuses past CANVAS_LIMIT cells, but the SVG is written
    # whatever the canvas decided, so the detour search gets its own, larger
    # ceiling. Past it an arrow keeps its plain L instead of the search eating the
    # turn; the picture is still the one the ASCII reports on.
    ROUTING_LIMIT = 20000

    @staticmethod
    def _extent(boxes):
        """Canvas in cells: every block's footprint plus a cell-wide margin."""
        return (max(b["x"] + b["w"] for b in boxes.values()) + 2,
                max(b["y"] + b["h"] for b in boxes.values()) + 2)

    def _ownership(self, boxes, width, height):
        """Which block owns each cell -- the only thing a route has to avoid."""
        owner = [[None] * width for _ in range(height)]
        for box in boxes.values():
            x1, y1, x2, y2 = self._rect(box)
            for cy in range(max(0, y1), min(height, y2 + 1)):
                for cx in range(max(0, x1), min(width, x2 + 1)):
                    owner[cy][cx] = box["id"]
        return owner

    @staticmethod
    def _free(owner, width, height, x, y):
        """A cell an arrow may run through: on the page, and nobody's block."""
        if owner is None:
            return True
        return 0 <= x < width and 0 <= y < height and owner[y][x] is None

    @staticmethod
    def _walk(points):
        """Every cell of an orthogonal polyline, a corner counted once.

        Empty for a segment that changes both x and y: a diagonal is not a route
        this tool can draw, and walking one would spin forever.
        """
        if not points:
            return []
        if len(points) == 1:
            return list(points)
        cells = []
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            if x1 != x2 and y1 != y2:
                return []
            step_x, step_y = _sign(x2 - x1), _sign(y2 - y1)
            x, y = x1, y1
            while (x, y) != (x2, y2):
                cells.append((x, y))
                x, y = x + step_x, y + step_y
        cells.append(points[-1])
        return cells

    @classmethod
    def _slim(cls, points, eps=0.01):
        """Drop repeated and straight-through waypoints: an L whose corner landed
        on the line is one segment, not two, in either rendering."""
        out = []
        for x, y in points:
            if out and abs(x - out[-1][0]) < eps and abs(y - out[-1][1]) < eps:
                continue
            out.append((x, y))
        if len(out) < 3:
            return out
        keep = [out[0]]
        for i in range(1, len(out) - 1):
            ax, ay = out[i - 1]
            px, py = out[i]
            bx, by = out[i + 1]
            if abs((px - ax) * (by - py) - (py - ay) * (bx - px)) > eps:
                keep.append(out[i])
        keep.append(out[-1])
        return keep

    @classmethod
    def _clear(cls, owner, width, height, points):
        cells = cls._walk(points)
        return bool(cells) and all(cls._free(owner, width, height, x, y)
                                   for x, y in cells)

    @classmethod
    def _visible(cls, points, owner, width, height):
        """Whether any cell of the route is actually drawn.

        The ASCII calls an arrow with no visible cell `invisible`; the SVG uses the
        same answer to leave the line out, so one drawing cannot promise a connect-
        ion the other says is missing.
        """
        return any(cls._free(owner, width, height, x, y) for x, y in cls._walk(points))

    def _detours(self, start, end, boxes, width, height):
        """Two-bend routes: step onto a lane beside a block, run past it, step back.

        Ordered by how far the line strays from its own L, so the arrow that bends
        is the one that had to and it bends as little as it can.
        """
        sx, sy = start
        ex, ey = end
        lanes = []
        for box in boxes.values():
            x1, y1, x2, y2 = self._rect(box)
            for column in (x1 - 1, x2 + 1):
                if 0 <= column < width:
                    lanes.append((abs(column - (sx + ex) / 2),
                                  self._polyline([start, (column, sy), (column, ey), end])))
            for row in (y1 - 1, y2 + 1):
                if 0 <= row < height:
                    lanes.append((abs(row - (sy + ey) / 2),
                                  self._polyline([start, (sx, row), (ex, row), end])))
        lanes.sort(key=lambda lane: lane[0])
        return [points for _, points in lanes]

    @classmethod
    def _polyline(cls, points):
        """Waypoints as whole cells, corners only."""
        return cls._slim([(int(x), int(y)) for x, y in points])

    def _route(self, a, b, boxes, owner, width, height):
        """The cells one arrow runs over, as waypoints. Both renderings ask here.

        The plain L the tool always drew is tried first, so a diagram that crossed
        nothing comes out exactly as it did; only an arrow that would have punched
        through someone else's block bends around it. When no route clears -- blocks
        with no gap left between them -- the L comes back untouched and the ASCII
        reports the arrow as invisible, which is the honest answer.
        """
        start, end, kind = self._anchors(a, b)
        first = (end[0], start[1]) if kind == "h" else (start[0], end[1])
        second = (start[0], end[1]) if kind == "h" else (end[0], start[1])
        candidates = [self._polyline([start, first, end]),
                      self._polyline([start, second, end])]
        if owner is not None:
            candidates.extend(self._detours(start, end, boxes, width, height))
        for points in candidates:
            if self._clear(owner, width, height, points):
                return points
        return candidates[0]

    @staticmethod
    def _label_spot(points):
        """The middle of the longest straight stretch of a route.

        The word belongs on the line that was drawn, not on the chord between the
        two anchors, and in the middle so the corners survive. The SVG uses the same
        rule in pixels, so an arrow's label sits on the same stretch in both.
        """
        best, longest = points[0], -1
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            length = abs(x2 - x1) + abs(y2 - y1)
            if length > longest:
                best, longest = ((x1 + x2) // 2, (y1 + y2) // 2), length
        return best

    # --- rendering ----------------------------------------------------------

    def _canvas(self, boxes, edges):
        if not boxes:
            return [], []
        problems = []
        width, height = self._extent(boxes)
        if width * height > CANVAS_LIMIT:
            problems.append(
                f"canvas would be {width}x{height}; bring the blocks closer "
                f"(limit {CANVAS_LIMIT} cells)")
            return [], problems

        grid = [[" "] * width for _ in range(height)]
        # The whole footprint belongs to the box, not just its frame: without this
        # an arrow crossing a box painted a line straight through the label, which
        # is the one thing that makes a diagram unreadable. Routes are planned
        # against the same map, so a line that can go around one does.
        owner = self._ownership(boxes, width, height)

        for box in boxes.values():
            x1, y1 = box["x"], box["y"]
            x2, y2 = self._rect(box)[2], self._rect(box)[3]
            for cx in range(x1, x2 + 1):
                grid[y1][cx] = "─"
                grid[y2][cx] = "─"
            for cy in range(y1, y2 + 1):
                grid[cy][x1] = "│"
                grid[cy][x2] = "│"
            grid[y1][x1], grid[y1][x2] = "┌", "┐"
            grid[y2][x1], grid[y2][x2] = "└", "┘"
            for offset, line in enumerate(box["lines"]):
                row = y1 + 1 + offset
                text = line[:x2 - x1 - 1]
                pad = (x2 - x1 - 1 - len(text)) // 2
                for i, ch in enumerate(text):
                    grid[row][x1 + 1 + pad + i] = ch

        for first, second in [(a, b) for a in boxes.values() for b in boxes.values()
                              if a["id"] < b["id"]]:
            if self._overlap(first, second):
                problems.append(f"{first['id']} and {second['id']} overlap — move one")

        for edge in edges:
            src, dst = edge.get("from"), edge.get("to")
            a, b = boxes.get(src), boxes.get(dst)
            if a is None or b is None:
                problems.append(f"arrow {src}->{dst} names an unknown block")
                continue
            if src == dst:
                problems.append(f"{src}->{src} is a loop on one block; not drawn")
                continue
            route = self._route(a, b, boxes, owner, width, height)
            if not self._paint(grid, owner, route):
                # Live run on 2026-09-24: kimi drew three boxes that touched, and
                # every cell of the arrow belonged to a box, so the picture showed
                # two diagrams where the model intended three. A line the model
                # cannot see has to be named, not quietly skipped.
                problems.append(f"the {src}->{dst} arrow is invisible — {src} and {dst} "
                                f"touch or overlap; leave at least 3 free cells between them")
            label = str(edge.get("label") or "").strip()
            if label:
                mid, row = self._label_spot(route)
                if not self._label(grid, owner, mid, row, label):
                    # "move a block" is advice the model cannot size. Saying how
                    # much room the arrow has lets it decide between widening the
                    # gap and dropping the word — and the words are the point of
                    # an arrow.
                    room = self._room(grid, owner, mid, row)
                    wanted = len(label[:14])
                    problems.append(
                        f"label “{label}” needs {wanted} free cells but the {src}->{dst} "
                        f"arrow has {room} — pull {dst} about {max(1, wanted - room)} cells "
                        f"away or drop the label")
        return ["".join(row).rstrip() for row in grid], problems

    def _paint(self, grid, owner, points):
        """An orthogonal line: out along the leaving axis, across, then in.

        The waypoints come from `_route`, the same one the SVG strokes, so a bend
        around a block is in the text the model reads back and not only in the
        file it never sees. False when every cell of the line belonged to a block,
        i.e. nothing was drawn.
        """
        cells = self._walk(points)
        if not cells:
            return False
        drawn = False
        for i, (x, y) in enumerate(cells[:-1]):
            nx, ny = cells[i + 1]
            if i == 0:
                drawn |= self._put(grid, owner, x, y, "─" if ny == y else "│")
                continue
            px, py = cells[i - 1]
            going_in = (_sign(x - px), _sign(y - py))
            going_out = (_sign(nx - x), _sign(ny - y))
            if going_in == going_out:
                ch = "─" if going_in[1] == 0 else "│"
            else:
                ch = _CORNERS.get((going_in, going_out), "●")
            drawn |= self._put(grid, owner, x, y, ch)
        # arrowhead pointing the way the line arrived
        ex, ey = cells[-1]
        px, py = cells[-2] if len(cells) > 1 else (ex, ey)
        head = {(1, 0): "→", (-1, 0): "←", (0, 1): "↓", (0, -1): "↑"}.get(
            (_sign(ex - px), _sign(ey - py)), "→")
        drawn |= self._put(grid, owner, ex, ey, head)
        return drawn

    @staticmethod
    def _put(grid, owner, x, y, ch):
        """True when the cell was written; a block's own cell is never overpainted."""
        if not (0 <= y < len(grid) and 0 <= x < len(grid[0])):
            return False
        if owner[y][x] is not None:          # never scribble over a box
            return False
        grid[y][x] = ch
        return True

    @staticmethod
    def _room(grid, owner, x, y) -> int:
        """How many cells a label can have on that row, starting at x."""
        if not (0 <= y < len(grid) and 0 <= x < len(grid[0])) or owner[y][x]:
            return 0
        room = 0
        while x + room < len(grid[0]) and owner[y][x + room] is None:
            room += 1
        return room

    def _label(self, grid, owner, x, y, text):
        """Write a label only if all of it lands on free cells.

        Half a word is worse than no word: the first version drew "sea" where the
        model wrote "seat", and that reads as a correct result.
        """
        text = text[:14]
        cells = [(x + i, y) for i in range(len(text))]
        for cx, cy in cells:
            if not (0 <= cy < len(grid) and 0 <= cx < len(grid[0])) or owner[cy][cx]:
                return False
        for (cx, cy), ch in zip(cells, text):
            grid[cy][cx] = ch
        return True

    # --- svg ----------------------------------------------------------------

    # The brand, not the browser default: green ink on paper, blocks filled with a
    # wash of the same green. The user judges this file; the model never sees it.
    INK = "#2f7d32"
    BLOCK_FILL = "#f3f7ea"
    PAPER = "#fffdf5"
    TEXT = "#1c1c1c"
    MONO = 'font-family="monospace"'

    def _svg(self, boxes, edges, scale=9):
        """The same drawing in vector form, for the user.

        One route per arrow, taken from `_route` -- the line the ASCII paints -- so
        the file cannot show a connection the agent did not just read back. Each
        arrow leaves and enters on a block's own edge, gets its own arrowhead, and
        carries its label once, on the stretch that was drawn. Arrows go down
        before blocks so a route that had nowhere to go passes behind a label
        rather than through it, and every string leaves through `_esc`.
        """
        if not boxes:
            return ""
        pad = 20
        cells_w, cells_h = self._extent(boxes)
        width = max(b["x"] + b["w"] for b in boxes.values()) * scale + 2 * pad
        height = max(b["y"] + b["h"] for b in boxes.values()) * scale + 2 * pad
        owner = (self._ownership(boxes, cells_w, cells_h)
                 if cells_w * cells_h <= self.ROUTING_LIMIT else None)
        rects = {bid: (b["x"] * scale + pad, b["y"] * scale + pad,
                       b["w"] * scale, b["h"] * scale) for bid, b in boxes.items()}

        heads, arrows, labels = [], [], []
        for index, edge in enumerate(list(edges or [])[:MAX_EDGES], start=1):
            a, b = boxes.get(edge.get("from")), boxes.get(edge.get("to"))
            if a is None or b is None or a is b:
                continue            # an unknown id or a loop is the ASCII's to report
            route = self._route(a, b, boxes, owner, cells_w, cells_h)
            if not self._visible(route, owner, cells_w, cells_h):
                continue            # invisible in the picture, so absent from the file
            points = self._stroke(route, a, b, scale, pad)
            heads.append(f'<marker id="head-{index}" markerUnits="userSpaceOnUse" '
                         f'markerWidth="11" markerHeight="9" refX="10" refY="4.5" '
                         f'orient="auto">'
                         f'<path d="M0,0 L10,4.5 L0,9 z" fill="{self.INK}"/></marker>')
            arrows.append(f'<polyline points="{" ".join(points)}" fill="none" '
                          f'stroke="{self.INK}" stroke-width="2" stroke-linejoin="round" '
                          f'stroke-linecap="round" marker-end="url(#head-{index})" '
                          f'data-from="{_esc(a["id"])}" data-to="{_esc(b["id"])}"/>')
            words = " ".join(str(edge.get("label") or "").split())[:14]
            if words:
                labels.append(self._arrow_label(points, words, scale))

        blocks = []
        for box in boxes.values():
            rx, ry, rw, rh = rects[box["id"]]
            lines, row, size = self._text_layout(box, rw, rh)
            group = [f'<g data-block="{_esc(box["id"])}">',
                     f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{rw:.1f}" height="{rh:.1f}" '
                     f'rx="6" fill="{self.BLOCK_FILL}" stroke="{self.INK}" stroke-width="2"/>']
            for offset, line in enumerate(lines):
                group.append(f'<text x="{rx + rw / 2:.1f}" '
                             f'y="{ry + (offset + 0.5) * row + size * 0.35:.1f}" '
                             f'{self.MONO} font-size="{size:.1f}" text-anchor="middle" '
                             f'fill="{self.TEXT}">{_esc(line)}</text>')
            group.append("</g>")
            blocks.append("".join(group))

        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
                 f'height="{height}" viewBox="0 0 {width} {height}">',
                 f'<defs>{"".join(heads)}</defs>',
                 f'<rect width="{width}" height="{height}" fill="{self.PAPER}"/>']
        parts += arrows + blocks + labels + ["</svg>"]
        return "\n".join(parts)

    def _stroke(self, route, a, b, scale, pad):
        """Cell waypoints to `points="x,y"`.

        Every waypoint becomes the centre of its own cell, and then the line is
        extended half a cell at each end to the border of the block it attaches to
        -- instead of running under the label to the centre, which is what made the
        old file cross blocks. The extra stub is collinear with the first stretch,
        so `_slim` folds it into it and only a real corner survives.
        """
        def centre(cell):
            return (round(cell[0] * scale + pad + scale / 2, 2),
                    round(cell[1] * scale + pad + scale / 2, 2))

        pixels = [self._edge_point(route[0], a, scale, pad)]
        pixels += [centre(cell) for cell in route]
        pixels.append(self._edge_point(route[-1], b, scale, pad))
        slim = self._slim([(round(x, 2), round(y, 2)) for x, y in pixels])
        return [f"{x:.1f},{y:.1f}" for x, y in slim]

    @staticmethod
    def _edge_point(cell, box, scale, pad):
        """The pixel where a route crosses the border of the block it attaches to.

        `cell` is the free cell beside that border, which is where `_anchors` puts
        the end of every arrow, so the side is not a guess.
        """
        x, y = cell
        left = box["x"] * scale + pad
        top = box["y"] * scale + pad
        right = left + box["w"] * scale
        bottom = top + box["h"] * scale
        middle_x = x * scale + pad + scale / 2
        middle_y = y * scale + pad + scale / 2
        if x == box["x"] + box["w"]:
            return right, middle_y
        if x == box["x"] - 1:
            return left, middle_y
        if y == box["y"] + box["h"]:
            return middle_x, bottom
        if y == box["y"] - 1:
            return middle_x, top
        return middle_x, middle_y

    @staticmethod
    def _text_layout(box, rw, rh):
        """Label lines, one strip each, and a font size that fits both ways.

        Fixed 18/13-pixel offsets put the third line of a label outside a box that
        was only three cells tall: the text overflowed the frame it described. A
        strip per line, centred in it, keeps any block's words inside its border.
        """
        lines = box["lines"] or [""]
        longest = max(len(line) for line in lines) or 1
        row = rh / len(lines)
        # 0.62 em is the widest a monospace glyph gets, so the width term holds for
        # any `scale`, not only the default one.
        size = min(12.0, row * 0.72, (rw - 6) / (0.62 * longest))
        return lines, row, max(size, 4.0)

    def _arrow_label(self, points, words, scale):
        """One word per arrow, on the longest stretch of its line, on a chip of
        paper so a crossing stroke cannot eat half of it."""
        nums = [tuple(float(v) for v in point.split(",")) for point in points]
        if len(nums) < 2:
            nums = [nums[0], nums[0]]
        best, longest = (nums[0], nums[1]), -1.0
        for p, q in zip(nums, nums[1:]):
            run = abs(q[0] - p[0]) + abs(q[1] - p[1])
            if run > longest:
                best, longest = (p, q), run
        x = (best[0][0] + best[1][0]) / 2
        y = (best[0][1] + best[1][1]) / 2
        size = min(11.0, scale * 1.3)
        wide, high = len(words) * size * 0.62 + 6, size + 6
        return (f'<rect x="{x - wide / 2:.1f}" y="{y - high / 2:.1f}" width="{wide:.1f}" '
                f'height="{high:.1f}" rx="3" fill="{self.PAPER}" stroke="{self.INK}" '
                f'stroke-width="0.8"/>'
                f'<text x="{x:.1f}" y="{y + size * 0.35:.1f}" {self.MONO} '
                f'font-size="{size:.1f}" text-anchor="middle" fill="{self.TEXT}">'
                f'{_esc(words)}</text>')

    # --- entry point --------------------------------------------------------

    def _save(self, target, display, boxes, arrows, picture, problems) -> str:
        """Write the file the picture earned, or say why it has none.

        Two rules, both from the same audit. *No picture, no file:* the canvas
        refuses a drawing it cannot fit, and the audited version wrote the SVG
        anyway, so a refused job still left 18 KB of someone's key text sitting in
        the project. *No partial file:* a drawing over `MAX_SVG_BYTES` is not
        something to write half of, it is a mistake to report, and a file the model
        was told nothing about is the worst outcome of the three.

        The bytes go through `write_text_preserving`, which encodes before it
        creates anything and renames over the target instead of truncating it --
        so a name that was made into a link after `_target` resolved it is refused
        rather than followed. The answer is the name to show the model, or "" when
        nothing was saved.
        """
        if not picture:
            problems.append("nothing could be drawn, so no file was written — fix the "
                            "layout and call diagram again")
            return ""
        svg = self._svg(boxes, arrows)
        size = len(svg.encode("utf-8", "replace"))
        if size > MAX_SVG_BYTES:
            problems.append(f"the SVG would be {size} bytes, over the {MAX_SVG_BYTES} this "
                            f"tool writes — nothing was saved, not half of it; draw fewer "
                            f"blocks or shorten the labels")
            return ""
        try:
            # The same arrow list, in the same order, that the ASCII was painted
            # from: one file cannot hold a connection the other does not show.
            write_text_preserving(target, svg)
        except OSError as exc:
            problems.append(f"the SVG was not written ({exc.strerror or exc})")
            return ""
        return display

    def execute(self, blocks=None, edges=None, file="") -> ToolResult:
        problems = []
        boxes = self._boxes(blocks, problems)
        if not boxes:
            return ToolResult(output="diagram needs at least one block: "
                                     "blocks=[{id, label, x, y}]", error=True)
        if len(blocks or []) > MAX_BOXES:
            problems.append(f"only the first {MAX_BOXES} blocks were drawn")
        arrows = list(edges or [])[:MAX_EDGES]
        drawn, canvas_problems = self._canvas(boxes, arrows)
        problems.extend(canvas_problems)

        picture = "\n".join(row for row in drawn if row.strip())
        saved = ""
        placed = self._target(file, problems)
        if placed is not None:
            target, display = placed
            saved = self._save(target, display, boxes, arrows, picture, problems)

        report = [f"your diagram, {len(boxes)} blocks and "
                  f"{min(len(edges or []), MAX_EDGES)} arrows:"]
        report.append(picture or "(nothing could be drawn)")
        if saved:
            report.append(f"file: {saved}")
        if problems:
            report.append("fix these and call diagram again:")
            report.extend(f"  - {item}" for item in problems)
        else:
            report.append("no overlaps, every arrow lands on a block.")
        return ToolResult(output="\n".join(report), error=False,
                          metadata={"blocks": len(boxes), "problems": problems,
                                    "file": saved})


_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;"}


def _esc(text: str) -> str:
    """Make a model's string safe as XML text and as an attribute value.

    re.sub hands the callable a Match, so dict.get() returned None and every `<`
    and `&` was deleted from the file instead of escaped. Quotation marks go the
    same way now that ids are written into `data-*` attributes.

    XML 1.0 has no encoding for a control character, and a lone surrogate cannot
    be encoded as UTF-8 at all, so `write_text` used to raise on both and take the
    whole drawing down with it. They land as U+FFFD: the file stays valid, and the
    odd character is still visible as the odd character it is.
    """
    out = []
    for ch in str(text):
        code = ord(ch)
        if code < 0x20 or 0x7f <= code <= 0x9f or 0xd800 <= code <= 0xdfff:
            out.append("\ufffd")
        else:
            out.append(_ESCAPES.get(ch, ch))
    return "".join(out)
