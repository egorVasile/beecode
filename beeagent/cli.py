import argparse
import asyncio
import os
import sys

from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.config.loader import load_config
from beeagent.providers.g4f_provider import G4fProvider
from beeagent.ui.components import (
    console, print_banner, print_welcome,
    render_model_info, print_models, print_providers,
)
from beeagent.ui.repl import run_repl, handle_callback

_PLUGIN_ACTIONS = ("list", "install", "remove", "uninstall", "enable", "disable")
_MCP_ACTIONS = ("list", "add", "remove", "connect", "tools")


def _run_command(action_words, prefix, known, fallback_line):
    """Reuse the slash-command handlers for the non-interactive CLI."""
    from beeagent.ui.commands import ReplContext, dispatch
    words = " ".join(action_words or [])
    first = words.split(maxsplit=1)[0] if words else ""
    line = f"{prefix} {words}" if first in known else fallback_line
    result = dispatch(ReplContext(), line)
    if result.output is not None:
        console.print(result.output)


def update_self(root=None) -> int:
    """Bring BeeCode up to date, whichever way it was installed.

    An editable install *is* a checkout, so pulling the checkout is the update —
    and running pip there would replace the development copy with a frozen one.
    Anything else lives in site-packages and needs pip, which also refreshes g4f
    and its borrowed endpoints.
    """
    import subprocess
    from pathlib import Path

    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    if (base / ".git").exists():
        print(f"updating the checkout at {base}", flush=True)
        return subprocess.call(["git", "-C", str(base), "pull", "--ff-only"])
    print("updating the installed BeeCode and g4f", flush=True)
    return subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade",
                            "git+https://github.com/egorVasile/beecode.git"])


def main():
    parser = argparse.ArgumentParser(
        prog="beecode",
        description="BeeCode — free AI coding agent powered by g4f",
    )
    parser.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive)")
    parser.add_argument("--model", help="Model to use")
    parser.add_argument("--provider", help="Provider to use")
    parser.add_argument("--mode", choices=["normal", "economy"], help="Operating mode")
    parser.add_argument("--lang", choices=["en", "ru"], help="Interface language (default: en)")
    parser.add_argument("--continue", dest="continue_session", action="store_true", help="Continue last session")
    parser.add_argument("--classic", action="store_true", help="Force the classic line REPL (default)")
    parser.add_argument("--tui", action="store_true", help="Launch the full-screen Textual TUI")
    parser.add_argument("--update", action="store_true",
                        help="Fetch the newest BeeCode and g4f, then exit")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("models", help="List available models")
    sub.add_parser("providers", help="List available providers")
    sub.add_parser("tui", help="Launch the full-screen mouse-driven TUI")
    plugins = sub.add_parser("plugins", help="Browse/install the extension catalog")
    plugins.add_argument("action", nargs="*",
                         help="list | install <name> | remove <name> | enable <name> | disable <name>")
    mcp = sub.add_parser("mcp", help="Inspect configured MCP servers")
    mcp.add_argument("action", nargs="*",
                     help="list | add <name> <command> [args] | remove <name> | connect <name> | tools")

    args = parser.parse_args()

    if args.update:
        sys.exit(update_self())

    if args.command == "models":
        print_models(G4fProvider.discover_models())
        return

    if args.command == "providers":
        print_providers([
            {"name": "g4f", "type": "free", "desc": "GPT4Free — no account needed"},
            {"name": "openai_compat", "type": "api", "desc": "OpenAI-compatible endpoint"},
            {"name": "ollama", "type": "local", "desc": "Local models via Ollama"},
        ])
        return

    if args.command == "plugins":
        _run_command(args.action, "/plugin", _PLUGIN_ACTIONS, "/plugins")
        return

    if args.command == "mcp":
        _run_command(args.action, "/mcp", _MCP_ACTIONS, "/mcp list")
        return

    config = load_config()
    from beeagent.i18n import set_lang
    set_lang(args.lang or os.environ.get("BEECODE_LANG") or config.language)
    if args.model:
        config.model = args.model
    if args.provider:
        config.provider = args.provider
    if args.mode:
        config.mode = args.mode

    # One-shot mode: no UI chrome beyond a banner.
    if args.prompt:
        agent = Agent(config=config)
        print_banner()
        render_model_info(config.model, config.provider, config.mode,
                          config.permissions.mode)
        console.print()
        agent.run_sync(args.prompt, callback=handle_callback)
        return

    session = None
    if args.continue_session:
        sessions = Session.list_sessions()
        if sessions:
            session = Session.load(sessions[-1])

    # Interactive: classic REPL by default; full-screen TUI only when asked.
    want_tui = args.command == "tui" or (args.tui and not args.classic)
    if want_tui:
        from beeagent.ui.tui import run_tui
        run_tui(config=config, session=session)
    else:
        agent = Agent(config=config)
        print_banner()
        render_model_info(config.model, config.provider, config.mode,
                          config.permissions.mode)
        print_welcome()
        asyncio.run(run_repl(agent, config, session=session))


if __name__ == "__main__":
    main()
