import sys
import time
from typing import Optional
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich.align import Align
from rich import box

console = Console()

BANNER = r"""
   ___               _
  / __\ ___   __ _ | |_   _   _  _ __  ___
 / _\ // _ \ / _` || __| | | | || '_ \/ __|
/ /   |  __/| (_| || |_  | |_| || | | \__ \
\/     \___| \__,_|\__|  \__,_||_| |_|___/
"""

def print_banner():
    console.print()
    text = Text(BANNER, style="bold green")
    console.print(Align.center(text))
    console.print(Align.center(Text("Free AI Coding Agent powered by g4f", style="dim")))
    console.print(Align.center(Text("v0.1.0", style="dim italic")))
    console.print()

def print_welcome():
    console.print()
    with Panel(
        Align.center(Text("Type your request or 'quit' to exit", style="dim")),
        border_style="green",
        box=box.ROUNDED,
        padding=(0, 2),
    ):
        pass
    console.print()

def render_tool_start(tool_name: str, tool_args: dict):
    args_str = ", ".join(f"{k}={v!r}" for k, v in tool_args.items())
    if len(args_str) > 60:
        args_str = args_str[:57] + "..."

    text = Text()
    text.append("  ⏳ ", style="bold")
    text.append(f"{tool_name}", style="bold yellow")
    text.append(f" {args_str}", style="dim")
    console.print(text)

def render_tool_end(tool_name: str, tool_args: dict, output: str, error: bool):
    if error:
        icon = "❌"
        color = "red"
    else:
        icon = "✅"
        color = "green"

    if tool_name == "write" and not error:
        path = tool_args.get("path", "file")
        console.print(f"    [bold green]●[/] [green]created file[/] [dim]{path}[/]")
        _render_code_preview(tool_args.get("content", ""))
    elif tool_name == "edit" and not error:
        path = tool_args.get("path", "file")
        console.print(f"    [bold green]●[/] [green]modified[/] [dim]{path}[/]")
    elif tool_name == "bash" and not error:
        cmd = tool_args.get("command", "")[:60]
        console.print(f"    [bold green]●[/] [green]executed[/] [dim]{cmd}[/]")
        if output.strip():
            _render_bash_output(output)
    elif tool_name == "read" and not error:
        path = tool_args.get("path", "file")
        console.print(f"    [bold cyan]●[/] [cyan]read[/] [dim]{path}[/]")
    elif tool_name == "grep" and not error:
        pattern = tool_args.get("pattern", "")
        console.print(f"    [bold cyan]●[/] [cyan]searched[/] [dim]{pattern}[/]")
    elif tool_name == "glob" and not error:
        pattern = tool_args.get("pattern", "")
        console.print(f"    [bold cyan]●[/] [cyan]found files[/] [dim]{pattern}[/]")
    elif tool_name == "git" and not error:
        cmd = tool_args.get("command", "")[:60]
        console.print(f"    [bold yellow]●[/] [yellow]git[/] [dim]{cmd}[/]")
    elif tool_name == "web_search" and not error:
        query = tool_args.get("query", "")[:60]
        console.print(f"    [bold blue]●[/] [blue]searched web[/] [dim]{query}[/]")
    elif tool_name == "todo" and not error:
        action = tool_args.get("action", "")
        console.print(f"    [bold magenta]●[/] [magenta]todo:[/] [dim]{action}[/]")
    else:
        console.print(f"    [bold {color}]{icon}[/] [{color}]{tool_name}[/]")

    if error and output:
        console.print(f"    [red]{output[:200]}[/]")

def _render_code_preview(content: str):
    if not content:
        return
    lines = content.split("\n")
    preview = lines[:12]
    if len(lines) > 12:
        preview.append(f"  ... ({len(lines) - 12} more lines)")

    code = "\n".join(preview)
    syntax = Syntax(code, "python", theme="monokai", line_numbers=False)
    panel = Panel(
        syntax,
        border_style="green",
        box=box.ROUNDED,
        padding=(0, 1),
    )
    console.print(panel)

def _render_bash_output(output: str):
    lines = output.strip().split("\n")
    preview = lines[:8]
    if len(lines) > 8:
        preview.append(f"  ... ({len(lines) - 8} more lines)")

    text = "\n".join(preview)
    panel = Panel(
        Text(text, style="dim"),
        border_style="dim",
        box=box.ROUNDED,
        padding=(0, 1),
    )
    console.print(panel)

def render_response(text: str):
    console.print()
    md = Markdown(text)
    panel = Panel(
        md,
        border_style="green",
        box=box.ROUNDED,
        title="[bold green]🐝 BeeAgent[/]",
        title_align="left",
        padding=(0, 1),
    )
    console.print(panel)
    console.print()

def render_error(message: str):
    console.print()
    panel = Panel(
        Text(message, style="red"),
        border_style="red",
        box=box.ROUNDED,
        title="[bold red]Error[/]",
        title_align="left",
    )
    console.print(panel)
    console.print()

def render_economy_hit():
    text = Text()
    text.append("  💾 ", style="bold")
    text.append("cache hit", style="bold green")
    text.append(" — response served from cache", style="dim")
    console.print(text)

def render_model_info(model: str, provider: str, mode: str):
    text = Text()
    text.append("  🐝 ", style="bold")
    text.append(f"model: ", style="dim")
    text.append(f"{model}", style="bold cyan")
    text.append(f"  provider: ", style="dim")
    text.append(f"{provider}", style="bold cyan")
    text.append(f"  mode: ", style="dim")
    text.append(f"{mode}", style="bold cyan" if mode == "normal" else "bold yellow")
    console.print(text)

def print_models(models: list[str]):
    console.print()
    table = Table(
        title="[bold green]Available Models[/]",
        box=box.ROUNDED,
        border_style="green",
        show_header=True,
        header_style="bold green",
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Model", style="bold cyan")
    table.add_column("Provider", style="dim")

    for i, model in enumerate(models, 1):
        table.add_row(str(i), model, "g4f")

    console.print(table)
    console.print()

def print_providers(providers: list[dict]):
    console.print()
    table = Table(
        title="[bold green]Available Providers[/]",
        box=box.ROUNDED,
        border_style="green",
        show_header=True,
        header_style="bold green",
    )
    table.add_column("Name", style="bold cyan")
    table.add_column("Type", style="dim")
    table.add_column("Description")

    for p in providers:
        table.add_row(p["name"], p["type"], p["desc"])

    console.print(table)
    console.print()
