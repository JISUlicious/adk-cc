"""Password auth against a real sshd that accepts ONLY passwords.

`e2e_ssh_askpass.py` proves OpenSSH reads our helper (no Docker needed). This
proves the rest: that a password-only host authenticates, runs commands, and
that a WRONG password fails promptly rather than hanging on repeated prompts.

The container is started with PASSWORD_ACCESS and NO authorized key, so a pass
here cannot come from key auth quietly working instead.

Run: .venv/bin/python tests/e2e_ssh_password.py      (needs Docker)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "tests"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_passed = _failed = 0
_PASSWORD = "adk-cc-test-pw"


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    from sshd_harness import SshdContainer, wait_ready

    from adk_cc.sandbox.ssh_transport import SshConnectionError, SshTransport

    with SshdContainer(password=_PASSWORD) as box:
        if box is None:
            return 0                      # harness printed the skip reason

        def transport(pw: str | None) -> SshTransport:
            return SshTransport(
                box.host, port=box.port, password=pw,
                extra_ssh_opts=box.extra_ssh_opts,
                control_dir=os.path.join(box.tmp, f"ctl-{pw or 'none'}"),
            )

        t = transport(_PASSWORD)
        banner = asyncio.run(wait_ready(t))
        check("a password-only host authenticates", banner is not None,
              "never became ready — password auth did not get in")
        if banner is None:
            return 1

        res = asyncio.run(t.run("echo hello-from-remote"))
        check("commands run over the password connection",
              res.exit_code == 0 and "hello-from-remote" in res.stdout,
              f"rc={res.exit_code} out={res.stdout[:80]!r}")

        # The port is non-default here (the harness maps one), so this run also
        # covers the custom-port half of the request.
        check("the custom port was used", box.port != 22, f"port={box.port}")

        # A wrong password must fail FAST. NumberOfPasswordPrompts=1 is what
        # stops ssh re-answering with the same wrong value; without it this
        # hangs, which is how it would present to a user with a stale password.
        started = time.time()
        try:
            bad = asyncio.run(transport("wrong-password").run("echo nope",
                                                             timeout_s=30))
            failed = bad.exit_code != 0
        except SshConnectionError:
            failed = True
        elapsed = time.time() - started
        check("a wrong password fails instead of hanging",
              failed and elapsed < 25, f"failed={failed} after {elapsed:.1f}s")

        # And key auth must still be refused on this box — otherwise the checks
        # above could have been satisfied by something other than the password.
        try:
            none = asyncio.run(transport(None).run("echo nope", timeout_s=20))
            keyless_ok = none.exit_code == 0
        except SshConnectionError:
            keyless_ok = False
        check("the box really is password-only", not keyless_ok,
              "connected with no password — the test proves nothing about "
              "password auth")

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
