"""Unit tests for `ssh_transport` — pure construction/classification logic.

No SSH connections here: these pin the ARGV/SCRIPT assembly (the security
surface) and the transport-error classifier. The live behavior (real sshd,
round trips, timeouts, reconnect) is `tests/e2e_ssh_transport.py`.

The load-bearing assertion: secret env VALUES appear only in the stdin
script, never in the ssh argv (argv is visible to `ps` on both machines).

Run: `uv run python tests/test_ssh_transport.py`
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.sandbox.ssh_transport import (  # noqa: E402
    SshTransport,
    build_script,
    get_transport,
    looks_like_transport_error,
)


def _transport(**kw) -> SshTransport:
    # Isolated control dir so tests never touch ~/.adk-cc-ssh.
    kw.setdefault("control_dir", tempfile.mkdtemp(prefix="adk-ssh-test-"))
    return SshTransport("testhost", **kw)


def test_build_script_exports_cd_and_command():
    s = build_script("echo hi", cwd="/work/dir", env={"B": "2", "A": "1"})
    lines = s.splitlines()
    # Exports first (sorted), then cd (exit 96 sentinel), then the command.
    # shlex.quote leaves already-safe words unquoted, so assert semantics.
    assert lines[0] == "export A=1", lines
    assert lines[1] == "export B=2", lines
    assert lines[2] == "cd /work/dir || exit 96", lines
    assert lines[3] == "echo hi", lines
    print("OK build_script_exports_cd_and_command")


def test_build_script_quotes_hostile_values():
    """Values with quotes/spaces/metachars survive as single sh words."""
    v = "pa'ss; rm -rf $HOME `boom`"
    s = build_script("true", env={"SECRET": v})
    # shlex.quote splits embedded single quotes into '"'"' — the value must
    # NOT appear raw (unquoted) in the script.
    assert "export SECRET=" in s
    assert "rm -rf $HOME" in s  # inside quotes, inert
    assert s.count("\n") == 2  # export + command, nothing injected
    import shlex

    # Round-trip: the exported word parses back to exactly the value.
    export_line = s.splitlines()[0]
    parsed = shlex.split(export_line.removeprefix("export "))
    assert parsed == [f"SECRET={v}"], parsed
    print("OK build_script_quotes_hostile_values")


def test_build_script_skips_invalid_env_names():
    s = build_script("true", env={"OK_NAME": "x", "bad-name": "y", "1BAD": "z"})
    assert "OK_NAME" in s
    assert "bad-name" not in s and "1BAD" not in s
    print("OK build_script_skips_invalid_env_names")


def test_secret_values_never_on_argv():
    """The security invariant: env values ride stdin, argv stays clean."""
    t = _transport()
    secret = "sk-live-VERY-SECRET-VALUE"
    script = build_script("deploy", env={"API_KEY": secret})
    argv = t.build_argv(["/bin/sh", "-s"])
    assert secret in script  # delivered via stdin
    assert all(secret not in a for a in argv), argv  # invisible to ps
    assert "API_KEY" not in " ".join(argv)  # not even the name
    print("OK secret_values_never_on_argv")


def test_build_argv_shape():
    t = _transport(port=2299, identity_file="/tmp/k", extra_ssh_opts=("-o", "X=1"))
    argv = t.build_argv(["/bin/sh", "-s"])
    joined = " ".join(argv)
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in joined  # never prompts
    assert "ControlMaster=auto" in joined and "/%C" in joined  # multiplexed
    assert f"ControlPersist=" in joined
    assert "-p" in argv and "2299" in argv
    assert "-i" in argv and "/tmp/k" in argv
    assert "X=1" in joined  # extra opts pass through
    # host then remote command, in that order, at the end
    assert argv[-3:] == ["testhost", "/bin/sh", "-s"], argv[-3:]
    print("OK build_argv_shape")


def test_transport_error_classifier():
    assert looks_like_transport_error(255, "ssh: connect to host x port 22: Connection refused")
    assert looks_like_transport_error(255, "Host key verification failed.")
    assert looks_like_transport_error(255, "x@y: Permission denied (publickey).")
    # Exit 255 with non-transport stderr → the REMOTE command's own doing.
    assert not looks_like_transport_error(255, "my tool intentionally exited 255")
    # Transport-looking stderr with a normal exit code → remote command noise.
    assert not looks_like_transport_error(1, "ssh: something")
    print("OK transport_error_classifier")


def test_registry_reuses_by_key():
    a = get_transport("hostA", port=22)
    b = get_transport("hostA", port=22)
    c = get_transport("hostA", port=2222)
    assert a is b
    assert a is not c
    print("OK registry_reuses_by_key")


def test_control_dir_created_private():
    d = tempfile.mkdtemp(prefix="adk-ssh-perm-")
    sub = os.path.join(d, "ctl")
    SshTransport("h", control_dir=sub)
    assert os.path.isdir(sub)
    if os.name == "posix":
        assert (os.stat(sub).st_mode & 0o777) == 0o700, oct(os.stat(sub).st_mode)
    print("OK control_dir_created_private")


def main():
    test_build_script_exports_cd_and_command()
    test_build_script_quotes_hostile_values()
    test_build_script_skips_invalid_env_names()
    test_secret_values_never_on_argv()
    test_build_argv_shape()
    test_transport_error_classifier()
    test_registry_reuses_by_key()
    test_control_dir_created_private()
    print("\nall ssh-transport unit tests passed")


if __name__ == "__main__":
    main()
