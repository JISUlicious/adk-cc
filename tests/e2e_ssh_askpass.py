"""Does real OpenSSH actually take a secret from our askpass helper?

The password path hangs entirely on that contract: `BatchMode=no`,
`SSH_ASKPASS=<helper>`, `SSH_ASKPASS_REQUIRE=force`, and the value in the child
env. Unit tests can check the helper prints the variable and that the flags are
on the argv, but not that OpenSSH honours any of it — a wrong flag name or an
ignored `SSH_ASKPASS_REQUIRE` would pass every unit test and fail on the user's
machine.

Proving it against a password-accepting sshd needs Docker (see
`e2e_ssh_password.py`). This gets the same guarantee without a server:
`ssh-add` uses the SAME askpass mechanism to collect a key passphrase. If
`ssh-add` can unlock an encrypted key through our helper, then OpenSSH is
reading it, forcing it without a tty, and getting the exact bytes.

Run: .venv/bin/python tests/e2e_ssh_askpass.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    for tool in ("ssh-keygen", "ssh-add", "ssh-agent"):
        if not shutil.which(tool):
            print(f"SKIP: {tool} not available."); return 0

    from adk_cc.sandbox.ssh_transport import SshTransport

    tmp = tempfile.mkdtemp(prefix="askpass-")
    passphrase = "correct horse battery staple"
    key = os.path.join(tmp, "id_ed25519")
    gen = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", passphrase, "-q", "-f", key],
        capture_output=True, text=True)
    if gen.returncode != 0:
        print(f"SKIP: ssh-keygen failed: {gen.stderr[:200]}"); return 0

    # A throwaway agent, so nothing touches the user's real one.
    agent = subprocess.run(["ssh-agent", "-s"], capture_output=True, text=True)
    sock = re.search(r"SSH_AUTH_SOCK=([^;]+);", agent.stdout)
    pid = re.search(r"SSH_AGENT_PID=(\d+);", agent.stdout)
    if not sock or not pid:
        print("SKIP: could not start a throwaway ssh-agent."); return 0
    agent_env = {"SSH_AUTH_SOCK": sock.group(1), "SSH_AGENT_PID": pid.group(1)}

    try:
        transport = SshTransport("unused.invalid", password=passphrase,
                                 control_dir=os.path.join(tmp, "ctl"))
        env = transport._spawn_env()
        assert env is not None
        base = {**agent_env,
                "SSH_ASKPASS": env["SSH_ASKPASS"],
                "SSH_ASKPASS_REQUIRE": env["SSH_ASKPASS_REQUIRE"],
                "DISPLAY": env.get("DISPLAY", ":0"),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

        # The real thing: OpenSSH must fetch the passphrase from our helper,
        # with no tty in sight.
        ok = subprocess.run(
            ["ssh-add", key],
            env={**base, "ADK_CC_SSH_PASSWORD": env["ADK_CC_SSH_PASSWORD"]},
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        check("real OpenSSH takes the secret from our askpass helper",
              ok.returncode == 0,
              f"ssh-add rc={ok.returncode} stderr={ok.stderr.strip()[:160]!r}")

        # And it is genuinely READING it, not being waved through: a wrong value
        # must not unlock the key. Without this check, a helper that printed
        # nothing at all would have "passed" the first one.
        #
        # `ssh-add` re-invokes askpass indefinitely on a wrong passphrase (it
        # has no prompt limit), so this call HANGS rather than failing — hence
        # the short timeout and why a timeout counts as "not accepted". The
        # product does not inherit that: `ssh` is invoked with
        # NumberOfPasswordPrompts=1, so a wrong stored password fails on the
        # first try instead of looping.
        subprocess.run(["ssh-add", "-D"], env=base, capture_output=True)
        rejected = False
        try:
            bad = subprocess.run(
                ["ssh-add", key],
                env={**base, "ADK_CC_SSH_PASSWORD": "not-the-passphrase"},
                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                timeout=10)
            rejected = bad.returncode != 0
            detail = f"rc={bad.returncode}"
        except subprocess.TimeoutExpired:
            rejected = True          # never unlocked; ssh-add just kept asking
            detail = "ssh-add looped on the wrong value (expected for ssh-add)"
        check("a wrong secret does not unlock the key", rejected,
              "ssh-add ACCEPTED a wrong passphrase — the value OpenSSH used is "
              "not the one our helper printed")
        print(f"    wrong-secret behaviour: {detail}")
        print(f"    helper: {env['SSH_ASKPASS']}")
    finally:
        subprocess.run(["kill", agent_env["SSH_AGENT_PID"]], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
