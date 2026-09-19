---
name: sql-optimizer
description: Diagnose and fix slow SQL queries
when_to_use: Slow queries, index design, schema reviews
---

# SQL Optimizer Skill

## Diagnose
1. Get the plan: `EXPLAIN (ANALYZE, BUFFERS)` (Postgres) / `EXPLAIN ANALYZE` (MySQL).
2. Read it bottom-up: where do the actual rows balloon?
3. The classic culprits, in order of frequency:
   - Seq Scan on a filtered table that lacks an index on the filter column.
   - N+1 from the application (query in a loop) — batch with IN / JOIN instead.
   - SELECT * dragging wide rows; join before filtering.
   - Function on the indexed column (`WHERE lower(email)=...`) disabling the index.
   - Off-by-one pagination with OFFSET deep pages — keyset pagination instead.

## Indexing rules
- Index the columns of equality predicates first, range/sort second:
  `CREATE INDEX ON orders (customer_id, created_at DESC)`.
- One composite index beats three single-column ones for a hot query.
- Every foreign key used in joins should be indexed.
- Measure writes cost: each index slows INSERT/UPDATE. Drop unused ones
  (check pg_stat_user_indexes).

## Verify
After the fix: `EXPLAIN ANALYZE` again, compare timings, keep the query plans
in the report. An index without a measured before/after is a guess.

## Report format
Query → before plan + time → change (index/rewrite) → after plan + time → speedup.
