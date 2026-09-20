"""Measure a model's real context window by making the endpoint refuse.

g4f does not publish window sizes, so the number has to be coaxed out of the
provider: send a growing prompt until it errors, and read the limit from what
it says. Results are cached in .beeagent/windows.json and win over any guess
from the model name — BeeCode then sizes its own requests to the real number.

    python scripts/probe_window.py gpt-4o-mini
    python scripts/probe_window.py --all-candidates      # curated list
    python scripts/probe_window.py --ceiling 32768 glm-4.7-flash
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beeagent.config.loader import load_config                 # noqa: E402
from beeagent.core import windows                              # noqa: E402
from beeagent.core.agent import Agent                          # noqa: E402
from beeagent.providers.g4f_provider import G4fProvider        # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure model context windows.")
    parser.add_argument("model", nargs="?", help="model id to measure")
    parser.add_argument("--all-candidates", action="store_true",
                        help="measure every model on the curated list")
    parser.add_argument("--ceiling", type=int, default=windows.LADDER[-1],
                        help="largest size to try, tokens")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    config = load_config(str(ROOT))
    agent = Agent(config=config)
    provider = agent.providers.select(config.provider)

    targets = list(G4fProvider.models) if args.all_candidates else [args.model]
    if not targets or not targets[0]:
        parser.error("give a model, or --all-candidates")

    for model in targets:
        print(f"\n{model}:", flush=True)

        def progress(size, accepted):
            print(f"  … {size} tokens (last accepted {accepted})", flush=True)

        result = asyncio.run(windows.probe(
            model, provider, ceiling=args.ceiling, timeout=int(args.timeout),
            on_step=progress))
        if result.window:
            print(f"  ✅ window ≈ {result.window} tokens ({result.note})", flush=True)
        else:
            print(f"  ❌ {result.note}", flush=True)


if __name__ == "__main__":
    main()
