"""A skin that leans into the bee theme: heavier frames, comb header, buzz spinner."""
import random

from rich import box
from rich.text import Text

BUZZ = [
    ("разминаю крылья…", "warming up the wings…"),
    ("несу нектар из токенов…", "carrying nectar made of tokens…"),
    ("жужжу на сервере…", "buzzing at the server…"),
    ("рою соты под задачу…", "digging cells for this task…"),
]


def _comb_header() -> None:
    from beeagent.ui.components import console

    comb = Text("⬡" * 14, style="bold #ffcc00")
    console.print(comb)
    console.print(Text("  🐝 BeeCode", style="bold #ffcc00"))
    console.print()


def _buzz() -> str:
    from beeagent.i18n import L

    return L(*random.choice(BUZZ))


def setup(api) -> None:
    api.skin("frame", "comb", {"box": box.HEAVY, "border_style": "bold #8a6d00"})
    api.skin("banner", "comb-header", _comb_header)
    api.skin("spinner", "buzz", _buzz)
    api.set_skin("frame", "comb")
    api.set_skin("banner", "comb-header")
    api.set_skin("spinner", "buzz")
