"""W4: refuse an oversized dataset load instead of letting the sandbox OOM.

The pure decisions are unit-tested; the last test drives the REAL `run_bash`
tool against a real file on disk, because the part that actually matters — that
the command never runs — lives in the tool, not in the helper.

Run: uv run python tests/test_dataset_guard.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.sandbox import dataset_guard as dg  # noqa: E402

_WS = tempfile.mkdtemp(prefix="dsguard-")


def test_paths_are_taken_from_the_code_only() -> None:
    """No directory scan, no guessing — only files the code names."""
    assert dg.data_paths("pd.read_csv('data/sales.csv')") == ["data/sales.csv"]
    assert dg.data_paths('pd.read_parquet("a/b.parquet")') == ["a/b.parquet"]
    # several, deduped, in order
    code = "pd.read_csv('x.csv'); pd.read_csv('y.tsv'); pd.read_csv('x.csv')"
    assert dg.data_paths(code) == ["x.csv", "y.tsv"]
    # unquoted mentions and non-data files are not candidates
    assert dg.data_paths("cat report.md; ls data/") == []
    assert dg.data_paths("open('notes.txt')") == []
    print("OK paths_are_taken_from_the_code_only")


def test_sampling_intent_disables_the_guard() -> None:
    """A guard that fires on careful code is a guard people turn off."""
    for code in (
        "pd.read_csv('big.csv', nrows=1000)",
        "pd.read_csv('big.csv', chunksize=500_000)",
        "pd.read_csv('big.csv', usecols=['a','b'])",
        "pd.read_parquet('big.parquet', columns=['a'])",
    ):
        assert dg.already_samples(code), code
    assert not dg.already_samples("pd.read_csv('big.csv')")
    # …but only READ-TIME limits count. `.head()` after the read has already
    # loaded the whole file — that is the case the guard exists for.
    assert not dg.already_samples("df = pd.read_csv('big.csv').head(50)")
    assert not dg.already_samples("pd.read_csv('big.csv').sample(100)")
    print("OK sampling_intent_disables_the_guard")


def test_refusal_names_the_file_and_a_way_out() -> None:
    msg = dg.refusal([("data/big.csv", 300 * 1024 * 1024)], cap=100 * 1024 * 1024)
    assert "data/big.csv" in msg and "300.0MB" in msg, msg
    # a refusal with no route out just gets retried verbatim
    for route in ("nrows=", "usecols=", "chunksize=", "parquet"):
        assert route in msg, route
    assert "ADK_CC_DATASET_MAX_MB" in msg, "must say how to raise the limit"
    print("OK refusal_names_the_file_and_a_way_out")


def test_probe_and_parse_roundtrip() -> None:
    cmd = dg.size_probe(["a b.csv", "plain.csv"])
    assert "'a b.csv'" in cmd and "wc -c" in cmd, cmd
    sizes = dg.parse_sizes("123 a b.csv\n456 plain.csv\ngarbage\n")
    assert sizes == {"a b.csv": 123, "plain.csv": 456}, sizes
    print("OK probe_and_parse_roundtrip")


# --- the part that matters: the real tool -----------------------------------

class _Ctx:
    agent_name = "coordinator"

    def __init__(self):
        self.state = {}


class _Backend:
    """Runs commands for real via /bin/sh, in the test workspace."""

    def __init__(self):
        self.ran: list[str] = []

    async def exec(self, command, fs_write=None, network=None, timeout_s=None, cwd=None):
        self.ran.append(command)
        proc = await asyncio.create_subprocess_shell(
            command, cwd=cwd or _WS,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()

        class _R:
            stdout = out.decode()
            stderr = err.decode()
            exit_code = proc.returncode
            timed_out = False
        return _R()


class _Ws:
    abs_path = _WS

    def fs_write_config(self):
        return None

    def fs_read_config(self):
        return None


def test_real_tool_refuses_without_running_it() -> None:
    """End to end through RunBashTool: an oversized load never executes."""
    import adk_cc.sandbox as sandbox
    import adk_cc.tools.bash.tool as bashmod
    from adk_cc.tools.schemas import RunBashArgs

    backend = _Backend()
    sandbox.get_backend = lambda ctx: backend
    sandbox.get_workspace = lambda ctx: _Ws()
    bashmod.get_backend = lambda ctx: backend
    bashmod.get_workspace = lambda ctx: _Ws()

    big = Path(_WS) / "big.csv"
    with big.open("wb") as f:
        f.write(b"a,b\n" + b"1,2\n" * 400_000)     # ~1.6MB
    small = Path(_WS) / "small.csv"
    small.write_bytes(b"a,b\n1,2\n")

    os.environ["ADK_CC_DATASET_MAX_MB"] = "1"
    tool = bashmod.BashTool()
    try:
        res = asyncio.run(tool._execute(
            RunBashArgs(command="python3 -c \"import pandas as pd; "
                                "df = pd.read_csv('big.csv'); print(df.shape)\""),
            _Ctx()))
        assert res.get("error_code") == "DATASET_TOO_LARGE", res
        assert "big.csv" in res["stderr"] and "nrows=" in res["stderr"], res["stderr"]
        # the python command itself must NEVER have run
        assert not any("read_csv" in c for c in backend.ran), backend.ran
        print("OK real_tool_refuses_without_running_it")

        # …and a small file is untouched by the guard
        backend.ran.clear()
        res2 = asyncio.run(tool._execute(
            RunBashArgs(command="python3 -c \"print(open('small.csv').read())\""),
            _Ctx()))
        assert res2.get("error_code") != "DATASET_TOO_LARGE", res2
        assert any("small.csv" in c for c in backend.ran), backend.ran
        print("OK small_file_runs_normally")

        # …and a non-python command is never probed at all
        backend.ran.clear()
        asyncio.run(tool._execute(RunBashArgs(command="cp big.csv copy.csv"), _Ctx()))
        assert not any("wc -c" in c for c in backend.ran), \
            f"guard probed a non-python command: {backend.ran}"
        print("OK non_python_command_costs_nothing")
    finally:
        os.environ.pop("ADK_CC_DATASET_MAX_MB", None)


def main() -> None:
    test_paths_are_taken_from_the_code_only()
    test_sampling_intent_disables_the_guard()
    test_refusal_names_the_file_and_a_way_out()
    test_probe_and_parse_roundtrip()
    test_real_tool_refuses_without_running_it()
    print("\nall dataset-guard tests passed")


if __name__ == "__main__":
    main()
