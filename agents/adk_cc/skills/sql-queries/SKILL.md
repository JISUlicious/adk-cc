---
name: sql-queries
description: >
  Write and validate SQL against the real schema — read the tables and keys
  first, check join cardinality and null semantics, then sanity-check the result
  before reporting it. Use for querying a database, reviewing someone's SQL, or
  turning a metric definition into a query.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["column and table names were read from the actual schema, not assumed", "join fan-out was checked (row count before vs after) rather than trusted", "the query was executed and its output shown, or clearly labelled as unexecuted", "filters state their handling of NULLs and of the time window's boundaries"]}
---

# SQL queries

Most wrong SQL is syntactically perfect. It joins one-to-many and doubles the
revenue, or filters `WHERE status != 'cancelled'` and silently drops every row
where status is NULL. This skill is about the checks that catch those.

## Workflow

### 1. Read the schema — never guess a column
```sql
-- postgres
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns WHERE table_schema='public' ORDER BY 1,2;
-- sqlite
SELECT name, sql FROM sqlite_master WHERE type='table';
```
In a workspace, the schema often lives in the repo — migrations, model
definitions, or a schema dump:
```bash
grep -rln "CREATE TABLE\|class .*\(Base\)\|Schema::create" --include=*.sql --include=*.py .
```
List the tables you will use, their grain (one row per *what*), and the keys
joining them. **Grain is the thing to write down** — nearly every fan-out bug is
a grain misunderstanding.

### 2. Pin the metric definition before writing SQL
"Active users" and "revenue" are not definitions. Which timestamp, which status
values count, is it inclusive of the boundary, which timezone, and are refunds
netted? Ask, or state your interpretation at the top of the answer — a query
answering a different question than intended is worse than no query.

### 3. Write it, with the traps in mind
- **Joins**: know the cardinality of each. If a join can multiply rows, either
  pre-aggregate or use `EXISTS`/`IN`.
- **NULLs**: `!=`, `NOT IN` and most comparisons exclude NULL rows. Use
  `IS DISTINCT FROM` / `COALESCE` deliberately, and say which you chose.
- **Aggregates after LEFT JOIN**: `COUNT(*)` counts the join, `COUNT(t.id)`
  counts real matches. Pick on purpose.
- **Time windows**: half-open `>= start AND < end` avoids both double-counting
  and missing the final microsecond. Name the timezone.
- **Deduplication**: `DISTINCT` hides the fan-out rather than fixing it; prefer
  fixing the grain.

### 4. Validate before reporting
```sql
SELECT COUNT(*) FROM base_table WHERE <filters>;   -- expected magnitude?
-- then the same query with the join added: did the count change? by how much?
SELECT COUNT(*) AS rows, COUNT(DISTINCT <grain_key>) AS entities FROM <result>;
```
`rows != entities` means the result is not at the grain you think it is. Also
run `EXPLAIN` (or `EXPLAIN ANALYZE`) on anything non-trivial — a sequential scan
over a large table is a finding worth reporting.

Sanity checks that catch real errors: does the total match a known figure? do
the period-over-period numbers move plausibly? does the sum of parts equal the
whole? are there suspiciously round numbers or an unexpected NULL group?

### 5. Report with the query
Give the query, the row count, the first rows of output, the metric definition
used, and any caveat (partial final period, excluded statuses, timezone). If you
could not execute it, say so explicitly — an unexecuted query is a draft.

## Reviewing someone else's SQL

Read it against the same list: grain, join cardinality, NULL semantics, window
boundaries, and whether the metric definition matches what was asked. Name the
concrete failing input for each issue you raise ("if a user has two addresses,
this returns them twice"), not a style preference.
