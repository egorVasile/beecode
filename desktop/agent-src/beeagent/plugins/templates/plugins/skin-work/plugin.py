"""Nothing but the work: thin frames, no opening animation, no waiting line.

Every slot is set to the plainest registered variant — this one adds no new
implementations, which is also a legitimate skin: choosing is a separate
decision from drawing.
"""


def setup(api) -> None:
    api.set_skin("frame", "minimal")
    api.set_skin("banner", "none")
    api.set_skin("spinner", "none")
