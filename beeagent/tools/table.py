"""A table the model fills: headers plus rows, drawn, not announced.

`{"tool": "table", "args": {"headers": ["a", "b"], "rows": [["1", "2"]]}}`
runs silently — no chalk line, no "done" line — and the interface draws a
real table instead. The transcript keeps one compact line (headers plus the
row count), because the model already knows the cells and the context window
should not pay for them twice.

Dimensions come from the data itself: three headers and four rows are a 3x4
table. Rows shorter than the headers are padded, longer ones trimmed, and
past MAX_ROWS the table is cut with a note saying how many rows never made it
to the screen — or to the transcript.
"""
from .base import BaseTool, ToolResult

MAX_COLS = 12
MAX_ROWS = 20
CELL_CHARS = 40


class TableTool(BaseTool):
    name = "table"
    description = (
        "Draw a table you fill, silently: "
        '{"headers": ["a", "b"], "rows": [["1", "2"]]}.'
    )
    parameters = {
        "type": "object",
        "properties": {
            "headers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Column titles — their count is the table width",
            },
            "rows": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
                "description": "One list of cells per row",
            },
        },
        "required": ["headers", "rows"],
    }
    silent = True

    def execute(self, headers: list, rows: list) -> ToolResult:
        heads = [str(h) for h in (headers or [])][:MAX_COLS]
        if not heads:
            return ToolResult(output="ERROR: a table needs headers", error=True)
        width = len(heads)
        body: list[list[str]] = []
        for row in (rows or [])[:MAX_ROWS]:
            cells = [str(c) for c in (row or [])]
            cells = (cells + [""] * width)[:width]
            body.append(cells)
        dropped = max(0, len(rows or []) - MAX_ROWS)
        # The picture as text, like `diagram` does: the model reads back what
        # it drew, so a later turn never remakes a table it cannot see. Cells
        # are capped — a novel in a cell is not a table.
        widths = [1] * width
        for ci, head in enumerate(heads):
            widths[ci] = max(widths[ci], min(len(head), CELL_CHARS))
        for ri, row in enumerate(body):
            for ci, cell in enumerate(row):
                widths[ci] = max(widths[ci], min(len(cell), CELL_CHARS))
        bar = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
        picture = [bar]
        picture.append("| " + " | ".join(h.ljust(widths[ci])[:widths[ci]]
                                         for ci, h in enumerate(heads)) + " |")
        picture.append(bar)
        for row in body:
            picture.append("| " + " | ".join(
                c[:CELL_CHARS].ljust(widths[ci]) for ci, c in enumerate(row)) + " |")
        picture.append(bar)
        output = f"table {width}x{len(body)} shown:\n" + "\n".join(picture)
        if dropped:
            output += f"\n({dropped} more rows cut)"
        return ToolResult(
            output=output,
            error=False,
            metadata={"render": "table", "headers": heads, "rows": body,
                      "dropped": dropped},
        )

    def is_safe(self) -> bool:
        return True
