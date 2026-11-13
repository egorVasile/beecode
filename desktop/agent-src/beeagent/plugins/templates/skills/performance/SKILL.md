---
name: performance
description: Profile-first performance optimization
when_to_use: Code or service is slow; user asks to speed things up
---

# Performance Skill

## Law: measure before touching anything
"Slow" without a measurement is a hypothesis, not a fact.

1. **Reproduce** the slowness with a realistic workload (data size, concurrency).
2. **Profile**: find where time actually goes — cProfile/pyinstrument for CPU,
   EXPLAIN for SQL, chrome devtools for frontend, `time` for the whole pipeline.
3. **Rank**: the top item is usually 10x the rest. Optimize THAT one only.

## Optimization ladder (cheapest first)
1. **Do less**: skip work — cache, memoize, precompute, lazy-load, avoid N+1 queries.
2. **Move work**: batch loops into vectorized/set operations, push filtering into
   the database instead of Python, move inner-loop invariant work outside the loop.
3. **Data structures**: dict/set lookup instead of list scans, generators instead
   of lists for large streams.
4. **Parallelize**: only after 1-3 — async for I/O-bound, processes for CPU-bound.
5. **Rewrite the hot part**: C extension / different algorithm — last resort.

## After every change
- Re-measure with the SAME workload. No measurement, no merge.
- Watch for regressions elsewhere (memory grew? p95 elsewhere worse?).
- Keep the old path behind a flag until the numbers hold in production-ish conditions.

## Report format
Before: X ms (top hotspot: A 60%, B 25%)
Change: what + which ladder step
After: Y ms (Zx faster) — measurements attached
