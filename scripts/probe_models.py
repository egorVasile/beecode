"""Ask every advertised model one tiny question and report what actually answers.

Free endpoints come and go, so "is it working" is only true for right now —
this script is the way to find out instead of guessing from a README.

    python scripts/probe_models.py                 curated list (fast)
    python scripts/probe_models.py --all           every discovered g4f model
    python scripts/probe_models.py --limit 60      first N models
    python scripts/probe_models.py --json out.json also write machine-readable
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beeagent.config.loader import load_config          # noqa: E402
from beeagent.core.agent import Agent                   # noqa: E402
from beeagent.providers.g4f_provider import G4fProvider  # noqa: E402

PING = "Reply with exactly one word: ok"


async def ask(provider, model: str, timeout: float, sem):
    async with sem:
        started = time.monotonic()
        try:
            answer = await asyncio.wait_for(
                provider.chat([{"role": "user", "content": PING}], model=model), timeout
            )
            text = (answer or "").strip()
            return {"model": model, "ok": bool(text), "ms": int((time.monotonic() - started) * 1000),
                    "reply": text[:60], "error": "" if text else "empty answer"}
        except Exception as e:
            return {"model": model, "ok": False, "ms": int((time.monotonic() - started) * 1000),
                    "reply": "", "error": f"{type(e).__name__}: {e}"[:90]}


async def run(models, provider, timeout: float, concurrency: int):
    sem = asyncio.Semaphore(concurrency)
    results = []
    tasks = [asyncio.create_task(ask(provider, m, timeout, sem)) for m in models]
    done = 0
    for future in asyncio.as_completed(tasks):
        result = await future
        results.append(result)
        done += 1
        flag = "✅" if result["ok"] else "❌"
        print(f"[{done:>3}/{len(models)}] {flag} {result['model']:<34} "
              f"{result['ms']:>5}ms  {result['reply'] or result['error']}", flush=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe which models answer.")
    parser.add_argument("--all", action="store_true", help="every discovered g4f model")
    parser.add_argument("--limit", type=int, default=0, help="probe only the first N")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--json", type=Path, help="also write results as JSON")
    args = parser.parse_args()

    config = load_config(str(ROOT))
    agent = Agent(config=config)
    provider = agent.providers.get(config.provider) or G4fProvider()

    models = G4fProvider.discover_models() if args.all else list(G4fProvider.models)
    if args.limit:
        models = models[:args.limit]

    print(f"probing {len(models)} models through '{config.provider}' "
          f"(concurrency {args.concurrency}, timeout {args.timeout:.0f}s)\n", flush=True)
    results = asyncio.run(run(models, provider, args.timeout, args.concurrency))

    working = [r for r in results if r["ok"]]
    print(f"\n{len(working)} of {len(results)} answered:")
    print("  " + ", ".join(sorted(r["model"] for r in working)))
    broken = sorted({r["error"].split(":")[0] for r in results if not r["ok"]})
    if broken:
        print("  failures: " + ", ".join(broken))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  wrote {args.json}")


if __name__ == "__main__":
    main()
