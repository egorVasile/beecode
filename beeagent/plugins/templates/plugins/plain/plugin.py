"""Example extension: an interface variant, contributed without touching the UI code.

Install it and BeeCode starts frameless with a quiet waiting line; `/skin` still
lets the user put the frames back, because a plugin *offers* variants and the
user's own choice wins.
"""


def setup(api) -> None:
    # A frame of one space is what "no frame" means to rich: the layout stays,
    # the border characters do not.
    from rich import box

    api.skin("frame", "plain", {"box": box.Box("\n".join(["    "] * 8))})
    api.set_skin("frame", "plain")
    api.set_skin("banner", "static")
    api.set_skin("spinner", "dots")
