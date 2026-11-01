import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from rctf_monitor import app as m
from rctf_monitor.client import RCTFClient, ApiError
from rctf_monitor.config import private_json, parse_target

REPO = Path(__file__).resolve().parents[2]
ID = "fe0fbf3a-5036-476e-af77-d50a20aeeb4d"
TEAM = "d0e7a040-b432-49d9-858e-d33c244bea61"


class Fixture:
    def __init__(self):
        self.requests = []
        self.auth = "AUTH-SECRET"
        self.team_token = "TEAM-SECRET"
        self.solved = False
        self.ended = False
        self.failure = None
        self.content = b"first attachment"
        self.name = "UUID problem"
        self.file_url = "/file.bin"
        self.extra_files = []
        self.challenges = True
        self.requested_auth = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.do_GET()

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                parent.requests.append(
                    (self.command, path, self.headers.get("Authorization"), body)
                )
                if path == "/api/v1/auth/login":
                    if json.loads(body)["teamToken"] != parent.team_token:
                        return self.reply("badTokenVerification", None)
                    return self.reply("goodLogin", {"authToken": parent.auth})
                if parent.failure and path == parent.failure[0]:
                    status, data = parent.failure[1:]
                    self.send_response(status)
                    self.send_header("Retry-After", "2")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if (
                    path.startswith("/api/v1/")
                    and path
                    not in (
                        "/api/v1/integrations/client/config",
                        "/api/v1/leaderboard/now",
                    )
                    and path in ("/api/v1/users/me", "/api/v1/challs")
                ):
                    if self.headers.get("Authorization") != "Bearer " + parent.auth:
                        self.send_response(401)
                        self.end_headers()
                        return
                if path == "/api/v1/integrations/client/config":
                    return self.reply(
                        "goodClientConfig",
                        {
                            "ctfName": "Test CTF",
                            "startTime": 1,
                            "endTime": 1000
                            if parent.ended
                            else int(time.time() * 1000 + 3600000),
                        },
                    )
                if path == "/api/v1/users/me":
                    return self.reply(
                        "goodUserSelfData",
                        {
                            "id": TEAM,
                            "name": "Own team",
                            "score": 123 if parent.solved else 0,
                            "globalPlace": 2 if parent.solved else None,
                            "solves": [{"id": ID}] if parent.solved else [],
                            "teamToken": parent.team_token,
                            "email": "private@example.test",
                        },
                    )
                if path == "/api/v1/challs":
                    return self.reply(
                        "goodChallenges",
                        [
                            {
                                "id": ID,
                                "name": parent.name,
                                "category": "web",
                                "description": "Description",
                                "points": 123,
                                "files": [{"name": "file.bin", "url": parent.file_url}]
                                + parent.extra_files,
                            }
                        ]
                        if parent.challenges
                        else [],
                    )
                if path == "/api/v1/leaderboard/now":
                    return self.reply(
                        "goodLeaderboard",
                        {
                            "total": 5000,
                            "leaderboard": [
                                {
                                    "id": "opponent-" + str(i),
                                    "name": "Opponent " + str(i),
                                    "score": 1000 - i,
                                }
                                for i in range(5)
                            ],
                        },
                    )
                if path.startswith("/api/v1/users/"):
                    return self.reply("goodUserData", {"solves": [{"id": ID}]})
                if path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", parent.file_url)
                    self.end_headers()
                    return
                if path == "/file.bin":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(parent.content)))
                    self.end_headers()
                    self.wfile.write(parent.content)
                    return
                self.send_response(404)
                self.end_headers()

            def reply(self, kind, data):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"kind": kind, "data": data}).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class Scenario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.profile = self.root / "private/profile.json"
        private_json(
            self.profile,
            {"url": self.fixture.url, "root": "../workspace", "name": "Test CTF"},
        )
        private_json(
            self.profile.parent / "credentials.json",
            {"team_token": self.fixture.team_token},
        )
        m.configure(self.profile)
        self.state = m.SharedState()
        self.stop = threading.Event()
        self.monitor = m.Monitor(self.state, self.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(
            lambda: self.monitor.executor.shutdown(wait=True, cancel_futures=True)
        )

    def sync(self):
        self.monitor.refresh_config()
        snapshot = self.monitor.fetch_snapshot()
        self.monitor.process_snapshot(snapshot)
        # Wait on actual futures without closing the executor before update tests.
        deadline = time.monotonic() + 5
        while self.monitor.active_files and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.monitor.active_files)
        return snapshot

    def deploy(self):
        return next(m.BASE_DIR.glob("*/chall/deploy"))


class ClientTests(Scenario):
    def test_exchange_and_bearer_are_distinct(self):
        self.monitor.fetch_team()
        login, me = self.fixture.requests
        self.assertIsNone(login[2])
        self.assertEqual(json.loads(login[3]), {"teamToken": "TEAM-SECRET"})
        self.assertEqual(me[2], "Bearer AUTH-SECRET")

    def test_bad_token_is_not_in_error(self):
        client = RCTFClient(self.fixture.url, "WRONG-SECRET")
        with self.assertRaises(ApiError) as error:
            client.login()
        self.assertNotIn("SECRET", str(error.exception))
        self.assertFalse(client.logged_in)

    def test_bad_json_and_schema(self):
        for data in (b"<html>", b"[]", b"{}", b'{"kind":"bad SECRET"}'):
            with self.subTest(data=data):
                self.fixture.failure = ("/api/v1/challs", 200, data)
                with self.assertRaises(ApiError):
                    self.monitor.fetch_snapshot()

    def test_401_discards_auth_and_next_fetch_reauthenticates(self):
        self.monitor.fetch_team()
        self.fixture.auth = "ROTATED"
        self.assertTrue(self.monitor.client.logged_in)
        with self.assertRaises(ApiError):
            self.monitor.fetch_team()
        self.assertFalse(self.monitor.client.logged_in)
        self.monitor.fetch_team()

    def test_retry_after(self):
        self.fixture.failure = ("/api/v1/challs", 429, b"secret response")
        with self.assertRaises(ApiError):
            self.monitor.fetch_snapshot()
        self.assertGreater(self.monitor.client.retry_until, time.monotonic() + 1)
        count = len(self.fixture.requests)
        with self.assertRaises(ApiError):
            self.monitor.fetch_snapshot()
        self.assertEqual(count, len(self.fixture.requests))

    def test_public_file_has_no_auth(self):
        self.monitor.fetch_team()
        with self.monitor.client.open_file("/file.bin") as stream:
            self.assertEqual(stream.read(), self.fixture.content)
        self.assertIsNone(self.fixture.requests[-1][2])

    def test_cross_origin_file_redirect_has_no_auth(self):
        second = Fixture()
        self.addCleanup(second.close)
        self.fixture.file_url = second.url + "/file.bin"
        self.monitor.fetch_team()
        with self.monitor.client.open_file("/redirect") as stream:
            self.assertEqual(stream.read(), second.content)
        self.assertIsNone(second.requests[-1][2])

    def test_target_login_url_is_split(self):
        self.assertEqual(
            parse_target("https://example.test/login?token=private"),
            ("https://example.test", "private"),
        )
        for url in (
            "file:///etc/passwd",
            "https://user:pass@example.test",
            "https://example.test/?other=1",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_target(url)

    def test_config_permissions(self):
        self.assertEqual(self.profile.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            (self.profile.parent / "credentials.json").stat().st_mode & 0o777, 0o600
        )


class RuntimeTests(Scenario):
    def test_real_schema_uuid_helpers_and_secret_free_state(self):
        current = self.sync()
        self.assertIn(ID, current["challenges"])
        deploy = self.deploy()
        self.assertEqual((deploy / "file.bin").read_bytes(), self.fixture.content)
        for helper in ("22.sh", "25.sh"):
            self.assertTrue(os.access(deploy.parents[1] / helper, os.X_OK))
        self.assertTrue((deploy.parent / "AGENTS.md").is_file())
        self.assertEqual(self.state.rank, "-")
        for file in m.STATE_DIR.rglob("*"):
            if file.is_file():
                data = file.read_bytes()
                self.assertNotIn(b"TEAM-SECRET", data)
                self.assertNotIn(b"AUTH-SECRET", data)
                self.assertNotIn(b"private@example", data)

    def test_update_solve_authoritative_score_and_rank(self):
        self.sync()
        self.fixture.solved = True
        self.fixture.name = "Renamed"
        self.sync()
        self.assertEqual(self.state.score, 123)
        self.assertEqual(self.state.rank, 2)
        self.assertEqual(len(list(m.BASE_DIR.glob("*/chall"))), 1)
        kinds = [e["kind"] for e in self.state.events]
        self.assertIn("UPDATED", kinds)
        self.assertIn("SOLVED", kinds)

    def test_restart_skips_unchanged_download_and_keeps_user_work(self):
        self.sync()
        deploy = self.deploy()
        (deploy.parent / "notes.py").write_text("user work")
        (deploy.parent / "AGENTS.md").write_text("user rules")
        count = sum(r[1] == "/file.bin" for r in self.fixture.requests)
        self.monitor.executor.shutdown()
        self.monitor = m.Monitor(self.state, self.stop)
        self.sync()
        self.assertEqual(sum(r[1] == "/file.bin" for r in self.fixture.requests), count)
        self.assertEqual((deploy.parent / "AGENTS.md").read_text(), "user rules")
        self.assertEqual((deploy.parent / "notes.py").read_text(), "user work")

    def test_modified_attachment_is_backed_up(self):
        self.sync()
        file = self.deploy() / "file.bin"
        file.write_bytes(b"local edit")
        self.fixture.content = b"new"
        self.sync()
        self.assertEqual(file.read_bytes(), b"new")
        self.assertIn(
            b"local edit", [p.read_bytes() for p in m.ARCHIVE_DIR.rglob("file.bin")]
        )

    def test_removed_challenge_preserves_work(self):
        self.sync()
        deploy = self.deploy()
        self.fixture.challenges = False
        self.sync()
        self.assertTrue(deploy.exists())
        self.assertEqual(self.state.challenges, {})
        self.assertIn("DELETED", [e["kind"] for e in self.state.events])

    def test_markdown_links_and_duplicate_names(self):
        files = self.monitor.normalize_files(
            {
                "description": "[one](https://files.test/one/a.zip) [website](https://x.test/start)",
                "files": [
                    {"name": "a.zip", "url": "https://files.test/two/a.zip"},
                    {"name": "a.zip", "url": "https://files.test/two/a.zip"},
                ],
            }
        )
        self.assertEqual([x["local_name"] for x in files], ["a.zip", "a_2.zip"])

    def test_signed_query_renewal_does_not_redownload(self):
        self.fixture.file_url = "/file.bin?signature=first"
        self.sync()
        self.fixture.file_url = "/file.bin?signature=renewed"
        self.sync()
        self.assertEqual(sum(r[1] == "/file.bin" for r in self.fixture.requests), 1)

    def test_end_time_milliseconds(self):
        self.fixture.ended = True
        self.sync()
        self.assertEqual(self.state.status, "ENDED")
        self.assertEqual(m.competition_countdown(self.state.view())[0], "ENDED")

    def test_board_is_bounded_and_cached(self):
        self.sync()
        self.sync()
        self.assertEqual(
            sum(r[1] == "/api/v1/leaderboard/now" for r in self.fixture.requests), 1
        )
        self.assertLessEqual(
            sum(
                r[1].startswith("/api/v1/users/opponent-")
                for r in self.fixture.requests
            ),
            5,
        )

    def test_private_opponent_solves_stay_unknown(self):
        self.fixture.failure = ("/api/v1/users/opponent-0", 403, b"")
        self.sync()
        self.assertIsNone(self.state.scoreboard[0]["solved_challenge_ids"])

    def test_zip_extract_and_traversal_rejection(self):
        for name, valid in [("inside.txt", True), ("../escaped", False)]:
            with self.subTest(name=name):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w") as archive:
                    archive.writestr(name, "hello")
                self.fixture.content = stream.getvalue()
                self.fixture.file_url = "/file.bin?v=" + str(valid)
                if list(m.BASE_DIR.glob("*/chall/deploy/file.bin")):
                    (self.deploy() / "file.bin").unlink()
                self.sync()
                if valid:
                    self.assertEqual(
                        (self.deploy() / "inside.txt").read_text(), "hello"
                    )
                else:
                    self.assertTrue(any("ERROR" == j.state for j in self.state.jobs))
                self.assertFalse((self.deploy().parent / "escaped").exists())

    def test_symlink_workspace_rejected(self):
        self.sync()
        deploy = self.deploy()
        (deploy / "file.bin").unlink()
        (deploy / "file.bin").symlink_to(self.root / "outside")
        with self.assertRaises(ValueError):
            self.sync()
        self.assertFalse((self.root / "outside").exists())

    def test_stale_failure_keeps_previous_snapshot(self):
        self.sync()
        old = m.CURRENT_FILE.read_bytes()
        self.fixture.failure = ("/api/v1/challs", 503, b"private")
        with self.assertRaises(ApiError):
            self.monitor.fetch_snapshot()
        self.assertEqual(m.CURRENT_FILE.read_bytes(), old)

    def test_prestart_response(self):
        self.fixture.failure = ("/api/v1/challs", 200, b'{"kind":"badNotStarted"}')
        with self.assertRaises(ApiError) as e:
            self.monitor.fetch_snapshot()
        self.assertEqual(e.exception.code, "badNotStarted")


class DisplayTests(Scenario):
    def test_uuid_comparison_and_own_rank_order(self):
        self.fixture.solved = True
        self.fixture.failure = ("/api/v1/users/opponent-3", 403, b"")
        self.sync()
        text = "\n".join(m.render_team_comparison(self.state.view(), 160))
        plain = m.ANSI_RE.sub("", text)
        self.assertIn("TEAM COMPARISON", plain)
        self.assertIn("✓", plain)
        self.assertIn(m.OWN_TEAM_BG, text)
        self.assertLess(plain.index("Opponent 0"), plain.index("Own team"))
        self.assertLess(plain.index("Own team"), plain.index("Opponent 2"))

    def test_sizes_resources_and_warning(self):
        self.sync()
        for columns, rows in ((120, 61), (109, 54), (80, 24), (30, 10)):
            with (
                self.subTest(columns=columns),
                patch.object(
                    m.shutil,
                    "get_terminal_size",
                    return_value=os.terminal_size((columns, rows)),
                ),
            ):
                screen = m.render_screen(
                    self.state.view(), m.ResourceSampler().sample()
                )
                self.assertLessEqual(len(screen.splitlines()), rows)
                self.assertTrue(
                    all(
                        m.display_width(line) <= columns for line in screen.splitlines()
                    )
                )
                if rows >= 24:
                    self.assertIn("CPU", screen)
                    self.assertIn("MEMORY", screen)
                if columns < 109:
                    self.assertTrue(m.terminal_size_warning_overlays(columns, rows))

    def test_badges_expire(self):
        self.assertIsNone(
            m.marker_badge({"marker": "NEW", "marker_expires": time.time() - 1})
        )
        self.assertEqual(
            m.marker_badge({"marker": "NEW", "marker_expires": time.time() + 50})[0],
            "NEW",
        )

    def test_terminal_injection_removed(self):
        value = m.terminal_text("evil\x1b]52;c;secret\x07\x1b[2Jname\r\n")
        self.assertNotIn("\x1b]52", value)
        self.assertNotIn("\x1b[2J", value)


class CLITests(Scenario):
    def test_once_process_and_no_secret_output(self):
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "rctf-monitor.py"),
                "--config",
                str(self.profile),
                "--once",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["challenges"], 1)
        self.assertNotIn("SECRET", result.stdout + result.stderr)

    def test_check_does_not_download(self):
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "rctf-monitor.py"),
                "--config",
                str(self.profile),
                "--check",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(r[1] == "/file.bin" for r in self.fixture.requests))

    def test_lock_rejects_second_instance(self):
        lock = m.InstanceLock(m.LOCK_FILE)
        self.assertTrue(lock.acquire())
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "rctf-monitor.py"),
                    "--config",
                    str(self.profile),
                    "--once",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("already running", result.stderr)
        finally:
            lock.release()

    def test_ctrl_c_clears_terminal(self):
        import pty, select

        master, slave = pty.openpty()
        env = dict(os.environ, PATH="/usr/bin:/bin", TERM="xterm-256color")
        process = subprocess.Popen(
            [
                sys.executable,
                str(REPO / "rctf-monitor.py"),
                "--config",
                str(self.profile),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
        )
        os.close(slave)
        data = b""
        try:
            deadline = time.monotonic() + 10
            while b"SYSTEM RESOURCES" not in data and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    data += os.read(master, 65536)
            self.assertIn(b"SYSTEM RESOURCES", data)
            process.send_signal(signal.SIGINT)
            while process.poll() is None:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        data += os.read(master, 65536)
                    except OSError:
                        break
                if time.monotonic() > deadline + 10:
                    self.fail("SIGINT shutdown timed out")
            process.wait(timeout=5)
            while select.select([master], [], [], 0.1)[0]:
                try:
                    data += os.read(master, 65536)
                except OSError:
                    break
            self.assertEqual(process.returncode, 0, data[-1000:])
            self.assertTrue(
                data.endswith(b"\x1b[0m\x1b[2J\x1b[H\x1b[?25h"), data[-100:]
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)


class FailureTests(Scenario):
    def test_real_401_period_responses_preserve_auth(self):
        self.monitor.fetch_team()
        for code in ("badNotStarted", "badEnded"):
            self.fixture.failure = (
                "/api/v1/challs",
                401,
                json.dumps({"kind": code}).encode(),
            )
            with self.assertRaises(ApiError) as error:
                self.monitor.fetch_snapshot()
            self.assertEqual(error.exception.code, code)
            self.assertTrue(self.monitor.client.logged_in)
            self.assertEqual(self.state.team_name, "Own team")

    def test_disk_full_preserves_prior_file_and_delays_retry(self):
        import errno

        self.sync()
        file = self.deploy() / "file.bin"
        file.write_bytes(b"local edit")
        with patch.object(
            m, "require_space", side_effect=OSError(errno.ENOSPC, "No space")
        ):
            self.sync()
        self.assertEqual(file.read_bytes(), b"local edit")
        count = sum(r[1] == "/file.bin" for r in self.fixture.requests)
        self.sync()
        self.assertEqual(sum(r[1] == "/file.bin" for r in self.fixture.requests), count)
        self.assertFalse(list(self.deploy().glob(".*rctf-part-*")))

    def test_archive_preflight_space(self):
        from rctf_monitor import archives
        from collections import namedtuple

        archive = self.root / "test.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("file", "content")
        usage = namedtuple("usage", "total used free")(1000, 999, 1)
        with (
            patch.object(archives.shutil, "disk_usage", return_value=usage),
            self.assertRaises(OSError),
        ):
            archives.extract(archive, self.root / "unpack")
        self.assertFalse((self.root / "unpack/file").exists())

    def test_unsupported_archive_not_retried_forever(self):
        import stat

        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(link, "/tmp/outside")
        self.fixture.content = stream.getvalue()
        self.sync()
        entry = next(iter(self.monitor.manifest.values()))
        self.assertEqual(entry["extraction"]["status"], "blocked")
        count = len(self.state.events)
        self.monitor.retry_files.clear()
        self.sync()
        self.assertEqual(count, len(self.state.events))
        self.assertTrue((self.deploy() / "file.bin").exists())

    def test_failed_setup_keeps_existing_credentials(self):
        from rctf_monitor.installer import main

        before = self.profile.read_bytes()
        creds = (self.profile.parent / "credentials.json").read_bytes()
        with (
            patch("getpass.getpass", return_value="WRONG"),
            self.assertRaises(ApiError),
        ):
            main(["--url", self.fixture.url, "--profile", str(self.profile)])
        self.assertEqual(self.profile.read_bytes(), before)
        self.assertEqual((self.profile.parent / "credentials.json").read_bytes(), creds)

    def test_successful_setup(self):
        from rctf_monitor.installer import main

        with (
            patch("getpass.getpass", return_value=self.fixture.team_token),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main(["--url", self.fixture.url, "--profile", str(self.profile)]), 0
            )
        self.assertEqual(json.loads(self.profile.read_text())["name"], "Test CTF")

    def test_auth_redirect_does_not_resend_secret(self):
        from rctf_monitor.client import SafeRedirect
        import urllib.request

        handler = SafeRedirect()
        for req in (
            urllib.request.Request(
                self.fixture.url + "/api/v1/users/me",
                headers={"Authorization": "Bearer PRIVATE"},
            ),
            urllib.request.Request(
                self.fixture.url + "/api/v1/auth/login", data=b"PRIVATE"
            ),
        ):
            with self.assertRaises(ApiError):
                handler.redirect_request(req, None, 302, "", {}, "https://other.test/")

    def test_missing_helper_fails_before_download(self):
        with patch.object(m, "find_helpers", side_effect=FileNotFoundError("22.sh")):
            with self.assertRaises(FileNotFoundError):
                m.Monitor(m.SharedState(), threading.Event())
        self.assertFalse(self.fixture.requests)


class MoreContracts(Scenario):
    def test_cancellation_preserves_existing_file(self):
        self.sync()
        file = self.deploy() / "file.bin"
        file.write_bytes(b"preserve")
        challenge = next(iter(self.monitor.previous["challenges"].values()))
        self.stop.set()
        self.monitor.download_worker(
            challenge, challenge["files"][0], self.deploy(), True, (ID, "file.bin")
        )
        self.assertEqual(file.read_bytes(), b"preserve")
        self.assertFalse(list(self.deploy().glob(".*rctf-part-*")))

    def test_short_response_preserves_existing_file(self):
        self.sync()
        file = self.deploy() / "file.bin"
        file.write_bytes(b"preserve")

        class Response(io.BytesIO):
            headers = {"Content-Length": "100"}

        challenge = next(iter(self.monitor.previous["challenges"].values()))
        with patch.object(
            self.monitor.client, "open_file", return_value=Response(b"short")
        ):
            self.monitor.download_worker(
                challenge, challenge["files"][0], self.deploy(), True, (ID, "file.bin")
            )
        self.assertEqual(file.read_bytes(), b"preserve")
        self.assertEqual(self.state.jobs[-1].state, "ERROR")

    def test_codex_rows_and_resource_peak(self):
        usage = {
            "status": "LIVE",
            "remaining_percent": 0,
            "credits_balance": "23.50",
            "competition_start_credits": "100",
        }
        line = m.ANSI_RE.sub("", m.codex_usage_line(usage, 120))
        self.assertIn("CODEX WEEKLY", line)
        self.assertIn("CREDITS 100.00 → 23.50", line)
        values = [None, 46.2, 0]
        lines = [
            m.ANSI_RE.sub("", x)
            for x in m.resource_chart("CPU", 17, 46.2, values, 35, m.CYAN)
        ]
        row = next(x for x in lines if "40% ┤" in x)
        self.assertIn("▆", row)
        self.assertNotIn("▆", next(x for x in lines if "60% ┤" in x))

    def test_known_ended_without_limit_and_bad_timezone(self):
        self.assertEqual(
            m.competition_countdown({"contest_end": "", "contest_end_known": True})[0],
            "NO LIMIT",
        )
        self.assertEqual(
            m.competition_countdown({"contest_end": "2026-01-01T00:00:00"})[0],
            "UNAVAILABLE",
        )

    def test_unicode_and_control_characters_in_names(self):
        name = m.safe_directory_name('bad/"`$(touch hacked)`\n한글')
        for forbidden in ("/", '"', "`", "$", "\n"):
            self.assertNotIn(forbidden, name)

    def test_incomplete_extraction_retries_after_restart(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr("inside.txt", "hello")
        self.fixture.content = stream.getvalue()
        self.sync()
        (self.deploy() / "inside.txt").unlink()
        self.monitor.executor.shutdown()
        self.monitor = m.Monitor(self.state, self.stop)
        self.sync()
        self.assertEqual((self.deploy() / "inside.txt").read_text(), "hello")
        self.assertEqual(sum(r[1] == "/file.bin" for r in self.fixture.requests), 1)


class LargeScreenContracts(Scenario):
    def test_long_comparison_does_not_hide_problem_list(self):
        self.sync()
        view = self.state.view()
        base = next(iter(view["challenges"].values()))
        view["challenges"] = {
            str(i): dict(base, id=str(i), name=f"Problem {i:02}") for i in range(69)
        }
        for row in view["scoreboard"]:
            row["solved_challenge_ids"] = ["0"] if row["id"] != TEAM else []
        with patch.object(
            m.shutil, "get_terminal_size", return_value=os.terminal_size((150, 60))
        ):
            text = m.ANSI_RE.sub(
                "", m.render_screen(view, m.ResourceSampler().sample())
            )
        self.assertGreaterEqual(text.count("Problem 00"), 2)
        self.assertIn("TEAM COMPARISON", text)
        self.assertIn("SYSTEM RESOURCES", text)

    def test_once_reports_blocked_archive_as_failure(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr("../escape", "no")
        self.fixture.content = stream.getvalue()
        for _ in range(2):
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "rctf-monitor.py"),
                    "--config",
                    str(self.profile),
                    "--once",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(json.loads(result.stdout)["unresolved_attachments"], 1)


class WorkspaceLayoutTests(Scenario):
    def test_fresh_setup_creates_competition_root(self):
        from rctf_monitor.installer import main
        profile = self.root / 'new-private/profile.json'
        with patch('getpass.getpass', return_value=self.fixture.team_token), contextlib.redirect_stdout(io.StringIO()):
            main(['--url', self.fixture.url, '--profile', str(profile)])
        config = json.loads(profile.read_text())
        self.assertEqual(config['root'], '../workspace/Test_CTF')
        m.configure(profile)
        self.monitor.executor.shutdown()
        self.monitor = m.Monitor(self.state, self.stop)
        self.sync()
        expected = self.root / 'workspace/Test_CTF/UUID_problem/chall/deploy/file.bin'
        self.assertTrue(expected.is_file())
        self.assertFalse((self.root / ('workspace/' + ID + '_UUID_problem')).exists())

    def test_names_only_and_rename_keeps_directory(self):
        current = self.sync()
        self.assertEqual(current['challenges'][ID]['directory'], 'UUID_problem')
        self.fixture.name = 'New name'
        self.assertEqual(self.sync()['challenges'][ID]['directory'], 'UUID_problem')

    def test_same_name_does_not_overwrite_other_problem(self):
        real = self.monitor.client.request_json
        def request(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if path == '/api/v1/challs':
                return result + [dict(result[0], id='second-id')]
            return result
        with patch.object(self.monitor.client, 'request_json', side_effect=request):
            current = self.sync()
            self.assertEqual({c['directory'] for c in current['challenges'].values()}, {'UUID_problem', 'UUID_problem_2'})
            self.assertEqual(self.sync()['challenges']['second-id']['directory'], 'UUID_problem_2')

    def test_existing_untracked_work_is_not_adopted(self):
        existing = m.BASE_DIR / 'UUID_problem'
        existing.mkdir(parents=True)
        (existing / 'notes.txt').write_text('keep')
        self.assertEqual(self.sync()['challenges'][ID]['directory'], 'UUID_problem_2')
        self.assertEqual((existing / 'notes.txt').read_text(), 'keep')
        self.assertFalse((existing / 'chall').exists())

    def test_setup_again_keeps_selected_competition_root(self):
        from rctf_monitor.installer import main
        config = json.loads(self.profile.read_text())
        config['root'] = '../workspace/Existing_Competition'
        private_json(self.profile, config)
        with patch('getpass.getpass', return_value=self.fixture.team_token), contextlib.redirect_stdout(io.StringIO()):
            main(['--url', self.fixture.url, '--profile', str(self.profile)])
        self.assertEqual(json.loads(self.profile.read_text())['root'], config['root'])
