from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax

console = Console()

def render_markdown(text: str):
    md = Markdown(text)
    console.print(md)

def render_code(code: str, language: str = "python"):
    syntax = Syntax(code, language, theme="monokai", line_numbers=True)
    console.print(syntax)

def render_tool_result(tool_name: str, output: str, error: bool = False):
    from rich.panel import Panel
    style = "red" if error else "green"
    panel = Panel(output, title=f"[{style}]{tool_name}[/{style}]", border_style=style)
    console.print(panel)
