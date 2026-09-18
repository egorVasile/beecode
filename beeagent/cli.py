import argparse
import sys
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.config.loader import load_config
from beeagent.providers.g4f_provider import G4fProvider

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
    parser.add_argument("--format", choices=["default", "json", "stream"], default="default")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("models", help="List available models")
    sub.add_parser("providers", help="List available providers")

    args = parser.parse_args()

    if args.command == "models":
        p = G4fProvider()
        print("Available models:")
        for m in p.models:
            print(f"  - {m}")
        return

    if args.command == "providers":
        print("Available providers:")
        print("  - g4f (GPT4Free)")
        print("  - openai_compat (OpenAI-compatible)")
        print("  - ollama (Local)")
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
        result = agent.run_sync(args.prompt)
        print(result)
        return

    session = None
    if args.continue_session:
        sessions = Session.list_sessions()
        if sessions:
            session = Session.load(sessions[-1])
            print(f"Continuing session: {session.session_id}")

    print("BeeAgent v0.1.0 — Free AI Coding Agent")
    print("Type your request or 'quit' to exit.\n")

    session = session or Session()

    try:
        while True:
            try:
                user_input = input("bee> ").strip()
            except EOFError:
                break

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                break

            result = agent.run_sync(user_input, session=session)
            print(f"\n{result}\n")

    except KeyboardInterrupt:
        print("\nGoodbye!")

    finally:
        session.save()

if __name__ == "__main__":
    main()
