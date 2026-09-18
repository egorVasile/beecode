import argparse
import sys
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.config.loader import load_config
from beeagent.providers.g4f_provider import G4fProvider
from beeagent.ui.components import (
    console, print_banner, print_welcome,
    render_tool_start, render_tool_end,
    render_response, render_error,
    render_economy_hit, render_model_info,
    print_models, print_providers,
)

def handle_callback(event: str, data: dict):
    if event == "tool_start":
        render_tool_start(data["tool"], data["args"])
    elif event == "tool_end":
        render_tool_end(data["tool"], data["args"], data["output"], data["error"])
    elif event == "response":
        render_response(data["text"])
    elif event == "error":
        render_error(data["message"])
    elif event == "economy_hit":
        render_economy_hit()

def main():
    parser = argparse.ArgumentParser(
        prog="beeagent",
        description="BeeAgent — Free AI coding agent powered by g4f",
    )
    parser.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive)")
    parser.add_argument("--model", help="Model to use")
    parser.add_argument("--provider", help="Provider to use")
    parser.add_argument("--mode", choices=["normal", "economy"], help="Operating mode")
    parser.add_argument("--continue", dest="continue_session", action="store_true", help="Continue last session")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("models", help="List available models")
    sub.add_parser("providers", help="List available providers")

    args = parser.parse_args()

    if args.command == "models":
        p = G4fProvider()
        print_models(p.models)
        return

    if args.command == "providers":
        print_providers([
            {"name": "g4f", "type": "free", "desc": "GPT4Free — no account needed"},
            {"name": "openai_compat", "type": "api", "desc": "OpenAI-compatible endpoint"},
            {"name": "ollama", "type": "local", "desc": "Local models via Ollama"},
        ])
        return

    config = load_config()
    if args.model:
        config.model = args.model
    if args.provider:
        config.provider = args.provider
    if args.mode:
        config.mode = args.mode

    agent = Agent(config=config)

    if args.prompt:
        print_banner()
        render_model_info(config.model, config.provider, config.mode)
        console.print()
        agent.run_sync(args.prompt, callback=handle_callback)
        return

    session = None
    if args.continue_session:
        sessions = Session.list_sessions()
        if sessions:
            session = Session.load(sessions[-1])
            console.print(f"  [dim]Continuing session: {session.session_id}[/]")

    print_banner()
    render_model_info(config.model, config.provider, config.mode)
    print_welcome()

    session = session or Session()

    try:
        while True:
            try:
                console.print()
                user_input = console.input("[bold green]🐝 > [/]").strip()
            except EOFError:
                break

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                console.print("\n  [dim]Goodbye! 🐝[/]\n")
                break

            agent.run_sync(user_input, session=session, callback=handle_callback)

    except KeyboardInterrupt:
        console.print("\n  [dim]Goodbye! 🐝[/]\n")

    finally:
        session.save()

if __name__ == "__main__":
    main()
