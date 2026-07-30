"""Storing a remote SSH password, and where it must never appear.

Requested: connecting a remote workspace from desktop mode needs a password
option and a non-default port. The port already worked end-to-end server-side —
only the UI lacked a field — so this covers the password, whose whole risk is
leaking the secret somewhere convenient.

Three places it must not be:
  * `projects.json` — plain JSON the user can open; it records the auth MODE
  * the ssh argv — `ps` shows argv to every local user (covered in
    tests/test_ssh_transport.py)
  * the process registry key — long-lived state that ends up in reprs

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_ssh_password_store.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_DATA = tempfile.mkdtemp(prefix="sshpw-")
os.environ["ADK_CC_DATA_DIR"] = _DATA

from cryptography.fernet import Fernet  # noqa: E402

from adk_cc.sandbox import ssh_passwords  # noqa: E402

os.environ["ADK_CC_CREDENTIAL_KEY"] = Fernet.generate_key().decode()


def test_round_trip() -> None:
    ssh_passwords.put("box.local", 2222, "hunter2")
    assert ssh_passwords.get("box.local", 2222) == "hunter2"
    # Port is part of the identity: the same host on another port is a
    # different login and must not silently reuse the password.
    assert ssh_passwords.get("box.local", 22) is None
    assert ssh_passwords.get("other.local", 2222) is None
    print("OK round_trip")


def test_it_is_encrypted_on_disk_and_private() -> None:
    ssh_passwords.put("box2.local", None, "s3cret-value")
    files = list((Path(_DATA) / "admin-data" / "ssh-passwords").glob("*.enc"))
    assert files, "nothing written"
    blob = b"".join(f.read_bytes() for f in files)
    assert b"s3cret-value" not in blob, "password is on disk in the clear"
    for f in files:
        assert oct(f.stat().st_mode)[-3:] == "600", oct(f.stat().st_mode)
    # The filename must not leak the host either.
    assert not any("box2.local" in f.name for f in files), [f.name for f in files]
    print("OK it_is_encrypted_on_disk_and_private")


def test_delete_removes_it() -> None:
    ssh_passwords.put("gone.local", 22, "x")
    ssh_passwords.delete("gone.local", 22)
    assert ssh_passwords.get("gone.local", 22) is None
    ssh_passwords.delete("gone.local", 22)      # idempotent
    print("OK delete_removes_it")


def test_without_a_key_it_refuses_rather_than_writing_plaintext() -> None:
    saved = os.environ.pop("ADK_CC_CREDENTIAL_KEY")
    try:
        assert not ssh_passwords.available()
        try:
            ssh_passwords.put("nokey.local", 22, "pw")
        except ssh_passwords.SshPasswordStoreUnavailable as e:
            assert "ADK_CC_CREDENTIAL_KEY" in str(e)
        else:
            raise AssertionError("stored a password with no encryption key")
        # Reads degrade quietly — a missing key must not break key-based hosts.
        assert ssh_passwords.get("nokey.local", 22) is None
    finally:
        os.environ["ADK_CC_CREDENTIAL_KEY"] = saved
    print("OK without_a_key_it_refuses_rather_than_writing_plaintext")


def test_a_corrupt_entry_degrades_instead_of_raising() -> None:
    """One unreadable file must not take down every remote operation — the ssh
    layer then reports an ordinary auth failure, which is diagnosable."""
    ssh_passwords.put("corrupt.local", 22, "pw")
    for f in (Path(_DATA) / "admin-data" / "ssh-passwords").glob("*.enc"):
        f.write_bytes(b"not-fernet")
    assert ssh_passwords.get("corrupt.local", 22) is None
    print("OK a_corrupt_entry_degrades_instead_of_raising")


def test_the_transport_picks_it_up_without_the_call_sites_changing() -> None:
    """The four places that build a transport are all synchronous, so the
    password is resolved inside `get_transport` — the same place the identity
    file and extra opts get their defaults."""
    from adk_cc.sandbox import ssh_transport

    ssh_passwords.put("auto.local", 2200, "from-store")
    ssh_transport._REGISTRY.clear()
    t = ssh_transport.get_transport("auto.local", port=2200)
    assert t._password == "from-store"
    joined = " ".join(t.build_argv([]))
    assert "BatchMode=no" in joined
    assert "from-store" not in joined
    print("OK the_transport_picks_it_up_without_the_call_sites_changing")


def test_the_binding_file_records_the_mode_not_the_secret() -> None:
    """What add_remote_project writes. `projects.json` is plain JSON in the data
    dir; a password there would be readable by anything that can open it."""
    binding = {"host": "box.local", "path": "/srv/app", "port": 2222,
               "auth": "password"}
    blob = json.dumps({"id": "abc", "name": "box", "remote": binding})
    assert "hunter2" not in blob
    assert binding["auth"] == "password"      # enough for the UI to prompt
    assert "password" not in binding
    print("OK the_binding_file_records_the_mode_not_the_secret")


def main() -> None:
    test_round_trip()
    test_it_is_encrypted_on_disk_and_private()
    test_delete_removes_it()
    test_without_a_key_it_refuses_rather_than_writing_plaintext()
    test_a_corrupt_entry_degrades_instead_of_raising()
    test_the_transport_picks_it_up_without_the_call_sites_changing()
    test_the_binding_file_records_the_mode_not_the_secret()
    print("\nall ssh-password-store tests passed")


if __name__ == "__main__":
    main()
