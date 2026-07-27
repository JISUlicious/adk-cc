# Judging a test suite

A count is not a signal. Read a sample and ask what would have to break for a
test to fail.

## Failure modes to look for

- **Mocked-everything** — the test exercises the mock, not the system. Common in
  "integration" tests that never touch a real dependency.
- **Assertion-free** — calls code, asserts nothing (or only `assert True` /
  `assert not None`). Passes forever.
- **Circular** — asserts the implementation against itself (recomputes the
  expected value with the same function under test).
- **Skipped/disabled** — `@skip`, `xfail`, `it.skip`, commented-out suites.
  Count them; a large skipped set usually means the suite stopped being trusted.
- **Happy-path only** — no error, boundary, or empty-input cases.
- **Non-deterministic** — time, randomness, network, or ordering dependence;
  look for retries and `sleep` as the tell.

## Evidence to gather

```bash
# does it actually run from a clean checkout?
<the project's own documented test command>
```
Record: pass/fail, runtime, count, skipped count, and whether setup required
undocumented steps. **A suite you could not run is a High finding** — say so
rather than omitting it.
