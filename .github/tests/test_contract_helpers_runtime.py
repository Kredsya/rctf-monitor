"""Execute shipped helpers on a private tmux server, never the user's server."""

import json
import fcntl
import pty
import select
import struct
import termios
import time
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class HelperRuntimeContracts(unittest.TestCase):
    def test_shipped_scripts_create_workers_and_reattach_without_overwriting_work(self):
        tmux = shutil.which("tmux")
        self.assertIsNotNone(
            tmux, "Install tmux to run the required helper integration gate"
        )
        for name, count in (("22.sh", 4), ("25.sh", 10)):
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory(prefix="ctfd-helper-") as d,
            ):
                root = Path(d)
                problem = root / "Problem name"
                problem.mkdir()
                chall = problem / "chall"
                (chall / "deploy").mkdir(parents=True)
                (chall / "AGENTS.md").write_text("reference instructions")
                (chall / "deploy/data").write_text("provided")
                script = problem / name
                shutil.copy2(REPO / "helpers" / name, script)
                binaries = root / "bin"
                binaries.mkdir()
                wrapper = binaries / "tmux"
                wrapper.write_text(
                    '#!/usr/bin/env python3\nimport os,sys\na=["-f","/dev/null"]+sys.argv[1:]\nif "attach-session" in a: sys.exit(0)\nos.execv('
                    + repr(tmux)
                    + ", ["
                    + repr(tmux)
                    + "]+a)\n"
                )
                wrapper.chmod(0o755)
                env = dict(
                    os.environ,
                    TMUX_TMPDIR=str(root),
                    PATH=str(binaries) + os.pathsep + os.environ["PATH"],
                )
                env.pop("TMUX", None)
                try:
                    subprocess.run(
                        ["bash", "-n", str(script)], check=True, capture_output=True
                    )
                    first = subprocess.run(
                        ["bash", str(script)],
                        cwd="/tmp",
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    self.assertEqual(first.returncode, 0, first.stderr)
                    self.assertEqual(
                        sorted(p.name for p in problem.glob("dist_*")),
                        sorted(f"dist_{i}" for i in range(1, count + 1)),
                    )
                    for i in range(1, count + 1):
                        worker = problem / f"dist_{i}"
                        self.assertEqual(
                            (worker / "AGENTS.md").read_text(), "reference instructions"
                        )
                        self.assertEqual(
                            (worker / "deploy/data").read_text(), "provided"
                        )
                    panes = subprocess.run(
                        [tmux, "-L", "ctf-codex-drag", "list-panes", "-a"],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    self.assertEqual(len(panes.stdout.splitlines()), count)
                    session = subprocess.check_output(
                        [
                            tmux,
                            "-L",
                            "ctf-codex-drag",
                            "list-sessions",
                            "-F",
                            "#{session_name}",
                        ],
                        env=env,
                        text=True,
                    ).strip()
                    master, slave = pty.openpty()
                    fcntl.ioctl(
                        slave, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 180, 0, 0)
                    )
                    client = subprocess.Popen(
                        [tmux, "-L", "ctf-codex-drag", "attach-session", "-t", session],
                        env=dict(env, TERM="xterm-256color"),
                        stdin=slave,
                        stdout=slave,
                        stderr=slave,
                    )
                    os.close(slave)

                    def state():
                        return subprocess.check_output(
                            [
                                tmux,
                                "-L",
                                "ctf-codex-drag",
                                "display-message",
                                "-p",
                                "-t",
                                session,
                                "#{synchronize-panes}:#{window_zoomed_flag}",
                            ],
                            env=env,
                            text=True,
                        ).strip()

                    def wait_state(expected):
                        deadline = time.monotonic() + 4
                        while time.monotonic() < deadline:
                            ready, _, _ = select.select([master], [], [], 0.05)
                            if ready:
                                try:
                                    os.read(master, 65536)
                                except OSError:
                                    break
                            if state() == expected:
                                return
                        self.assertEqual(state(), expected)

                    try:
                        wait_state("1:0")
                        # Send actual terminal F2/F3 sequences to a real tmux client.
                        os.write(master, b"\x1bOQ")
                        wait_state("0:0")
                        os.write(master, b"\x1bOQ")
                        wait_state("1:0")
                        os.write(master, b"\x1bOR")
                        wait_state("0:1")
                        os.write(master, b"\x1bOQ")
                        wait_state("0:1")
                        os.write(master, b"\x1bOR")
                        wait_state("1:0")
                    finally:
                        # Closing the PTY first prevents a client blocked on terminal output
                        # from hanging teardown after all keyboard assertions have completed.
                        os.close(master)
                        if client.poll() is None:
                            client.terminate()
                        try:
                            client.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            client.kill()
                            client.wait(timeout=5)

                    (problem / "dist_1/deploy/data").write_text("my progress")
                    second = subprocess.run(
                        ["bash", str(script)],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(second.returncode, 0, second.stderr)
                    self.assertEqual(
                        (problem / "dist_1/deploy/data").read_text(), "my progress"
                    )
                finally:
                    subprocess.run(
                        [tmux, "-L", "ctf-codex-drag", "kill-server"],
                        env=env,
                        capture_output=True,
                    )
