from beeagent.ui.components import _pixel_text, BANNER_ROWS, BANNER, PIXEL_FONT, print_banner


def test_pixel_text_has_five_rows():
    rows = _pixel_text("BEECODE")
    assert len(rows) == 5


def test_pixel_rows_are_uniform_width():
    rows = _pixel_text("BEECODE")
    widths = {len(r) for r in rows}
    assert len(widths) == 1
    # 7 glyphs * 5 cols + 6 separators
    assert widths.pop() == 7 * 5 + 6


def test_unknown_char_falls_back_to_blank():
    rows = _pixel_text("*")
    assert all(r.strip() == "" for r in rows)


def test_banner_says_beecode():
    assert BANNER_ROWS == _pixel_text("BEECODE", xscale=2)
    assert "█" in BANNER


def test_banner_is_stretched_wider():
    narrow = _pixel_text("BEECODE", xscale=1)
    assert len(BANNER_ROWS[0]) == len(narrow[0]) * 2 - 6  # doubled cells, single separators
    assert len(BANNER_ROWS[0]) > len(narrow[0])


def test_font_glyphs_are_5x5():
    for ch, rows in PIXEL_FONT.items():
        assert len(rows) == 5
        assert all(len(r) == 5 for r in rows)


def test_print_banner_does_not_crash():
    print_banner()
