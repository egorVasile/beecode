from rich.text import Text

from beeagent.ui.bee import BEE_FRAMES, BEE_FRAME_COUNT, render_bee


def test_frames_uniform_dimensions():
    assert {len(f) for f in BEE_FRAMES} == {8}
    assert {len(row) for f in BEE_FRAMES for row in f} == {13}


def test_multiple_frames_for_animation():
    assert BEE_FRAME_COUNT == len(BEE_FRAMES) >= 2


def test_render_bee_returns_text():
    assert isinstance(render_bee(0), Text)


def test_render_bee_wraps_index():
    assert render_bee(0).plain == render_bee(BEE_FRAME_COUNT).plain


def test_paw_waves_between_frames():
    def paw_rows(frame):
        return [i for i, row in enumerate(frame) if "P" in row]
    # the waving paw sits on different rows in different frames
    assert paw_rows(BEE_FRAMES[0]) != paw_rows(BEE_FRAMES[2])


def test_wings_flap():
    # frame 0 has a two-row wing, frame 1 tucks it to one row
    assert BEE_FRAMES[0][0].strip() != ""
    assert BEE_FRAMES[1][0].strip() == ""
