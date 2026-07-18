"""E2E: SshTransport against a REAL sshd (throwaway Docker container).

Boots `lscr.io/linuxserver/openssh-server` on a loopback port with a
generated ed25519 keypair (key auth only — mirrors the product's
no-password policy), then exercises the actual transport:

  - run(): stdout/stderr/exit codes; env delivered via stdin script;
    cwd honored; missing cwd → exit 96
  - write_file()/read_file(): binary round trip (all 256 byte values),
    parent auto-mkdir, FileNotFoundError on a missing path
  - timeout: client-side kill → timed_out=True, bounded wall clock
  - multiplexing: ControlMaster alive after first op; later ops fast
  - reconnect: close() drops the master; next op transparently re-opens
  - transport-error mapping: unreachable port → SshConnectionError

Host-key handling is test-scoped: a throwaway UserKnownHostsFile +
accept-new via `extra_ssh_opts` — production inherits the user's own
known_hosts and never relaxes checking.

Benign commands only (echo/pwd/sleep/cat). Skips gracefully (exit 0)
when Docker or the image is unavailable.

Run: `uv run python tests/e2e_ssh_transport.py`
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_IMAGE = "lscr.io/linuxserver/openssh-server:latest"
_PORT = 42219  # loopback-only host port for the container's sshd (:2222)
_USER = "dev"


def _sh(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kw)


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    return _sh(["docker", "version", "--format", "{{.Server.Version}}"]).returncode == 0


def main() -> int:
    if not _docker_ready():
        print("[SKIP] docker daemon not available — start Docker Desktop and rerun")
        return 0

    tmp = tempfile.mkdtemp(prefix="adk-ssh-e2e-")
    key = os.path.join(tmp, "id_ed25519")
    r = _sh(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", key])
    if r.returncode != 0:
        print(f"FAIL ssh-keygen: {r.stderr}")
        return 1
    pub = open(key + ".pub", encoding="utf-8").read().strip()

    # Pull may take a while on first run; treat network failure as SKIP.
    if _sh(["docker", "image", "inspect", _IMAGE]).returncode != 0:
        print(f"[e2e] pulling {_IMAGE} …")
        if _sh(["docker", "pull", _IMAGE], timeout=300).returncode != 0:
            print("[SKIP] could not pull sshd image (offline?)")
            return 0

    cid = None
    failures: list[str] = []
    try:
        run = _sh(
            [
                "docker", "run", "-d", "--rm",
                "-p", f"127.0.0.1:{_PORT}:2222",
                "-e", f"PUBLIC_KEY={pub}",
                "-e", f"USER_NAME={_USER}",
                _IMAGE,
            ]
        )
        if run.returncode != 0:
            print(f"FAIL docker run: {run.stderr}")
            return 1
        cid = run.stdout.strip()

        from adk_cc.sandbox.ssh_transport import (
            SshConnectionError,
            SshTransport,
        )

        known_hosts = os.path.join(tmp, "known_hosts")
        extra = (
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "StrictHostKeyChecking=accept-new",
        )
        t = SshTransport(
            f"{_USER}@127.0.0.1",
            port=_PORT,
            identity_file=key,
            extra_ssh_opts=extra,
            control_dir=os.path.join(tmp, "ctl"),
        )

        async def drive() -> None:
            # -- wait for sshd (container boot + key install) -------------
            deadline = time.monotonic() + 90
            last = None
            while time.monotonic() < deadline:
                try:
                    res = await t.run("echo ready", timeout_s=10)
                    if res.exit_code == 0 and "ready" in res.stdout:
                        break
                    last = f"exit={res.exit_code} err={res.stderr[:120]}"
                except SshConnectionError as e:
                    last = str(e)[:160]
                await asyncio.sleep(2)
            else:
                failures.append(f"sshd never became ready: {last}")
                return
            print("  [PASS] sshd up; key-auth run() round trip")

            # -- exit codes ----------------------------------------------
            res = await t.run("exit 7")
            if res.exit_code != 7:
                failures.append(f"exit code: expected 7, got {res.exit_code}")
            else:
                print("  [PASS] remote exit codes propagate")

            # -- env via stdin script + cwd ------------------------------
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

            # -- probe ----------------------------------------------------
            if not probe["home"] or probe["uname"] != "Linux":
                failures.append(f"probe unexpected: {probe}")
            else:
                print(f"  [PASS] probe: home={probe['home']} git={probe['git']} uname={probe['uname']}")

            # -- timeout --------------------------------------------------
            t0 = time.monotonic()
            res = await t.run("sleep 30", timeout_s=2)
            wall = time.monotonic() - t0
            if not res.timed_out or wall > 10:
                failures.append(f"timeout: timed_out={res.timed_out} wall={wall:.1f}s")
            else:
                print(f"  [PASS] client-side timeout kill ({wall:.1f}s)")

            # -- multiplexing --------------------------------------------
            if not t.is_connected():
                failures.append("ControlMaster not alive after ops")
            else:
                t0 = time.monotonic()
                await t.run("echo fast")
                dt = time.monotonic() - t0
                print(f"  [PASS] master alive; multiplexed op in {dt * 1000:.0f}ms")

            # -- reconnect after close -----------------------------------
            t.close()
            res = await t.run("echo back")
            if res.exit_code != 0 or "back" not in res.stdout:
                failures.append(f"reconnect after close failed: {res.stderr[:120]}")
            else:
                print("  [PASS] close() then next op reconnects")

            # -- unreachable → SshConnectionError ------------------------
            dead = SshTransport(
                f"{_USER}@127.0.0.1",
                port=_PORT + 1,  # nothing listens here
                identity_file=key,
                extra_ssh_opts=extra,
                control_dir=os.path.join(tmp, "ctl2"),
                connect_timeout_s=5,
            )
            try:
                await dead.run("echo hi", timeout_s=15)
                failures.append("unreachable host did not raise SshConnectionError")
            except SshConnectionError as e:
                print(f"  [PASS] unreachable → SshConnectionError ({str(e)[:60]}…)")

        asyncio.run(drive())
    finally:
        try:
            if cid:
                _sh(["docker", "rm", "-f", cid])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("\nFAIL — ssh transport e2e:")
        for m in failures:
            print(f"  [FAIL] {m}")
        return 1
    print("\nssh transport e2e: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
