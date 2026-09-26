# Writing a BeeCode skin

A skin used to be a colour dict: pick a frame variant, pick a spinner, done. It can
now be a Python module with access to the render cycle — one that animates a frame,
reacts to a token arriving, to a tool call starting, to an error. This is the guide
to writing one: from an empty folder to a skin that moves.

Everything below was run against the host in `beeagent/core/skins.py` and the
painter in `beeagent/core/renderer.py`. Where the two disagree, the code on disk
wins and this page says so.

## 1. Read this first: a skin is Python inside our process

**A skin is not sandboxed.** It is Python, imported into BeeCode's interpreter,
running with BeeCode's file, network and process permissions. `plugins/skin-x/plugin.py`
can read `~/.ssh/id_rsa` and post it anywhere, if it wants to. Nothing in this
repository stops it.

There is an AST gate — `beeagent.core.skins.check_source()` — and it is important
to be precise about what it is: **a pre-flight refusal, not a container.** It
parses your source and refuses the file *before a byte of it runs* when it imports
something off the allow-list, or reaches for `open`, `exec`, `eval`, `__import__`,
`compile`, `globals`, `locals`, `breakpoint`, `input`, or any name starting with
`_`. It does not evaluate anything, so it cannot see what a computed string
becomes: `getattr` on a name assembled from characters, `().__class__.__bases__[0]`,
a lambda walking to its own `__globals__` — all of them are outside its reach, and
all of them are ways back to the machine. A skin that wanted to escape would have
to go out of its way, and would look like it was doing so. That is the whole value:
it catches honest mistakes, it makes hostile code conspicuous, and it prints the
offending line and token where you can read them.

So the real check is not the gate. It is **the human who read the file and typed
`/trust yes`.** BeeCode's permission model for other people's code is
`beeagent/core/trust.py`: every folder is asked about once, the answer is stored in
`~/.beecode/trusted.json` keyed by the folder, an install pins the *hash* of the
bytes it wrote, and editing a `plugin.py` afterwards changes the hash — which makes
the question come back, because the code you approved is not the code that is about
to run. A folder that merely *contains* `.beeagent/plugins/thing/plugin.py` has not
installed it, and nothing runs from it until you say so.

**Installing a skin from a stranger means trusting it.** Read the `plugin.py`. It
is usually under a hundred lines, and there is no build step and no binary inside
the pack, so what you read is what runs. If you do not want to read it, do not
install it — a colour scheme is not worth a shell.

## 2. What a pack is

### The layout

```
.beeagent/plugins/<folder>/          # installed: a user's copy
beeagent/plugins/templates/plugins/<folder>/   # shipped with BeeCode
├── plugin.py                        # the skin: hooks at module level, setup(api)
└── plugin.json                      # the manifest
```

`plugin.py` is the only file the loader looks for, and it looks for that name.
`setup(api)` is called once, right after the module is imported. Nothing registers
itself: the loader imports your file and calls `setup()`, and unless `setup()` calls
`register()`, your hooks are attributes nobody will ever find.

### The manifest

```json name=plugin.json
{
  "name": "thinking-pulse",
  "version": "1.0.0",
  "type": "plugin",
  "description": "A thinking pulse: one ASCII cell of light on the bottom row, lit by the token events",
  "entry": "plugin.py"
}
```

| field | what it means | who reads it today |
| --- | --- | --- |
| `name` | the pack's name; keep it equal to the folder name | nobody (see below) |
| `version` | your own version string | nobody |
| `type` | `"plugin"` — the only value a skin pack uses | the catalog, for filtering |
| `description` | one sentence, shown in `/plugins` and `/plugin list` | the catalog entry, not this file |
| `entry` | `"plugin.py"` | nobody — the loader hardcodes that name |

Say that plainly: **no code in BeeCode reads `plugin.json` yet.** The loader finds
`plugin_dir / "plugin.py"`, the folder name comes from disk, and the install ledger
is `.beeagent/plugins.json` (a different file — the state of what this folder has
installed). Write the manifest anyway: it is the contract of the format, it is what
a catalog entry is built from, and a pack without it cannot be added to the shipped
catalog. Keep `entry` honest: a pack that names `skin.py` will silently do nothing.

Two more name collisions worth learning early, because both bit someone:

* the **folder** name and the **skin** name are different things. `/plugin install
  https://github.com/you/beecode-thinking-pulse` names the folder after the last
  path segment (`beecode-thinking-pulse`); `/skins` switches by the name you pass to
  `register()`. Make them the same, or you will type a command that does not exist.
* `beeagent/plugins/manager.py` and `beeagent/ui/commands.py` both have a `plugins`
  name for something else. The path to the loader is
  `from beeagent.plugins.loader import PluginLoader`.

### Getting it loaded

BeeCode loads a pack only when it is *installed*, and installing means an entry in
`.beeagent/plugins.json` plus a folder on disk. Three real routes:

| route | command | notes |
| --- | --- | --- |
| shipped template | `/plugin install skin-pulse` | copies `templates/plugins/skin-pulse`, pins its hash |
| someone's repository | `/plugin install https://github.com/you/my-skin --trust` | `git clone --depth 1`; refuses without `--trust`, because it is your Python |
| your own folder, in development | write `.beeagent/plugins/<name>/` **and** the entry in `.beeagent/plugins.json`, then start BeeCode in that folder and answer `/trust yes` | the state entry is `{ "<name>": { "type": "plugin", "enabled": true } }` |

There is no `/plugin install ./local-folder` yet. In-development packs are the
route above; the file to copy is in section 5.

A git or market install of a *pack* is external code by any standard, so
`/plugin install <url>` stops and tells you to read it first:

```
"…" installs external code that runs inside BeeCode as tools.
Read it first, then re-run: /plugin install … --trust
```

### Wearing one

`/skins` is a list you pick from — mouse in the full-screen interface, arrow keys in
the classic one — and its rows are numbered, so all three of these wear the same
skin:

```
/skins                    → the list; click it, or type the number
/skins 3                  → the third row of that list
/skins shimmer            → by the name the skin calls itself
/skins skin-shimmer       → by the name you installed it under. Both work: the shelf
                            and the skin use different names, and the mismatch is
                            where "I downloaded a skin and nothing happened" came from
/skins next               → walk the skins that can actually be worn
/skins off                → BeeCode's own interface back
/skins list               → the same board as text, with why a skin is refused
```

Row 1 of every list is BeeCode's own interface, because taking a skin off should be
the easiest thing to find. The choice goes into `beeagent.json` as `skin` and is put
back on at the next start, after the packs have registered — installing a skin pack
puts it on right away when nothing else was chosen. A skin that draws nothing in the
interface you are in says so in the same row, and a refused one shows the line and
the token the gate objected to.

One distinction the install line has to keep: a pack BeeCode ships is our own bytes,
so it registers and is worn in the same breath. A pack from a git or market source is
stranger code, and until this folder is trusted its `plugin.py` never runs — which
the answer says plainly, with `/trust yes` as the command that changes it, rather
than reporting a skin that is on when nothing was loaded.

One licensing fact, because it surprises people: BeeCode is GPL-3.0, its own
templates are GPL-3.0-or-later, and the market's client-side licence gate
(`beeagent/plugins/catalog.py`, `OPEN_LICENSES`) does **not** list GPL. A GPL skin
can be shipped as a template or installed by git URL; it cannot pass the market
gate. MIT/Apache-2.0/BSD/Zlib/CC0 can.

### Checking yourself against the gate

The gate runs on source text. Run it on your own file before you publish it, so a
refusal is something you find at your desk and not something a user finds in their
interface:

```bash
python -c "from beeagent.core.skins import check_source, refusal_text; \
s=open('.beeagent/plugins/my-skin/plugin.py', encoding='utf-8').read(); \
r=check_source(s); print(refusal_text('my-skin', r) if r else 'gate: clean')"
```

What it refuses, and why:

| refused | the reason |
| --- | --- |
| `import` of anything outside `beeagent.core.skins`, `beeagent.core.renderer`, `math`, `random`, `time`, `json`, `dataclasses`, `typing`, `string`, `itertools`, `collections` | an import is a road to the machine |
| a relative import (`from . import x`) | it leaves the allow-list by construction |
| `open`, `exec`, `eval`, `compile`, `__import__`, `globals`, `locals`, `breakpoint`, `input` | the file, the interpreter and this module's namespace |
| any name or attribute starting with `_` | `__globals__`, `__class__`, `_getframe`: the escape routes that are not builtin names |

That last line has a practical consequence for your code style: **do not name a
field `self._busy`.** The gate reads an attribute, not an intention, and
`self._busy = True` is refused exactly like `f.__globals__` is. Private-by-convention
naming is not available to a skin; use `self.busy`, or `self.internal_busy`.

The gate is *not* run on a `plugin.py` the loader imports (the trust gate already
stood in front of that file, and running the AST gate there would be a second,
weaker answer to the same question). It *is* run on every skin registered from
text, which is the path `install_source()` and a paste-a-string session take. Write
your pack so it passes both, and check it with the command above.

## 3. The hooks

All ten are optional. `beeagent.core.skins` looks them up by name on whatever you
registered — a module, an instance, a dict of callables.

| hook | called | receives | what it is for |
| --- | --- | --- | --- |
| `on_init(ctx)` | once, from `switch()` | a `skins.Context`: `ctx.name()`, `ctx.size()`, `ctx.colors()`, `ctx.post()`, `ctx.stats()`, `ctx.language()`, `ctx.translate(en, ru)`, `ctx.owns()`, `ctx.claims()`, `ctx.hud_rows()` | read the terminal, allocate state, reset counters |
| `on_surfaces(ctx)` | once, from `switch()` | the same context | the list of lines you want to own — see "Surfaces" below |
| `on_frame(dt, painter)` | once per frame, from the render loop | `dt`: seconds since the previous frame; `painter`: the surface below | drawing, and only drawing |
| `on_event(event, payload)` | every agent event | the event name, a `dict` payload | updating state. **Never draw here** |
| `on_output(text)` | every block of answer text the UI is about to print | the text, as a `str` | reacting to the answer. A copy: rewriting it changes nothing the user sees |
| `on_status(default)` | whenever the state line is written | the host's own wording | the line under the prompt. Return markup |
| `on_spinner(default, clock)` | whenever the waiting line is written | the host's phrase, and the monotonic clock | the "working" line before an answer starts. Return markup, or `None` |
| `on_thinking(line)` | each line of the reasoning block on screen | that line | the reasoning tail. Return **plain text** |
| `on_stream(piece, done)` | each arriving piece of the answer | the piece, and `True` on the last call | the answer text on its way to the log. Return **plain text** |
| `on_answer(text, final)` | when the answer block is drawn — once at the end, and about three times a second while it arrives in the TUI | the whole answer text, and whether this is the finished block | the answer's outline, its lists and its headings. Return a **Rich renderable** or markup text |
| `on_hud(painter, dt)` | once per frame, while you hold the `hud` surface | a painter sized to your reserved rows | the strip above the state line |
| `on_banner(seconds, width, rows, default)` | when the opening logo is printed, and on every frame the TUI shows | the monotonic clock, the size of the logo box, and the shipped logo | the logo: a colour cycle across the letters, or your own art. Return markup or a renderable |
| `on_frame_color(role, seconds)` | once per panel drawn, and once per frame for the live answer panel in the TUI | which panel is asking (`"answer"`, `"tool"`, `"output"`, `"error"`, `"prompt"`, `"picker"`, `"thinking"`) and the clock | the **colour** of that panel's border, as a style string: `"bold #7cb342"`. The box stays the `frame` slot's |

A hook may declare fewer parameters than the contract offers and still be called:
`on_frame(self, dt)` gets `dt` only, `on_event(self)` gets nothing. The host counts
the parameters you *declare* — defaults included — and hands you that many
(`_accepts()` in `core/skins.py`), so `on_stream(piece, done=False)` really does
see the flag. Declaring `*args` means "give me everything".

**If a hook is missing, nothing breaks and nothing is announced.** `frame()` returns
`0.0`, `post()` returns `False`, `emit_output()` returns `False`, and `/skins` lists
the skin with `no hooks`. That is a real failure mode: a skin whose `on_frame` is
named `onFrame` is registered, marked active, and draws nothing, forever. Check with
`/skins` — the line for your skin names the hooks it found, and the lines it owns.

### Surfaces: which lines are yours

A skin that only paints a grid can be ignored: the interface carries on around it.
Eight pieces of that interface are *yours* if you ask for them, and asking is the
whole contract:

| surface | hook | what you may change | what happens when you get it wrong |
| --- | --- | --- | --- |
| `status` | `on_status(default)` | the state line's words, order and colour | you lose that line, and it is said out loud |
| `spinner` | `on_spinner(default, clock)` | the waiting line, and how it moves | same |
| `thinking` | `on_thinking(line)` | how a reasoning line is introduced | same |
| `stream` | `on_stream(piece, done)` | the answer text as it arrives | same |
| `answer` | `on_answer(text, final)` | the whole answer block: its outline, its lists, its headings | same |
| `hud` | `on_hud(painter, dt)` | `HUD_ROWS` rows above the state line, drawn through the painter | same |
| `banner` | `on_banner(seconds, width, rows, default)` | the opening logo, and the word in the corner of the full-screen interface | same |
| `frame` | `on_frame_color(role, seconds)` | the colour of a panel's border, per role | same |

`frame` answers with a colour and nothing else, on purpose. The shape of the box is
the `frame` slot's decision (`/skin frame none`), and a skin that could draw the box
could undo a setting the user chose with their hands. Roles let several panels on one
screen sit at different points of one cycle; a role you do not answer for keeps the
colour BeeCode would have used.

In the full-screen interface `banner` is shown twice: the whole logo goes into the
log at startup, and its first line recolours the word in the header on every frame —
that window has no 48×5 box to spend, and a logo nobody can see is not an animation.

`answer` is the only surface that answers with a **block** instead of a line, and
the only one that may return a Rich renderable — a `Panel`, a `Group`, a
`Text`, somebody's `Markdown` subclass. It is also the only one the host asks about
on a rhythm rather than per token: the TUI lays the arriving answer out about three
times a second (`ANSWER_BEATS`), because a block re-typeset twelve times a second
is a fifth of a core spent on colour nobody can follow while reading. `final` tells
you which of the two you are drawing, so the expensive path can be the one that
happens once per answer.

**You do not get a surface for writing a hook. You get it for claiming it** — with
`SURFACES = ("status", "hud")` in the module, or with `on_surfaces(ctx)` when the
answer depends on the screen you were handed. The host resolves the claim when
`switch()` runs and refuses three things: a name that is not a surface, a surface
whose hook you never wrote, and a claim that raises. `/skins` prints which lines
each skin holds, so a claim you forgot to make is visible instead of mysterious.

The rules on a returned value, in one place:

* `None` means *keep yours* — the host's wording stands, unedited.
* `""` means *show nothing here*. That is a thing a skin may legitimately want, and
  the host does not second-guess it.
* Anything that is not text loses the surface, with the reason named.
* `status` and `spinner` are printed as **Rich markup** by both interfaces, so
  `[bold #7cb342]…[/]` is colour and not letters. `thinking` and `stream` are
  **plain text**: they are the model's bytes, and the model writes `[bold]` inside
  code. `beeagent.core.renderer.markup()` escapes a body for you.
* `answer` is asked about the model's text too, and its answer is drawn, not
  printed: return a Rich renderable (a pack may import `rich`), or markup as a
  string (what a skin installed from **source** can do — the gate there will not
  let it import Rich). Returning `None` keeps BeeCode's own answer panel.
* Your hook is handed the host's own text as its first argument, so you can keep
  it, wrap it or throw it away without asking the host for its state.

### Which door reaches which interface

The slots in `beeagent/ui/skin.py` (`frame`, `banner`, `spinner`, `stream`) predate
the skin host and are read only by the classic REPL's components; the surfaces are
asked by both. That difference decides what a skin can promise:

| what you want to change | the door | classic REPL | the TUI |
| --- | --- | --- | --- |
| the state line, the waiting line, the reasoning tail | `status`, `spinner`, `thinking` | yes | yes |
| the answer text as it arrives | `stream` | yes | yes |
| the answer block: outline, lists, headings | `answer` | yes | yes |
| a strip of its own rows above the state line | `hud` | yes | yes |
| the border every panel and table wears | `frame` slot | yes | no — Textual styles borders in CSS |
| the opening wordmark | `banner` slot | yes | no — the TUI draws its own header |
| the whole line printer, not one line of it | `stream` slot (a `ResponseStream` subclass) | yes | no |

`skin-shimmer` in `beeagent/plugins/templates/plugins/` is the pack that uses every
row of this table at once, and says in its own docstring which half of it the TUI
cannot see.

Cost is the other half of the contract. A surface line is asked on the caller's
thread — per token for `stream` — so the event budget applies: three calls over it
and the surface is dropped; a single call over the hard cap loses the surface *and*
stops the skin drawing at all. The notice says which line and why, once.

In the TUI these lines are redrawn by the same 12 fps clock that runs your
`on_frame`, and the clock only runs while the skin on screen can show movement. A
skin that claims nothing and only has `on_frame` is not left invisible: the rows it
paints are shown as the strip above the state line, which is how `skin-pulse`,
`skin-pet` and `skin-baseline` are seen in the default interface at all. In the
classic REPL each line is printed as it happens and cannot be redrawn — a spinner
there answers once per turn, which is the honest limit of a line-oriented terminal.

Two names wear one skin. A pack folder and the skin inside it usually differ — the
shelf says `skin-pulse`, the skin says `pulse` — so `/skins` accepts both, in any
case, and the listing shows `pulse (skin-pulse)`. Installing a skin pack puts it on
when nothing was chosen, and otherwise prints the one command to type.

The order a skin can observe, end to end:

```
register(name, skin)      # your file is imported; nothing is drawn; nothing is active
switch(name)              # you become active; on_init(ctx) runs once
  post(event, payload)    # agent events, as they happen, on the caller's thread
  frame(painter, dt)      # the render loop's thread
  emit_output(text)       # the UI, just before it prints an answer
unload(name)              # forgotten; if it was active, the baseline returns
```

Registering never activates: choosing and drawing are separate acts, and a skin that
became active on import would replace the interface without being asked.

`post()` hands `on_event` a dict. A payload that was not a dict arrives wrapped as
`{"data": <it>}`. The event names are the agent's own, read off the call sites in
`beeagent/core/agent.py`:

| event | payload keys | fires when |
| --- | --- | --- |
| `stream_delta` | `text` | the model wrote a chunk of the answer |
| `reasoning_delta` | `text` | the model wrote a chunk of its reasoning |
| `tool_start` | `tool`, `args` | a tool call is about to run |
| `tool_end` | `tool`, `args`, `output`, `error` | …and it came back |
| `tool_denied` | `tool`, `args`, `message` | the permission gate refused it |
| `done` | `text` | the turn's answer is complete |
| `stopped` | `turn` | the user stopped the turn |
| `error` | `message` | the request failed, or the model gave up |
| `retry` | `attempt` | a request is being retried |
| `economy_hit` | — | the answer came from the local cache |
| `context_trimmed` | `dropped` | history was cut to fit the window |
| `nudged` | — | the model went quiet and got a nudge |

Those twelve are `skins.CORE_EVENTS`; `skins.EVENTS` is the whole vocabulary the
agent emits, including `status`, `response`, `waiting`, `stream_reset`,
`model_switched`, `provider_fallback`, `queued_sent`, `tool_repaired`,
`tool_dropped`, `tool_renamed`, `tool_unknown`, `tool_error`. An unknown name is
still delivered and counted (`skins.unknown_counts()`), because an emitter and a
list drift, and dropping an event silently is the failure nobody notices.

## 4. The painter

`on_frame`'s second argument is `beeagent.core.renderer.Painter`. Six methods, and
that is the surface:

| call | returns | notes |
| --- | --- | --- |
| `draw_text(x, y, text, color=None, bg=None, style=None)` | cells written (`int`) | top-left at `(x, y)`, 0-based columns/rows. `style` is `"bold"`, `"bold dim"`, `"underline"`, … — unknown words are dropped with a warning, not an exception |
| `draw_box(x, y, w, h, border=None, fill=None, title=None)` | `None` | box-drawing rectangle; `charset="light"` by default, `"ascii"` and `"none"` also available. Refuses `w<=1` or `h<=1` and warns |
| `clear_region(x, y, w, h)` | `None` | blanks a rectangle back to nothing. This is how you erase a line you drew last frame |
| `get_terminal_size()` | `(cols, rows)` | cells. 80×24 when there is no terminal to ask |
| `color_support()` | `"truecolor"` \| `"256"` \| `"16"` | detected once, cached; see below |
| `color(value)` | an opaque token (`str`) | accepts `(r,g,b)`, `"#rgb"`, `"#rrggbb"`, a palette name, or a token this returned. Hand it back; do not parse it |

Coordinates are **cells**, not bytes and not Python characters. `"привет"` is 6
cells; `你` is 2. `draw_text` reports the cells it wrote, and the difference between
that and `len()` of your string is how much the grid clipped.

Everything out of range is clipped, not raised. A bad colour falls back to the
terminal default. A control character inside your text is stripped before it is
drawn. The reasons land in `painter.frame_warnings`, which the host surfaces — your
skin does not have to police itself for correctness, only for cost.

### The widgets, on top of the six

Those six are the floor. The painter in this build also answers the calls below, and
they behave exactly like `draw_text`: they clip, they degrade colours, and every
shortcut lands in `frame_warnings`. Nothing in them can raise.

| call | what it draws |
| --- | --- |
| `draw_bar(x, y, width, fraction, color=None, bg=None, filled="█", empty="░")` | a progress bar; `fraction` in 0..1 |
| `draw_ramp(x, y, width, color_a, color_b, filled="█", empty=" ", fraction=1.0, bg=None, style=None)` | `width` cells coloured from A to B, filled as far as `fraction` |
| `draw_sparkline(x, y, width, values, color=None, bg=None)` | the last numbers as one row of `▁▂▃▄▅▆▇█` |
| `draw_ticker(x, y, width, text, phase=0.0, color=None, bg=None, style=None, gap=3)` | a marquee: `text` through `width` cells, scrolled by anything that grows |
| `cell(x, y)` | the cell already on the grid, `None` off it — read what is drawn instead of remembering it |
| `cols`, `rows` | the grid you are being asked to paint, which is the *terminal* in a frame loop and your `HUD_ROWS` strip in `on_hud` |

A skin that must also run on a build without them asks first — four lines, and
`skin-hud`'s `paint()` is that helper:

```python
def paint(painter, widget, *args, **kwargs):
    call = getattr(painter, widget, None)
    if not callable(call):
        return False
    call(*args, **kwargs)
    return True
```

### Colour and motion for a line of text

`status` and `spinner` reach the screen as Rich markup, so the arithmetic that turns
a colour into a string lives in `beeagent.core.renderer` — which is on the import
allow-list, and the only legal way for a skin to get one:

| call | returns |
| --- | --- |
| `markup(text, fg=None, bg=None, style=None)` | `text` inside one tag, with its own square brackets escaped |
| `ramp_markup(text, a, b, style="")` | every character its own colour between `a` and `b` — a shimmer in one call |
| `bar_markup(fraction, width=10, filled="█", empty="░", fg=None, bg=None, label="")` | a bar as text, for a line printed with Rich instead of a grid |
| `spark_markup(values, fg=None, width=0)` | a row of block glyphs for the last few numbers |
| `ticker_markup(text, width, phase_value=0.0, gap=3, fg=None)` | a scrolling strip cut to `width` cells |
| `blend(a, b, t)` | the colour between two, as `#rrggbb`; `t` is clamped, names are resolved |
| `ease(name, t)` | `t` through `"linear"`, `"in"`, `"out"`, `"in_out"` or `"pulse"`; an unknown name is linear |
| `phase(clock, period=1.0)` | where in a cycle of `period` seconds you are, 0..1 |
| `blink(on, text, off=None)` | the text or its own width in spaces, so a line does not jump when a dot leaves it |
| `text_cells(text)` | how many columns `text` will take: `"привет"` is 6, `你` is 2 |

### The colour story

You never emit an escape sequence. Not `\x1b[38;2;…`, not a `rich` markup string,
not `\033c`. Three reasons, all of them true on a machine you will be asked to
support:

1. **You do not own the screen.** `renderer.Emitter` diffs two grids and writes
   only what changed, so an escape you send yourself is a byte the diff does not
   know about: the next frame will not clear it, and the cell you wrote is still
   holding the colour you asked for while the character over it is somebody else's.
2. **The terminal decides what your colour is.** `Painter` degrades the colour you
   asked for into the deepest palette this terminal can actually show, and every
   skin on the same terminal degrades the same way. Two skins, one look.
3. **An escape a terminal does not understand is printed as letters.** On a plain
   conhost without virtual-terminal processing, `\x1b[38;2;255;204;0m` shows up as
   `[38;2;255;204;0m` in the middle of your status line. The renderer turns that
   case on for you (`_enable_windows_vt`), and turns itself off for a pipe — a
   `beecode | grep` gets no escape codes at all, because corrupting the *other*
   program is not a feature.

What `color()` does with the honey the interface is built on, measured:

| you ask | `truecolor` | `256` | `16` |
| --- | --- | --- | --- |
| `"honey"` / `"#ffcc00"` / `(255, 204, 0)` | `#ffcc00` | `x256:220` | `x16:11` |
| `"leaf"` / `"#7cb342"` | `#7cb342` | `x256:107` | **`x16:8`** |
| `"red"` | `x16:1` | `x16:1` | `x16:1` |

Three facts in that table:

* The same colour asked three ways gives the same token: a name from
  `renderer.NAMED_RGB` (`honey`, `leaf`, `amber`, `hive`, `flash`, `cream`, `lime`,
  `ink`, `paper`), a hex string, or an `(r, g, b)` tuple.
* **The 16 basic names never degrade.** `red` stays `x16:1` at every depth, because
  `30`–`37`/`90`–`97` mean *whatever the user's theme says red means*. A fixed RGB
  would overwrite their theme. Prefer a basic name where a basic name is honest.
* Degradation is nearest-neighbour on squared RGB distance, and nearest is not
  always what you meant: on a 16-colour console `#7cb342` (leaf green) collapses to
  **bright black**. The renderer is not wrong — grey *is* the closest of the sixteen
  — but the result is not your design. So a skin that cares ships its own map for
  the 16 case, which is exactly what the reference skins do:

```python
ANSI16 = {"#ffcc00": "yellow", "#7cb342": "green", "#43a047": "green"}
SIXTEEN = ("16",)


def tone(painter, value):
    """The colour to hand `draw_text`, in a spelling this terminal can print."""
    depth = str(painter.color_support()).lower()
    if depth in SIXTEEN:
        return ANSI16.get(value, "white")     # 16 colours: pick what reads right
    return value                              # anything else: let the renderer degrade it
```

`str()` and a tuple test, not an `==`, because the contract's three spellings are
not the only three in the wild: `renderer.color_support()` does return exactly
`"truecolor" | "256" | "16"`, but the fallback painter in `core/skins.py`
(`NullPainter`, what you get when the renderer is not importable) answers with a
`rich` colour-system name — `"standard"`, `"windows"`, `"truecolor"`, `"none"`.
Anything you do not recognise goes through the renderer's degradation, which is the
safe direction: a wrong colour beats a crash.

### Frames, and what a frame is allowed to cost

The host's numbers, as constants in `beeagent.core.skins`:

| constant | value | meaning |
| --- | --- | --- |
| `FRAME_BUDGET_MS` | `8.0` | wall clock one `on_frame` may take |
| `DEMOTE_AFTER_OVERRUNS` | `3` | over-budget frames *in a row* that end the skin |
| `HARD_CAP_FACTOR` | `4.0` | …so a single frame over **32 ms** ends it at once: that is a hang, not decoration |
| `EVENT_BUDGET_MS` | `8.0` | the same clock, charged to `on_init`, `on_event` and `on_output` |

Going over is not a punishment, it is a handback: the skin is demoted to the
baseline, once, with a visible sentence naming the skin and the reason
(`skin "pulse" was stopped and the plain interface is back: a frame took 41.3 ms,
past the 32 ms ceiling — that is a hang, not decoration`). A hook that raises is
demoted immediately, the same way, with the exception text shown once. Never a
silent switch, never a second notice. After a demotion the only way back is to
register the skin afresh.

Note what `EVENT_BUDGET_MS` means for `on_event`: it runs **on the agent's own
thread**, inside the token loop, because a token stream cannot tolerate
reordering. A slow `on_event` is not a dropped frame, it is a slow answer. And
measuring a frame after it returned is not stopping it — Python offers no way to
preempt in-process code — so the policy is "one late frame, then never again from
this skin". That is why the ceiling is yours to respect, not theirs to enforce.

## 5. A complete skin: `thinking-pulse`

Two hooks, five fields, all of it real. The whole skin does one thing: a cell of
light that moves while the model works, and wipes itself when it stops.

`.beeagent/plugins/thinking-pulse/plugin.json` is the manifest in section 2, with
`name` and `description` matching the folder.

```python name=plugin.py
"""thinking-pulse: a cell of light that moves while the model works."""
from beeagent.core.skins import register

NAME = "thinking-pulse"


class Pulse:
    """A phase that only `dt` moves, and a light that only the events turn on."""

    ramp = ".-~=+*#@"              # ASCII on purpose: a cp1251 console has no braille
    word = "thinking "
    t = 0.0                        # 0..1 through one cycle — the only clock there is
    busy = False
    painted = False

    def on_event(self, event, payload):
        self.busy = event in ("reasoning_delta", "stream_delta", "tool_start")

    def on_frame(self, dt, painter):
        cols, rows = painter.get_terminal_size()
        left, row = max(cols - len(self.word) - 1, 0), rows - 1
        if not self.busy:
            if self.painted:                       # one last frame, to wipe the row
                self.painted = False
                painter.clear_region(left, row, len(self.word) + 1, 1)
            return                                 # idle frames cost two float adds
        self.t = (self.t + dt) % 1.0
        painter.draw_text(left, row,
                          self.word + self.ramp[int(self.t * len(self.ramp))],
                          color="honey")
        self.painted = True


skin = Pulse()


def setup(api):
    """The loader calls this. It registers; it does not draw."""
    register(NAME, skin)
```

Things to notice, because each one is a rule from sections 3 and 4:

* `register(NAME, skin)` hands over an **object with hook attributes**. A dict of
  callables works too — `register(NAME, {"on_frame": …})` is read as a lifecycle,
  and `api.skin_hooks()` in a pack goes through that door — but a dict whose keys
  are colours is the legacy skin format, and that one draws nothing.
* No `on_init`. It is optional; class attributes are this skin's initial state. No
  `on_output` either, and `post()` returns `False` for skins that do not want it.
* `on_event` sets one boolean and returns. It does not paint, does not allocate, and
  does not read the payload — which is why it cannot be the thing that blows the
  8 ms event budget.
* The pulse advances from `dt`, never from `time.sleep`, never from
  `time.monotonic()`. The same sequence of `dt` values gives the same frames, so a
  test can drive a whole turn without waiting for anything.
* `painted` is this skin's own memory of a line it drew. `painter.cell(x, y)` can
  read a cell back, but a skin that clears a region by asking what is in it will
  clear the wrong region the first time another skin has drawn the same glyph.
* The glyph is `.`–`#`, not `⠋⠙⠸⠴`. See section 6.

Install it (see the third row of the table in section 2 for the development route),
then switch to it:

```
/skins thinking-pulse      -> skin: thinking-pulse
/skins                     -> the list, `*` marks what is in force
/skins off                 -> skin: baseline
```

`/skins <name>` is a *switch*, and it is the only thing that calls your `on_init`.
It is also a setting: the name goes into `beeagent.json` as `skin`, and the next
start puts it back on after the packs have registered it — before them there would
be no name to switch to. `off` writes an empty value, which means BeeCode's own
interface. A name nothing registered (a pack uninstalled from this folder) is
reported by `/extensions` and the baseline stays.
If it answers with anything other than `skin: <name>`, that sentence is your error:
`no skin "…" — /skins lists what there is`, a refusal with a line and a token, or
`skin "…" is stopped: <reason>`.

### Driving it from a test, without a terminal

This is what `tests/test_skin_docs.py` does to the block above, and it is the loop
to keep — no tty, no thread, no sleep:

```python
import importlib.util
from pathlib import Path

from beeagent.core import renderer, skins

PACK = Path(".beeagent/plugins/thinking-pulse")


def test_the_pulse_lights_and_wipes():
    skins.reset()                               # the registry is process-global
    source = (PACK / "plugin.py").read_text(encoding="utf-8")
    assert skins.check_source(source) == []     # the gate, before any of it runs

    spec = importlib.util.spec_from_file_location("pulse_under_test", PACK / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)             # what PluginLoader does with a pack
    module.setup(None)                          # …and the function it calls, given an api
    assert "on_frame" in skins.stats("thinking-pulse")["hooks"]

    grid = renderer.Grid(60, 12)
    painter = renderer.Painter(grid, size=(60, 12))    # a real grid, never sent to a tty
    assert skins.switch("thinking-pulse") == ""        # "" is how success reads
    skins.post("reasoning_delta", {"text": "hi"})

    for dt in (0.1, 0.15, 0.2):
        assert skins.frame(painter, dt) < skins.budget()["frame_ms"]
    assert "thinking" in grid.line(11)

    skins.post("done", {})
    skins.frame(painter, 0.1)                   # the wipe lands on the next frame
    assert grid.line(11).strip() == ""
```

`renderer.Painter(grid, size=…)` never touches stdout: only `FrameLoop` emits, and
only to a terminal. Asserting on `grid.line(y)` is the whole trick.

This version calls `setup()` by hand to keep the loop short.
`tests/test_skin_docs.py` runs the same extracted pack through the real
`PluginLoader` — an `Agent` built in a temp folder with the trust answer recorded —
so the file layout and the install ledger get tested too, not just the code.

### A second skin: three lines, no animation at all

This one is the smallest thing that owns part of the interface. It keeps the host's
words everywhere and adds the one number the user has no other way to see — how
much has arrived — on the line, on the waiting phrase, on the answer block and as a
bar in its own row.

```python name=surfaces.py
"""turn-ledger: counts what arrived, on the four lines it takes over."""
import beeagent.core.renderer as R

NAME = "turn-ledger"
SURFACES = ("status", "spinner", "answer", "hud")
HUD_ROWS = 1
HONEY, LEAF = "#ffcc00", "#7cb342"

tokens = 0


def on_event(event, payload):
    global tokens
    if event == "stream_delta":
        tokens += 1


def on_status(default):
    return R.markup("%d tok" % tokens, HONEY, style="bold") + " " + (default or "")


def on_spinner(default, clock):
    return R.ramp_markup(default or "working", HONEY, LEAF)


def on_answer(text, final=False):
    # A source skin cannot import Rich, so markup is how it draws a block: this
    # one puts a counted line above the answer and leaves the text itself alone.
    return R.markup("%d tok" % tokens, LEAF, style="bold") + "\n" + text.replace(
        "[", "\\[")


def on_hud(painter, dt):
    painter.draw_bar(0, 0, painter.cols, min(1.0, tokens / 200.0), color=LEAF)


def hooks():
    return {"SURFACES": SURFACES, "HUD_ROWS": HUD_ROWS, "on_event": on_event,
            "on_status": on_status, "on_spinner": on_spinner,
            "on_answer": on_answer, "on_hud": on_hud}


def setup(api):
    api.skin_hooks(NAME, hooks())
```

What to notice:

* `SURFACES` travels with the hooks. A claim is read off the registered object, so
  a pack that hands over a dict of functions and leaves `SURFACES` in the file owns
  nothing and will wonder why.
* `on_status` keeps `default` and prepends its own words. Replacing the state line
  outright is allowed and usually a loss: the host's text names the model, the
  provider and what went wrong this turn.
* `on_spinner` gets the host's phrase and the monotonic clock, and `ramp_markup`
  turns the phrase into a wave. Returning `None` would have left the joke alone.
* `on_answer` escapes the brackets in the answer before returning it as markup:
  the text is the model's, and a `[bold]` inside a quoted piece of code is data.
  A pack that may `import rich` would return a renderable here instead and get an
  outline, its own list markers and its own headings — see `skin-shimmer`.
* `on_hud` is handed a painter whose grid is exactly `HUD_ROWS` rows tall — the
  strip, not the screen. `painter.cols` is the width you have, and it is 30 columns
  on somebody's terminal.
* Nothing here animates. A skin that owns `status` is asked whenever that line is
  written, which in the TUI is twelve times a second, so the cheap answer is often
  the right one.

`tests/test_skin_docs.py` installs this block from source text — so it passes the
gate before it paints — switches to it, and reads the four lines back, including the
markup answer.

## 6. Rules that keep a skin cheap

The budget is 8 ms per frame at 12 fps. That is not much, and it is not the point:
the point is that your code runs in the same process as the answer the user is
waiting for.

* **No allocation storms per frame.** Building the same 12-character string twice is
  free; `[Cell(x, y, ch) for x in range(cols) for y in range(rows)]` at 12 fps is
  144 000 objects a second, and the GC pauses will show up as a stuttering spinner.
  Prefer: precompute constant geometry into module names, index one string
  (`self.ramp[i]`), cache the line you last drew and skip the paint when it has not
  changed, and use one-entry caches (`self.cache = (key, text)`) rather than dicts
  that nobody prunes. `__slots__` on the skin class is a free win, and legal: the
  gate refuses *reading* `_`-prefixed names, and a class-body assignment is a store.
* **Nothing unbounded in a pet.** A `list` of history — every token, every tool call,
  every answer — is a memory leak with a friendly name, and a long session is hours
  of tokens. If you need history, bound it: `collections.deque(maxlen=4)`, a
  count, a hash. A `dict` keyed by tool name needs a key that leaves: drop entries
  older than some `clock` value inside `on_frame`, and *only* there — a timer you
  never service is a timer that leaks.
* **Animate from `dt`, not from `time.sleep`.** `on_frame` is a callback on the
  render loop's thread; sleeping inside it does not slow your animation, it stalls
  the loop and gets you demoted (`frame()` measures wall time). `time` is on the
  allow-list for one acceptable use: `time.perf_counter()` to measure yourself
  before the host does. Never read the clock to decide *what* to draw — `dt`
  already carries that, and reading it makes your skin untestable and
  nondeterministic.
* **Never assume a UTF-8 console.** This is not hypothetical: the machines BeeCode
  ships to include Russian Windows boxes whose console is cp1251 and cannot encode
  `▰`, `⠿`, `●`, or most box-drawing glyphs — and the host's own strings are not
  immune either: `/skins` prints curly quotes, and on this project's terminal they
  arrive as `?`. So: ASCII for anything you animate (`.-~=+*#@`, `|/-\`, `o *`), and
  if you want a box, take it from `draw_box`'s `"ascii"` charset rather than typing
  `╭`. Cyrillic *text* is a different matter — the renderer encodes with
  `errors="replace"`, so it degrades to `?` instead of raising — but a `?` in the
  middle of your animation is as broken as a crash.
* **An event payload is text a model wrote.** `tool_start`'s `args`, `error`'s
  `message` and `done`'s `text` are not yours and not safe. The renderer strips
  control characters from anything it draws (`utils/sanitize.strip_terminal`), so an
  OSC 52 in a tool argument will not silently rewrite the user's clipboard — but
  bound what you display anyway (`str(x)[:20]`), because `draw_text` will clip it
  into nonsense at the grid edge and a clipped label is worse than a short one.
* **Draw in `on_frame`, change state in `on_event`.** Tokens arrive far faster than
  12 a second. Painting from `on_event` means painting per token, off the render
  loop's clock, on the agent's thread.
* **Ask the painter, do not assume it.** `get_terminal_size()` and
  `color_support()` are in the contract; the `Painter` from a build without the
  renderer (`NullPainter`) answers differently. Read section 4's `tone()` for the
  shape of a defensive call: try the method, fall back to a number you can draw with.
* **Do not fight the widgets.** BeeCode's own panels draw through `beeagent/ui/skin.py`
  slots (frame, banner, spinner, stream). A skin that paints row 0 fights the
  banner. Pick a row nothing else owns and `clear_region` exactly that.

## 7. What the API does not have yet

Things hit while writing and driving the skins above. Each one is a real gap, not a
wish: this is what a first-time author finds, so the engine agents can decide which
of them to close before the first stranger does.

Closed since the first draft of this page, so nobody reports them twice: a dict of
hooks is now read as a lifecycle instead of a colour table; `ExtensionAPI` has a
`skin_hooks()` door; `Context` carries `language()` and `translate(en, ru)`; the
painter has `cell()` and the widgets; `beeagent.core.renderer` has the blend,
easing and markup helpers, which is why the reference packs stopped copying a colour
table into every file; and the answer became a surface (`answer`), so a skin can
draw the reply itself — outline, lists and all — in both interfaces.

1. **The AST gate never sees a `plugin.py`.** It runs on source text
   (`install_source`, `register(source=…)`), and the loader's `exec_module` path is
   gated by `/trust` alone. So the guide's "your file must pass the gate" is a
   convention for the folder route, not an enforcement. Either run
   `check_source()` on the entry file before importing it, or say in the interface
   which door a pack came through. The three legacy slot packs import `rich` and
   `beeagent.ui.components` on purpose, so a blanket gate would refuse them: the
   answer has to name the door, not just widen or narrow it.
2. **`plugin.json` is decorative.** Nothing reads `name`, `version`, `type` or
   `entry`; the loader hardcodes `plugin.py`, and a folder is loaded only if
   `.beeagent/plugins.json` names it. Honour `entry`, and give the development loop
   a real command — `/plugin install ./folder`, or `--link`.
3. **There is no `on_shutdown`.** `unload()` is the host forgetting a skin without
   telling it. A skin whose state is numbers does not care; a skin that opened
   anything — a thread, an accumulator a pet feeds — has no door to close it
   through, and `/skins off` will not call it.
4. **A frame nobody emits is invisible, and says so nowhere.** `frame(dt)` with no
   painter hands your skin a real `Painter` whose grid nothing writes to a screen:
   the draw calls succeed, `stats()` counts the frame, and nothing moves. That is
   the right design for a headless run and the most confusing first hour of writing
   a skin, because it looks like a skin that does not work. The TUI now hands
   `on_frame` the strip's own rows — one to four — so a skin that paints a row is
   seen, and a skin that paints a whole screen picture still is not.
   `skins.painter_visible()`, or the loop reporting "nobody is emitting these
   frames", would turn the rest of it into a message instead of a mystery.
5. **Three pieces of the interface have no surface.** `status`, `spinner`,
   `thinking`, `stream`, `answer` and the `hud` strip are yours to claim; the
   banner, the prompt's own prefix and the sidebar are not, and are rewritten only
   through the legacy `ui/skin.py` slots a skin cannot reach from the TUI. The
   answer *block* became a surface with `skin-shimmer`; the border a Textual panel
   wears is still CSS, which no skin has a door to.
6. **The classic REPL cannot redraw.** It is line-oriented on purpose — the prompt
   owns the screen under `patch_stdout` — so a `spinner` there answers once per
   turn and no animation follows a line that already scrolled away. Anything that
   moves has to live in the TUI or in a strip the host repaints.

## 8. Reference packs

Five shipped packs are the other half of this document; read them before asking why
something is missing here.

| pack | what it teaches |
| --- | --- |
| `skin-baseline` | a static line, and the shape every other skin falls back to |
| `skin-pulse` | a token-nudged pulse, a tool progress bar, self-policing against the frame budget |
| `skin-pet` | an ASCII animal: moods from events, bounded state, and a plain terminal that gets one line |
| `skin-hud` | **surfaces**: claiming four lines, answering for them, drawing a timed strip, and asking only for what the screen can hold |
| `skin-shimmer` | **the answer**: a ramp-coloured outline, list markers and headings that walk it, a railed streaming line, heavy borders on every panel, and a two-row HUD |
| `skin-prism` | **the whole screen**: seven surfaces at once — a crest that loops across the logo under a palette that rotates on its own slower clock, border colours that sit at different points of one cycle per panel, an answer that keeps every character the model wrote, and a two-row HUD |
| `skin-hive` / `skin-work` / `skin-terminal` | legacy slot packs: colours and callables, no `on_frame` at all |
| `plain` | frameless interface through the slots |

They are in `beeagent/plugins/templates/plugins/`, and they install with
`/plugin install skin-shimmer`. A pack's `setup(api)` hands its lifecycle to the
host through `api.skin_hooks(name, hooks)`, which is the door this document
describes; where a build has no such verb, the packs fall back to the event bus
rather than sit out the turn.
