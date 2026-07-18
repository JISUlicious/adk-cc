"""E2E: SshTransport against a REAL sshd (throwaway Docker container).

Exercises the actual transport over real ssh (key auth, loopback):
  - run(): stdout/stderr/exit codes; env delivered via the stdin script;
    cwd honored; missing cwd → exit 96
  - run_stream(): chunks arrive live, terminal result chunk matches
  - write_file()/read_file(): binary round trip (all 256 byte values),
    parent auto-mkdir, FileNotFoundError on a missing path
  - timeout: client-side kill → timed_out=True, bounded wall clock
  - multiplexing: ControlMaster alive after first op; later ops fast
  - reconnect: close() drops the master; next op transparently re-opens
  - transport-error mapping: unreachable port → SshConnectionError

Container boot lives in `sshd_harness.py` (shared with the backend/panel/
checkpoint e2es). Benign commands only. Skips gracefully (exit 0) when
Docker or the image is unavailable.

Run: `uv run python tests/e2e_ssh_transport.py`
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

sys.path.insert(0, os.path.dirname(__file__))
from sshd_harness import SshdContainer, wait_ready  # noqa: E402


def main() -> int:
    failures: list[str] = []
    with SshdContainer() as box:
        if box is None:
            return 0  # SKIP already printed

        from adk_cc.sandbox.ssh_transport import SshConnectionError, SshTransport

        t = SshTransport(
            box.host,
            port=box.port,
            identity_file=box.identity_file,
            extra_ssh_opts=box.extra_ssh_opts,
            control_dir=box.control_dir,
        )

        async def drive() -> None:
            err = await wait_ready(t)
            if err:
                failures.append(f"sshd never became ready: {err}")
                return
            print("  [PASS] sshd up; key-auth run() round trip")

            res = await t.run("exit 7")
            if res.exit_code != 7:
                failures.append(f"exit code: expected 7, got {res.exit_code}")
            else:
                print("  [PASS] remote exit codes propagate")

            secret = "s3cr3t-via-stdin"
            res = await t.run('printf "%s" "$MY_TOKEN"', env={"MY_TOKEN": secret})
            if res.stdout != secret:
                failures.append(f"env not delivered: {res.stdout!r} / {res.stderr!r}")
            else:
                print("  [PASS] env delivered via stdin script")

            probe = await t.probe()
            home = probe["home"]
            await t.run(f"mkdir -p {home}/wsp/sub")
            res = await t.run("pwd", cwd=f"{home}/wsp")
            if res.exit_code == 0 and res.stdout.strip().endswith("/wsp"):
                print("  [PASS] cwd honored")
            else:
                failures.append(f"cwd: exit={res.exit_code} out={res.stdout!r} err={res.stderr!r}")

            res = await t.run("true", cwd="/definitely/not/here")
            if res.exit_code != 96:
                failures.append(f"missing cwd: expected exit 96, got {res.exit_code}")
            else:
                print("  [PASS] missing cwd → exit 96 sentinel")

            # -- streaming ------------------------------------------------
            chunks: list = []
            async for c in t.run_stream(
                'echo one; sleep 1; echo two; echo err >&2', timeout_s=30
            ):
                chunks.append(c)
            kinds = [c.kind for c in chunks]
            result = chunks[-1].result if chunks and chunks[-1].kind == "result" else None
            if (
                result is None
                or result.exit_code != 0
                or "one" not in result.stdout
                or "two" not in result.stdout
                or "err" not in result.stderr
                or kinds.count("result") != 1
                or len([k for k in kinds if k == "stdout"]) < 2  # arrived as ≥2 live chunks
            ):
                failures.append(f"run_stream: kinds={kinds} result={result}")
            else:
                print(f"  [PASS] run_stream live chunks ({len(chunks) - 1} + result)")

            # -- binary file round trip ----------------------------------
            blob = bytes(range(256)) * 4
            path = f"{home}/e2e/sub/blob.bin"
            await t.write_file(path, blob)
            back = await t.read_file(path)
            if back != blob:
                failures.append(f"binary round trip: {len(back)} bytes != {len(blob)}")
            else:
                print("  [PASS] binary write/read round trip (+auto mkdir)")

            try:
                await t.read_file(f"{home}/e2e/missing.bin")
                failures.append("missing read did not raise FileNotFoundError")
            except FileNotFoundError:
                print("  [PASS] missing file → FileNotFoundError")

            if not probe["home"] or probe["uname"] != "Linux":
                failures.append(f"probe unexpected: {probe}")
            else:
                print(f"  [PASS] probe: home={probe['home']} git={probe['git']} uname={probe['uname']}")

            t0 = time.monotonic()
            res = await t.run("sleep 30", timeout_s=2)
            wall = time.monotonic() - t0
            if not res.timed_out or wall > 10:
                failures.append(f"timeout: timed_out={res.timed_out} wall={wall:.1f}s")
            else:
                print(f"  [PASS] client-side timeout kill ({wall:.1f}s)")

            if not t.is_connected():
                failures.append("ControlMaster not alive after ops")
            else:
                t0 = time.monotonic()
                await t.run("echo fast")
                dt = time.monotonic() - t0
                print(f"  [PASS] master alive; multiplexed op in {dt * 1000:.0f}ms")

            t.close()
            res = await t.run("echo back")
            if res.exit_code != 0 or "back" not in res.stdout:
                failures.append(f"reconnect after close failed: {res.stderr[:120]}")
            else:
                print("  [PASS] close() then next op reconnects")

            dead = SshTransport(
                box.host,
                port=box.port + 1,  # nothing listens here
                identity_file=box.identity_file,
                extra_ssh_opts=box.extra_ssh_opts,
                control_dir=box.control_dir + "2",
                connect_timeout_s=5,
            )
            try:
                await dead.run("echo hi", timeout_s=15)
                failures.append("unreachable host did not raise SshConnectionError")
            except SshConnectionError as e:
                print(f"  [PASS] unreachable → SshConnectionError ({str(e)[:60]}…)")

        asyncio.run(drive())

    if failures:
        print("\nFAIL — ssh transport e2e:")
        for m in failures:
            print(f"  [FAIL] {m}")
        return 1
    print("\nssh transport e2e: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
