from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.panel import Panel
from rich import box

console = Console()

def render_markdown(text: str):
    md = Markdown(text)
    console.print(md)

def render_code(code: str, language: str = "python"):
    syntax = Syntax(code, language, theme="monokai", line_numbers=True)
    panel = Panel(
        syntax,
        border_style="green",
        box=box.ROUNDED,
        padding=(0, 1),
    )
    console.print(panel)

def render_tool_result(tool_name: str, output: str, error: bool = False):
    style = "red" if error else "green"
    panel = Panel(
        output,
        title=f"[bold {style}]{tool_name}[/]",
        border_style=style,
        box=box.ROUNDED,
    )
    console.print(panel)
