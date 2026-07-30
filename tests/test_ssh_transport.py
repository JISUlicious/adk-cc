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


def test_password_auth_turns_prompting_on() -> None:
    """A configured password is the one case that needs BatchMode OFF.

    Reported: connecting a remote from desktop mode needs password auth and a
    non-22 port. Port already worked end-to-end server-side; passwords could not
    work at all, because every invocation passed `BatchMode=yes` and ssh will
    not read a password in batch mode."""
    t = _transport(port=2299, password="s3cret")
    argv = t.build_argv(["/bin/sh", "-s"])
    joined = " ".join(argv)
    assert "BatchMode=no" in joined and "BatchMode=yes" not in joined
    assert "PreferredAuthentications=password,keyboard-interactive" in joined
    assert "PubkeyAuthentication=no" in joined      # else keys are tried first
    assert "NumberOfPasswordPrompts=1" in joined    # fail fast on a wrong one
    assert "-p" in argv and "2299" in argv
    print("OK password_auth_turns_prompting_on")


def test_the_password_is_never_on_argv() -> None:
    """`sshpass -p` would put it there, where `ps` shows it to every local
    user. The whole reason for the askpass helper."""
    t = _transport(password="s3cret")
    argv = t.build_argv(["/bin/sh", "-s"])
    assert not any("s3cret" in a for a in argv), argv
    assert "sshpass" not in " ".join(argv)
    print("OK the_password_is_never_on_argv")


def test_askpass_helper_is_private_and_prints_the_env_var() -> None:
    import os
    import stat
    import subprocess as sp

    t = _transport(password="s3cret")
    env = t._spawn_env()
    assert env is not None
    assert env["ADK_CC_SSH_PASSWORD"] == "s3cret"
    assert env["SSH_ASKPASS_REQUIRE"] == "force"
    helper = env["SSH_ASKPASS"]
    mode = stat.S_IMODE(os.stat(helper).st_mode)
    assert mode == 0o700, oct(mode)          # readable only by this uid
    # It must actually emit the password ssh asks for.
    out = sp.run([helper], capture_output=True, text=True,
                 env={"ADK_CC_SSH_PASSWORD": "s3cret"})
    assert out.stdout == "s3cret", (out.stdout, out.stderr)
    print("OK askpass_helper_is_private_and_prints_the_env_var")


def test_the_key_path_is_untouched_without_a_password() -> None:
    """No password configured → byte-for-byte the old behaviour, and no
    inherited env override that could change auth for key-based hosts."""
    t = _transport(port=22)
    joined = " ".join(t.build_argv([]))
    assert "BatchMode=yes" in joined
    assert "PreferredAuthentications" not in joined
    assert "PubkeyAuthentication" not in joined
    assert t._spawn_env() is None
    print("OK the_key_path_is_untouched_without_a_password")


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


def test_registry_separates_passwords_without_storing_them() -> None:
    """A changed password must yield a new transport (and a new ControlMaster),
    but the registry key holds a digest — this dict is long-lived process state
    and a plaintext secret in a key surfaces in reprs and crash dumps."""
    # Distinctive values: "one" would have matched the "None" in the repr of a
    # password-less key and failed this test for the wrong reason.
    a = get_transport("hostP", password="pw-alpha")
    b = get_transport("hostP", password="pw-alpha")
    c = get_transport("hostP", password="pw-beta")
    d = get_transport("hostP")
    assert a is b and a is not c and a is not d
    from adk_cc.sandbox.ssh_transport import _REGISTRY

    flat = repr(list(_REGISTRY.keys()))
    assert "pw-alpha" not in flat and "pw-beta" not in flat, flat
    print("OK registry_separates_passwords_without_storing_them")


def test_long_control_dir_falls_back_to_short_socket_path():
    """Unix sockets cap the path at ~104 bytes and %C adds 40 chars — a deep
    control dir (macOS $TMPDIR!) must be swapped for a short deterministic
    fallback, or every ssh op dies with 'ControlPath too long' (live-e2e
    reproduced). Short dirs are kept verbatim."""
    deep = "/var/folders/jk/ynxxgwhn2jjdzb_s20mfxl3m0000gn/T/adk-ssh-e2e-x/ctl"
    t = SshTransport("h", control_dir=deep)
    assert t._control_dir != deep
    assert len(t._control_dir) + 1 + 40 <= 104, t._control_dir
    # Deterministic per configured path (isolation preserved across instances).
    assert t._control_dir == SshTransport("h", control_dir=deep)._control_dir
    assert t._control_dir != SshTransport("h", control_dir=deep + "2")._control_dir
    # NB: macOS mkdtemp paths are themselves too long — use a truly short one.
    short = f"/tmp/ctl-{os.getpid()}"
    assert SshTransport("h", control_dir=short)._control_dir == short
    print("OK long_control_dir_falls_back_to_short_socket_path")


def test_control_dir_created_private():
    # Short path (a long one triggers the socket-length fallback by design);
    # assert on the transport's ACTUAL control dir.
    sub = f"/tmp/adk-ssh-perm-{os.getpid()}"
    try:
        t = SshTransport("h", control_dir=sub)
        assert t._control_dir == sub
        assert os.path.isdir(sub)
        if os.name == "posix":
            assert (os.stat(sub).st_mode & 0o777) == 0o700, oct(os.stat(sub).st_mode)
    finally:
        import shutil

        shutil.rmtree(sub, ignore_errors=True)
    print("OK control_dir_created_private")


def main():
    test_build_script_exports_cd_and_command()
    test_build_script_quotes_hostile_values()
    test_build_script_skips_invalid_env_names()
    test_secret_values_never_on_argv()
    test_build_argv_shape()
    test_password_auth_turns_prompting_on()
    test_the_password_is_never_on_argv()
    test_askpass_helper_is_private_and_prints_the_env_var()
    test_the_key_path_is_untouched_without_a_password()
    test_registry_separates_passwords_without_storing_them()
    test_transport_error_classifier()
    test_registry_reuses_by_key()
    test_long_control_dir_falls_back_to_short_socket_path()
    test_control_dir_created_private()
    print("\nall ssh-transport unit tests passed")


if __name__ == "__main__":
    main()
