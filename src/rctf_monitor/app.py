"""Live rCTF challenge monitor and downloader with competition profiles."""

from __future__ import annotations


from .archives import extract, require_space


from .client import ApiError, RCTFClient


from .assets import REQUIRED_HELPERS, DEFAULT_AGENTS, find_helpers


from . import __version__


import argparse


import concurrent.futures


import copy


import fcntl


import hashlib


import json


import math


import os


import re


import select


import signal


import shutil


import subprocess


import sys


import tarfile


import threading


import time


import unicodedata


import urllib.error


import urllib.parse


import urllib.request


import uuid


import zipfile


from collections import deque


from dataclasses import dataclass, field


from datetime import datetime, timezone


from pathlib import Path


from typing import Any


TEST_MODE = os.environ.get("RCTF_MONITOR_TEST") == "1"


BASE_URL = os.environ.get("RCTF_MONITOR_URL", "")


TOKEN = os.environ.get("RCTF_MONITOR_TOKEN", "")


TITLE = "rCTF Monitor"


CONTEST_END = ""


POLL_INTERVAL = 0.5 if TEST_MODE else 5.0


BADGE_TTL = 60.0


DONE_TTL = 5.0


MAX_WORKERS = 4


UI_REFRESH_INTERVAL = 1.0


RESOURCE_SAMPLE_INTERVAL = 1.0


RESOURCE_HISTORY_SECONDS = 120.0


RESOURCE_RETENTION_SECONDS = RESOURCE_HISTORY_SECONDS


RESOURCE_HISTORY_SAMPLES = (
    int(RESOURCE_RETENTION_SECONDS / RESOURCE_SAMPLE_INTERVAL) + 2
)


SCRIPT_DIR = Path(__file__).resolve().parent


BASE_DIR = Path(os.environ.get("RCTF_MONITOR_ROOT", SCRIPT_DIR))


STATE_DIR = Path(os.environ.get("RCTF_MONITOR_STATE", BASE_DIR / ".rctf-monitor"))


CURRENT_FILE = STATE_DIR / "current.json"


EVENT_FILE = STATE_DIR / "events.jsonl"


SNAPSHOT_DIR = STATE_DIR / "snapshots"


ARCHIVE_DIR = STATE_DIR / "archive"


MANIFEST_FILE = STATE_DIR / "manifest.json"


CODEX_USAGE_FILE = STATE_DIR / "codex-usage.json"


LOCK_FILE = STATE_DIR / "monitor.lock"


STAGING_DIR = STATE_DIR / "staging"


HELPER_FILES = REQUIRED_HELPERS


CODEX_USAGE_POLL_INTERVAL = 10.0 if TEST_MODE else 12.0


MIN_TERMINAL_COLUMNS = 109


MIN_TERMINAL_ROWS = 54


ESC = "\033["


RESET = f"{ESC}0m"


BOLD = f"{ESC}1m"


RED = f"{ESC}38;5;203m"


GREEN = f"{ESC}38;5;84m"


YELLOW = f"{ESC}38;5;220m"


CYAN = f"{ESC}38;5;45m"


BLUE = f"{ESC}38;5;75m"


GRAY = f"{ESC}38;5;245m"


WHITE = f"{ESC}38;5;255m"


GREEN_BG = f"{ESC}48;2;35;48;42m"


RED_BG = f"{ESC}48;2;52;38;41m"


OWN_TEAM_BG = f"{ESC}48;2;45;65;78m"


SCREEN_STYLE = f"{ESC}48;2;19;22;27m{ESC}38;2;220;220;220m"


COMPARISON_GREEN = f"{ESC}38;2;166;191;174m"


COMPARISON_RED = f"{ESC}38;2;203;173;177m"


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


TERMINAL_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\|$)|[PX^_].*?(?:\x1b\\|$)|.|$)",
    re.DOTALL,
)


PARTIAL_BLOCKS = " ▁▂▃▄▅▆▇█"


def terminal_text(text: str) -> str:
    """Keep styling, discard cursor/clipboard/control commands from remote text."""
    text = TERMINAL_ESCAPE_RE.sub(
        lambda match: match[0] if re.fullmatch(r"\x1b\[[0-9;]*m", match[0]) else "",
        text,
    )
    return "".join(
        char
        for char in text
        if char == "\x1b" or (ord(char) >= 32 and not 127 <= ord(char) < 160)
    )


def paint(text: str, color: str, *, bold: bool = False) -> str:
    return f"{BOLD if bold else ''}{color}{text}{RESET}"


def display_width(text: str) -> int:
    width = 0
    for character in ANSI_RE.sub("", text):
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def truncate(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    result: list[str] = []
    used = 0
    position = 0
    while position < len(text):
        ansi = ANSI_RE.match(text, position)
        if ansi:
            result.append(ansi.group())
            position = ansi.end()
            continue
        character = text[position]
        character_width = (
            0
            if unicodedata.combining(character)
            else (2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1)
        )
        if used + character_width > max(0, width - 1):
            break
        result.append(character)
        used += character_width
        position += 1
    return "".join(result) + "…" + RESET


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def boxed(title: str, lines: list[str], width: int, color: str) -> list[str]:
    label = f" {truncate(title, max(1, width - 4))} "
    output = [
        paint(
            "┌" + label + "─" * max(0, width - display_width(label) - 2) + "┐",
            color,
            bold=True,
        )
    ]
    inner = width - 2
    for line in lines or [""]:
        line = truncate(line, inner - 2)
        output.append(
            paint("│", color) + " " + pad(line, inner - 1) + paint("│", color)
        )
    output.append(paint("└" + "─" * (width - 2) + "┘", color))
    return output


def fit_panel(lines: list[str], rows: int, width: int, color: str) -> list[str]:
    """Keep panel borders and make omitted rows explicit when the screen is full."""
    if len(lines) <= rows:
        return lines
    if rows < 3:
        return []
    omitted = len(lines) - rows + 1
    notice = boxed(
        "", [paint(f"… {omitted} more rows · enlarge terminal", GRAY)], width, color
    )[1]
    return lines[: rows - 2] + [notice, lines[-1]]


def safe_directory_name(value: object) -> str:
    name = re.sub(r"\s+", "_", str(value).strip())
    # Helpers send a quoted cd command to worker shells. Do not let remote
    # titles introduce shell substitutions or terminate those quotes.
    name = re.sub(r'[/\\"$`\x00-\x1f\x7f]', "_", name)
    if name in {"", ".", ".."}:
        name = "unnamed_challenge"
    encoded = name.encode("utf-8")
    if len(encoded) > 220:
        name = encoded[:220].decode("utf-8", errors="ignore").rstrip("._ ")
    return name or "unnamed_challenge"


def safe_file_name(value: str, fallback: str) -> str:
    name = Path(urllib.parse.unquote(value)).name
    name = re.sub(r"[/\\\x00-\x1f\x7f]", "_", name).strip()
    return name if name not in {"", ".", ".."} else fallback


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def timestamp_name() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DownloadCancelled(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            return False
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"{os.getpid()}\n")
        self.handle.flush()
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


@dataclass
class TransferJob:
    challenge_id: str
    challenge: str
    filename: str
    action: str
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    percent: int = 0
    progress: str = ""
    state: str = "ACTIVE"
    done_at: float | None = None
    created_at: float = field(default_factory=time.monotonic)


class ResourceSampler:
    """Collect a stable rolling window of host CPU and memory usage."""

    def __init__(self) -> None:
        self.samples: deque[tuple[float, float, float]] = deque(
            maxlen=RESOURCE_HISTORY_SAMPLES
        )
        # Completed display cells, not extra raw samples. Keep their heights
        # until they scroll out, even as the 120-second raw history is pruned.
        self.plot_cache: dict[
            tuple[int, int], tuple[float, int, tuple[float | None, ...]]
        ] = {}
        self.previous_cpu = self.read_cpu_times()
        self.last_sample = time.monotonic()
        try:
            self.cpu_cores = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            self.cpu_cores = os.cpu_count() or 1
        memory_percent, used_gib, total_gib = self.read_memory()
        self.current_cpu = 0.0
        self.current_memory = memory_percent
        self.used_gib = used_gib
        self.total_gib = total_gib
        self.samples.append((self.last_sample, self.current_cpu, self.current_memory))

    @staticmethod
    def read_cpu_times() -> tuple[int, int]:
        fields = (
            Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:9]
        )
        values = [int(value) for value in fields]
        return sum(values), values[3] + values[4]

    @staticmethod
    def read_memory() -> tuple[float, float, float]:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, remainder = line.partition(":")
            if remainder:
                values[key] = int(remainder.split()[0])
        total_kib = values.get("MemTotal", 0)
        available_kib = values.get(
            "MemAvailable",
            values.get("MemFree", 0)
            + values.get("Buffers", 0)
            + values.get("Cached", 0),
        )
        used_kib = max(0, total_kib - available_kib)
        percent = used_kib * 100 / total_kib if total_kib else 0.0
        return percent, used_kib / (1024 * 1024), total_kib / (1024 * 1024)

    def sample(self) -> dict[str, object]:
        now = time.monotonic()
        if now - self.last_sample >= RESOURCE_SAMPLE_INTERVAL:
            try:
                total, idle = self.read_cpu_times()
                previous_total, previous_idle = self.previous_cpu
                total_delta = total - previous_total
                idle_delta = idle - previous_idle
                if total_delta > 0:
                    self.current_cpu = max(
                        0.0, min(100.0, (total_delta - idle_delta) * 100 / total_delta)
                    )
                self.previous_cpu = (total, idle)
                self.current_memory, self.used_gib, self.total_gib = self.read_memory()
            except (OSError, ValueError, IndexError):
                pass
            self.last_sample = now
            self.samples.append((now, self.current_cpu, self.current_memory))

        cutoff = now - RESOURCE_RETENTION_SECONDS
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        return {
            "now": now,
            "cpu": self.current_cpu,
            "memory": self.current_memory,
            "cpu_cores": self.cpu_cores,
            "used_gib": self.used_gib,
            "total_gib": self.total_gib,
            "samples": list(self.samples),
            "plot_cache": self.plot_cache,
        }


class SharedState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.status = "CONNECTING"
        self.team_name = "-"
        self.account_id = None
        self.score = 0
        self.rank = "-"
        self.scoreboard: list[dict[str, object]] = []
        self.contest_end = CONTEST_END
        self.contest_end_known = bool(CONTEST_END)
        self.challenges: dict[str, dict[str, object]] = {}
        self.codex_usage: dict[str, object] = {"status": "ABSENT"}
        self.events: list[dict[str, object]] = []
        self.jobs: list[TransferJob] = []

    def add_job(self, job: TransferJob) -> None:
        with self.lock:
            self.jobs.append(job)

    def update_job(self, job: TransferJob, **values: object) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(job, key, value)

    def finish_job(self, job: TransferJob, *, error: str = "") -> None:
        with self.lock:
            job.percent = 100 if not error else job.percent
            job.state = "ERROR" if error else "DONE"
            job.progress = error or job.progress
            job.done_at = time.monotonic()

    def view(self) -> dict[str, object]:
        now = time.monotonic()
        with self.lock:
            self.jobs = [
                job
                for job in self.jobs
                if job.done_at is None or now - job.done_at < DONE_TTL
            ]
            return {
                "status": self.status,
                "team_name": self.team_name,
                "account_id": self.account_id,
                "score": self.score,
                "rank": self.rank,
                "scoreboard": copy.deepcopy(self.scoreboard),
                "contest_end": self.contest_end,
                "contest_end_known": self.contest_end_known,
                "challenges": copy.deepcopy(self.challenges),
                "codex_usage": copy.deepcopy(self.codex_usage),
                "events": copy.deepcopy(self.events[-100:]),
                "jobs": copy.copy(self.jobs),
            }


class Monitor:
    @staticmethod
    def read_codex_weekly_usage() -> dict[str, object]:
        command = shutil.which("codex")
        if not command:
            raise RuntimeError("codex command not found")
        process = subprocess.Popen(
            [command, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        try:
            if process.stdin is None or process.stdout is None:
                raise RuntimeError("Codex app server pipes are unavailable")
            requests = [
                {
                    "method": "initialize",
                    "id": 0,
                    "params": {
                        "clientInfo": {
                            "name": "rctf_monitor",
                            "title": "rCTF Monitor",
                            "version": "1.0.0",
                        }
                    },
                },
                {"method": "account/rateLimits/read", "id": 1, "params": {}},
            ]
            for request in requests:
                process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + 12.0
            result: dict[str, object] | None = None
            while time.monotonic() < deadline:
                ready, _, _ = select.select(
                    [process.stdout], [], [], min(0.5, deadline - time.monotonic())
                )
                if not ready:
                    if process.poll() is not None:
                        break
                    continue
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict) and message.get("id") == 1:
                    value = message.get("result")
                    result = value if isinstance(value, dict) else None
                    break
            if result is None:
                raise RuntimeError("Codex usage request timed out")
            limits_by_id = result.get("rateLimitsByLimitId")
            codex = (
                limits_by_id.get("codex") if isinstance(limits_by_id, dict) else None
            )
            if not isinstance(codex, dict):
                codex = result.get("rateLimits")
            if not isinstance(codex, dict):
                raise RuntimeError("Codex rate limit data is unavailable")
            windows = [
                window
                for window in (codex.get("primary"), codex.get("secondary"))
                if isinstance(window, dict)
            ]
            if not windows:
                raise RuntimeError("Codex usage window is unavailable")
            weekly = max(
                windows, key=lambda window: int(window.get("windowDurationMins") or 0)
            )
            used = max(0.0, min(100.0, float(weekly.get("usedPercent") or 0.0)))
            credits = (
                codex.get("credits") if isinstance(codex.get("credits"), dict) else {}
            )
            return {
                "status": "LIVE",
                "used_percent": used,
                "remaining_percent": 100.0 - used,
                "window_minutes": int(weekly.get("windowDurationMins") or 0),
                "credits_balance": str(credits.get("balance") or "")
                if credits.get("hasCredits")
                else "",
                "credits_unlimited": bool(credits.get("unlimited")),
                "plan": str(codex.get("planType") or ""),
                "updated_at": iso_now(),
            }
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()

    def run_codex_usage(self) -> None:
        while not self.stop.is_set():
            if not shutil.which("codex"):
                with self.state.lock:
                    self.state.codex_usage = {"status": "ABSENT"}
                self.stop.wait(CODEX_USAGE_POLL_INTERVAL)
                continue
            with self.state.lock:
                if self.state.codex_usage.get("status") == "ABSENT":
                    self.state.codex_usage = {"status": "WAITING"}
            try:
                usage = self.read_codex_weekly_usage()
            except Exception as error:
                with self.state.lock:
                    previous = self.state.codex_usage
                    self.state.codex_usage = {
                        **previous,
                        "status": "ERROR",
                        "error": str(error),
                    }
            else:
                current_credits = usage.get("credits_balance")
                start_credits = self.codex_usage_cache.get("competition_start_credits")
                with self.state.lock:
                    contest_status = self.state.status
                if current_credits:
                    if contest_status == "PRE-START":
                        self.codex_usage_cache["pre_start_credits"] = current_credits
                        atomic_json(CODEX_USAGE_FILE, self.codex_usage_cache)
                    elif not start_credits:
                        start_credits = (
                            self.codex_usage_cache.get("pre_start_credits")
                            or current_credits
                        )
                        self.codex_usage_cache["competition_start_credits"] = (
                            start_credits
                        )
                        self.codex_usage_cache.pop("pre_start_credits", None)
                        atomic_json(CODEX_USAGE_FILE, self.codex_usage_cache)
                usage["competition_start_credits"] = (
                    start_credits or current_credits or ""
                )
                with self.state.lock:
                    self.state.codex_usage = usage
            self.stop.wait(CODEX_USAGE_POLL_INTERVAL)

    def cleanup_interrupted_work(self) -> None:
        if STAGING_DIR.is_dir():
            shutil.rmtree(STAGING_DIR, ignore_errors=True)
        try:
            for path in BASE_DIR.glob("*/chall/deploy/.*.rctf-part-*"):
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            for path in STATE_DIR.rglob(".*.tmp-*"):
                path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def find_helpers() -> dict[str, Path]:
        return find_helpers(HELPER_FILES, BASE_DIR, SCRIPT_DIR)

    def load_events(self) -> None:
        events: list[dict[str, object]] = []
        try:
            for line in EVENT_FILE.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    events.append(value)
                    event_id = value.get("event_id")
                    if isinstance(event_id, str):
                        self.event_ids.add(event_id)
        except OSError:
            pass
        with self.state.lock:
            self.state.events = events[-200:]

    def add_event(
        self,
        kind: str,
        message: str,
        challenge_id: str | None = None,
        *,
        event_id: str | None = None,
    ) -> None:
        with self.event_lock:
            if event_id and event_id in self.event_ids:
                return
            event = {
                "time": iso_now(),
                "kind": kind,
                "message": message,
                "challenge_id": challenge_id,
                "expires_at": time.time() + BADGE_TTL
                if kind in {"NEW", "UPDATED"}
                else None,
            }
            if event_id:
                event["event_id"] = event_id
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with EVENT_FILE.open("a", encoding="utf-8") as output:
                output.write(json.dumps(event, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
            if event_id:
                self.event_ids.add(event_id)
            with self.state.lock:
                self.state.events.append(event)

    def marker_for(self, challenge_id: str) -> tuple[str, float] | None:
        now = time.time()
        with self.state.lock:
            for event in reversed(self.state.events):
                if event.get("challenge_id") != challenge_id or event.get(
                    "kind"
                ) not in {"NEW", "UPDATED"}:
                    continue
                expires = float(event.get("expires_at") or 0)
                try:
                    created = datetime.fromisoformat(
                        str(event.get("time") or "").replace("Z", "+00:00")
                    )
                    if created.tzinfo is None:
                        return None
                    expires = min(expires, created.timestamp() + BADGE_TTL)
                except (ValueError, OverflowError):
                    return None
                return (str(event["kind"]), expires) if expires > now else None
        return None

    def archive_existing(self, challenge_id: str, destination: Path) -> None:
        if destination.is_file():
            target_dir = ARCHIVE_DIR / str(challenge_id) / timestamp_name()
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, target_dir / destination.name)

    @staticmethod
    def manifest_key(challenge_id: str, local_name: str) -> str:
        return f"{challenge_id}/{local_name}"

    def save_manifest(self) -> None:
        with self.manifest_lock:
            atomic_json(MANIFEST_FILE, self.manifest)

    def local_file_is_current(
        self, challenge_id: str, file_info: dict[str, str], destination: Path
    ) -> bool:
        key = self.manifest_key(challenge_id, file_info["local_name"])
        with self.manifest_lock:
            entry = copy.deepcopy(self.manifest.get(key))
        if (
            not isinstance(entry, dict)
            or entry.get("source") != file_info["path"]
            or not destination.is_file()
        ):
            return False
        try:
            expected_size = int(entry.get("size", -1))
            expected_hash = str(entry.get("sha256") or "")
            stat = destination.stat()
            signature = (stat.st_size, stat.st_mtime_ns, expected_hash)
            if stat.st_size != expected_size or not expected_hash:
                self.validation_cache.pop(key, None)
                return False
            if self.validation_cache.get(key) == signature:
                return True
            valid = sha256_file(destination) == expected_hash
            if valid:
                self.validation_cache[key] = signature
            else:
                self.validation_cache.pop(key, None)
            return valid
        except (OSError, ValueError):
            return False

    def extraction_is_current(
        self, challenge_id: str, file_info: dict[str, str], deploy_dir: Path
    ) -> bool:
        key = self.manifest_key(challenge_id, file_info["local_name"])
        with self.manifest_lock:
            entry = copy.deepcopy(self.manifest.get(key))
        extraction = entry.get("extraction") if isinstance(entry, dict) else None
        if isinstance(extraction, dict) and extraction.get("status") == "blocked":
            return True  # Preserve archive; retry only after its content changes.
        if not isinstance(extraction, dict) or extraction.get("status") != "complete":
            return False
        paths = extraction.get("paths")
        if not isinstance(paths, list):
            return False
        root = deploy_dir.resolve()
        for relative in paths:
            target = (deploy_dir / str(relative)).resolve()
            if not target.is_relative_to(root) or not target.exists():
                return False
        return True

    def schedule_file(
        self,
        challenge: dict[str, object],
        file_info: dict[str, str],
        deploy_dir: Path,
        changed: bool,
    ) -> None:
        if self.stop.is_set():
            return
        key = (str(challenge["id"]), file_info["local_name"])
        with self.active_lock:
            if key in self.active_files or time.monotonic() < self.retry_files.get(
                key, 0
            ):
                return
            self.active_files.add(key)
        self.executor.submit(
            self.download_worker, challenge, file_info, deploy_dir, changed, key
        )

    def schedule_extract(
        self, challenge: dict[str, object], file_info: dict[str, str], deploy_dir: Path
    ) -> None:
        if self.stop.is_set():
            return
        key = (str(challenge["id"]), file_info["local_name"])
        with self.active_lock:
            if key in self.active_files or time.monotonic() < self.retry_files.get(
                key, 0
            ):
                return
            self.active_files.add(key)
        self.executor.submit(self.extract_worker, challenge, file_info, deploy_dir, key)

    @staticmethod
    def format_bytes(value: int) -> str:
        if value < 1024:
            return f"{value} B"
        if value < 1024 * 1024:
            return f"{value / 1024:.1f} KB"
        return f"{value / (1024 * 1024):.1f} MB"

    def download_worker(
        self,
        challenge: dict[str, object],
        file_info: dict[str, str],
        deploy_dir: Path,
        changed: bool,
        key: tuple[str, str],
    ) -> None:
        job = TransferJob(
            str(challenge["id"]),
            str(challenge["name"]),
            file_info["local_name"],
            "DOWNLOAD",
        )
        self.state.add_job(job)
        destination = deploy_dir / file_info["local_name"]
        temporary = destination.with_name(
            f".{destination.name}.rctf-part-{uuid.uuid4().hex}"
        )
        try:
            if self.stop.is_set():
                raise DownloadCancelled("download cancelled")
            with (
                self.client.open_file(file_info["url"]) as response,
                temporary.open("wb") as output,
            ):
                total = int(response.headers.get("Content-Length") or 0)
                require_space(deploy_dir, total)
                received = 0
                while True:
                    if self.stop.is_set():
                        raise DownloadCancelled("download cancelled")
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    require_space(deploy_dir, len(chunk))
                    output.write(chunk)
                    received += len(chunk)
                    progress = self.format_bytes(received) + (
                        f"/{self.format_bytes(total)}" if total else ""
                    )
                    self.state.update_job(
                        job,
                        percent=min(99, int(received * 100 / total)) if total else 0,
                        progress=progress,
                    )
                    if TEST_MODE:
                        time.sleep(0.03)
            if total and received != total:
                raise OSError(f"incomplete download: {received}/{total} bytes")
            if self.stop.is_set():
                raise DownloadCancelled("download cancelled")
            new_hash = sha256_file(temporary)
            if self.stop.is_set():
                raise DownloadCancelled("download cancelled")
            old_hash = sha256_file(destination) if destination.is_file() else ""
            manifest_key = self.manifest_key(
                str(challenge["id"]), file_info["local_name"]
            )
            if destination.is_file() and new_hash != old_hash:
                self.archive_existing(str(challenge["id"]), destination)
            if self.stop.is_set():
                raise DownloadCancelled("download cancelled")
            temporary.replace(destination)
            archive = zipfile.is_zipfile(destination) or tarfile.is_tarfile(destination)
            with self.manifest_lock:
                previous = self.manifest.get(manifest_key)
                previous_extraction = (
                    previous.get("extraction") if isinstance(previous, dict) else None
                )
                self.manifest[manifest_key] = {
                    "sha256": new_hash,
                    "size": destination.stat().st_size,
                    "source": file_info["path"],
                    "updated": iso_now(),
                    "extraction": {
                        "status": "pending",
                        "paths": list(previous_extraction.get("paths", []))
                        if isinstance(previous_extraction, dict)
                        else [],
                    }
                    if archive
                    else None,
                }
                atomic_json(MANIFEST_FILE, self.manifest)
            self.state.update_job(
                job, percent=100, progress=self.format_bytes(destination.stat().st_size)
            )
            self.state.finish_job(job)
            if archive and not self.stop.is_set():
                self.apply_extraction(challenge, file_info, destination, deploy_dir)
        except DownloadCancelled:
            self.state.finish_job(job, error="cancelled")
        except Exception as error:
            self.retry_files[key] = time.monotonic() + 60
            self.state.finish_job(job, error=str(error))
            self.add_event(
                "ERROR", f"{challenge['name']} / {file_info['local_name']}: {error}"
            )
        finally:
            if temporary.exists():
                temporary.unlink()
            with self.active_lock:
                self.active_files.discard(key)

    def extract_worker(
        self,
        challenge: dict[str, object],
        file_info: dict[str, str],
        deploy_dir: Path,
        key: tuple[str, str],
    ) -> None:
        try:
            archive = deploy_dir / file_info["local_name"]
            if not self.stop.is_set():
                self.apply_extraction(challenge, file_info, archive, deploy_dir)
        finally:
            with self.active_lock:
                self.active_files.discard(key)

    def apply_extraction(self, *args):
        with self.extraction_lock:
            return self._apply_extraction(*args)

    def _apply_extraction(
        self,
        challenge: dict[str, object],
        file_info: dict[str, str],
        archive: Path,
        deploy_dir: Path,
    ) -> None:
        job = TransferJob(
            str(challenge["id"]), str(challenge["name"]), archive.name, "EXTRACT"
        )
        self.state.add_job(job)
        transaction = STAGING_DIR / job.job_id
        payload = transaction / "payload"
        manifest_key = self.manifest_key(str(challenge["id"]), file_info["local_name"])

        def record_failure(message) -> None:
            with self.manifest_lock:
                entry = self.manifest.get(manifest_key)
                if isinstance(entry, dict):
                    previous = entry.get("extraction")
                    entry["extraction"] = {
                        "status": "blocked"
                        if isinstance(message, ValueError)
                        else "error",
                        "error": str(message),
                        "paths": previous.get("paths", [])
                        if isinstance(previous, dict)
                        else [],
                    }
                    try:
                        atomic_json(MANIFEST_FILE, self.manifest)
                    except OSError:
                        pass  # Job/event still reports failure if the disk is unavailable.

        try:
            payload.mkdir(parents=True, exist_ok=True)
            with self.manifest_lock:
                entry = self.manifest.get(manifest_key)
                if not isinstance(entry, dict):
                    raise RuntimeError("archive manifest entry disappeared")
                previous = entry.get("extraction")
                entry["extraction"] = {
                    "status": "pending",
                    "paths": previous.get("paths", [])
                    if isinstance(previous, dict)
                    else [],
                }
                atomic_json(MANIFEST_FILE, self.manifest)
            extract(archive, payload, cancelled=self.stop.is_set)
            paths: list[str] = []
            deploy_root = deploy_dir.resolve()
            sources = [
                source for source in sorted(payload.rglob("*")) if source.is_file()
            ]
            # Validate the entire publication plan before replacing any file.
            protected = {archive.resolve()} | {
                (deploy_dir / str(item["local_name"])).resolve()
                for item in challenge.get("files", [])
                if isinstance(item, dict) and "local_name" in item
            }
            for source in sources:
                destination = deploy_dir / source.relative_to(payload)
                for part in (destination, *destination.parents):
                    if part.is_symlink():
                        raise ValueError(
                            "extraction destination contains a symbolic link"
                        )
                    if part == deploy_dir:
                        break
                resolved = destination.resolve()
                if not resolved.is_relative_to(deploy_root) or resolved in protected:
                    raise ValueError(
                        "extraction would escape deploy or overwrite an attachment"
                    )
                if destination.exists() and not destination.is_file():
                    raise ValueError("extraction destination is not a regular file")
                for parent in destination.parents:
                    if parent == deploy_dir:
                        break
                    if parent.exists() and not parent.is_dir():
                        raise ValueError("extraction parent is not a directory")
            for source in sources:
                if not source.is_file():
                    continue
                if self.stop.is_set():
                    raise DownloadCancelled("extraction cancelled")
                relative = source.relative_to(payload)
                destination = (deploy_dir / relative).resolve()
                if not destination.is_relative_to(deploy_root):
                    raise ValueError("extraction destination escaped deploy")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    self.archive_existing(str(challenge["id"]), destination)
                temporary = destination.with_name(
                    f".{destination.name}.rctf-part-{uuid.uuid4().hex}"
                )
                try:
                    shutil.copy2(source, temporary)
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
                paths.append(relative.as_posix())

            if self.stop.is_set():
                raise DownloadCancelled("extraction cancelled")

            with self.manifest_lock:
                entry = self.manifest.get(manifest_key)
                if not isinstance(entry, dict):
                    raise RuntimeError("archive manifest entry disappeared")
                old_extraction = entry.get("extraction")
                old_paths = (
                    set(old_extraction.get("paths", []))
                    if isinstance(old_extraction, dict)
                    else set()
                )
                claimed_elsewhere: set[str] = set()
                for other_key, other_entry in self.manifest.items():
                    if other_key == manifest_key or not isinstance(other_entry, dict):
                        continue
                    other_extraction = other_entry.get("extraction")
                    if isinstance(other_extraction, dict):
                        claimed_elsewhere.update(
                            str(path) for path in other_extraction.get("paths", [])
                        )
                for stale in old_paths - set(paths) - claimed_elsewhere:
                    target = (deploy_dir / str(stale)).resolve()
                    if target.is_relative_to(deploy_root) and (
                        target.is_file() or target.is_symlink()
                    ):
                        self.archive_existing(str(challenge["id"]), target)
                        target.unlink(missing_ok=True)
                entry["extraction"] = {
                    "status": "complete",
                    "paths": paths,
                    "updated": iso_now(),
                }
                atomic_json(MANIFEST_FILE, self.manifest)
            self.state.finish_job(job)
        except DownloadCancelled:
            record_failure("cancelled")
            self.state.finish_job(job, error="cancelled")
        except Exception as error:
            self.retry_files[(str(challenge["id"]), file_info["local_name"])] = (
                time.monotonic() + 60
            )
            record_failure(error)
            self.state.finish_job(job, error=str(error))
            self.add_event(
                "ERROR", f"extract {challenge['name']} / {archive.name}: {error}"
            )
        finally:
            shutil.rmtree(transaction, ignore_errors=True)

    @staticmethod
    def scoreboard_team(
        rows: list[dict[str, object]], team: dict[str, object]
    ) -> dict[str, object] | None:
        identifier = team.get("id")
        if identifier is not None:
            matched = next(
                (
                    row
                    for row in rows
                    if str(row.get("account_id", row.get("id"))) == str(identifier)
                ),
                None,
            )
            if matched:
                return matched
            rows = [row for row in rows if row.get("account_id", row.get("id")) is None]
        return next(
            (
                row
                for row in rows
                if team.get("name") and row.get("name") == team["name"]
            ),
            None,
        )

    def __init__(self, state, stop):
        self.state, self.stop = state, stop
        self.client = RCTFClient(BASE_URL, TOKEN)
        self.previous = read_json(CURRENT_FILE, {"challenges": {}, "team": {}})
        self.manifest = read_json(MANIFEST_FILE, {})
        self.helpers = self.find_helpers()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self.active_files = set()
        self.retry_files = {}
        self.extraction_lock = threading.RLock()
        self.active_lock = threading.Lock()
        self.manifest_lock = threading.RLock()
        self.validation_cache = {}
        self.event_lock = threading.RLock()
        self.event_ids = set()
        self.codex_usage_cache = read_json(CODEX_USAGE_FILE, {})
        self.last_config_refresh = 0.0
        self.last_board_refresh = 0.0
        self.board_cache = []
        self.config = {}
        self.load_events()
        self.cleanup_interrupted_work()
        with state.lock:
            state.challenges = copy.deepcopy(self.previous.get("challenges", {}))
            team = self.previous.get("team", {})
            state.team_name = str(team.get("name") or "-")
            state.account_id = team.get("id")
            state.score = team.get("score", 0)
            state.rank = team.get("globalPlace") or "-"

    def normalize_files(self, challenge):
        entries = list(challenge.get("files") or [])
        # Some deployments use Markdown download links instead of the native files field.
        description = str(challenge.get("description") or "")
        extensions = (
            ".zip",
            ".tar",
            ".gz",
            ".tgz",
            ".bz2",
            ".xz",
            ".7z",
            ".rar",
            ".pdf",
            ".txt",
            ".pcap",
            ".pcapng",
            ".py",
            ".c",
            ".bin",
            ".elf",
            ".exe",
        )
        for label, url in re.findall(
            r"\[([^\]]+)\]\((https?://[^\s)]+)\)", description
        ):
            if urllib.parse.urlsplit(url).path.lower().endswith(extensions):
                entries.append({"url": url})
        output, urls, names = [], set(), set()
        for index, entry in enumerate(entries):
            url = (
                entry
                if isinstance(entry, str)
                else entry.get("url")
                if isinstance(entry, dict)
                else None
            )
            if not isinstance(url, str) or not url:
                continue
            url = urllib.parse.urljoin(BASE_URL + "/", url)
            parsed = urllib.parse.urlsplit(url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                continue
            if url in urls:
                continue
            urls.add(url)
            name = safe_file_name(
                str(entry.get("name") or parsed.path)
                if isinstance(entry, dict)
                else parsed.path,
                f"attachment_{index + 1}",
            )
            local_name, suffix = name, 2
            while local_name in names:
                local_name = f"{Path(name).stem}_{suffix}{Path(name).suffix}"
                suffix += 1
            names.add(local_name)
            # Storage hostname + path is stable across signed URL renewals.
            identity = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, "", "")
            )
            output.append(
                {"url": url, "path": identity, "name": name, "local_name": local_name}
            )
        return output

    @staticmethod
    def check_destination(path):
        root = BASE_DIR.resolve()
        if not path.resolve().is_relative_to(root):
            raise ValueError("Workspace path escapes root")
        for item in (path, *path.parents):
            if item.is_symlink():
                raise ValueError("Workspace destination contains a symbolic link")
            if item == BASE_DIR:
                break

    def prepare_challenge(self, challenge):
        folder = BASE_DIR / challenge["directory"]
        deploy = folder / "chall/deploy"
        self.check_destination(deploy)
        deploy.mkdir(parents=True, exist_ok=True)
        for name, source in self.helpers.items():
            destination = folder / name
            self.check_destination(destination)
            if not destination.exists():
                shutil.copy2(source, destination)
                destination.chmod(destination.stat().st_mode | 0o100)
        description = folder / "chall/README.md"
        self.check_destination(description)
        content = f"# {challenge['name']}\n\n{challenge['description']}\n"
        if not description.exists() or description.read_text() != content:
            self.archive_existing(challenge["id"], description)
            temp = description.with_name(".README.md.rctf-part-" + uuid.uuid4().hex)
            temp.write_text(content)
            temp.replace(description)
        instructions = folder / "chall/AGENTS.md"
        self.check_destination(instructions)
        if not instructions.exists():
            instructions.write_text(DEFAULT_AGENTS)
        for entry in challenge["files"]:
            self.check_destination(deploy / entry["local_name"])
        return deploy

    def refresh_config(self):
        if time.monotonic() - self.last_config_refresh < 60 and self.config:
            return
        config = self.client.request_json(
            "/api/v1/integrations/client/config", authenticated=False
        )
        if not isinstance(config, dict):
            raise ApiError("invalid_config")
        self.config = {k: config.get(k) for k in ("ctfName", "startTime", "endTime")}
        end = config.get("endTime")
        self.state.contest_end = (
            datetime.fromtimestamp(float(end) / 1000, timezone.utc).isoformat()
            if end
            else ""
        )
        self.state.contest_end_known = "endTime" in config
        self.last_config_refresh = time.monotonic()

    def fetch_team(self):
        raw = self.client.request_json("/api/v1/users/me")
        if (
            not isinstance(raw, dict)
            or not raw.get("id")
            or not isinstance(raw.get("solves"), list)
        ):
            raise ApiError("invalid_team")
        # /users/me contains teamToken and email. Never persist the raw response.
        return {
            key: raw.get(key)
            for key in (
                "id",
                "name",
                "score",
                "globalPlace",
                "divisionPlace",
                "division",
                "solves",
            )
        }

    def fetch_scoreboard(self, team):
        now = time.monotonic()
        if now - self.last_board_refresh >= 30 or not self.board_cache:
            raw = self.client.request_json(
                "/api/v1/leaderboard/now?limit=5&offset=0", authenticated=False
            )
            if not isinstance(raw, dict) or not isinstance(
                raw.get("leaderboard"), list
            ):
                raise ApiError("invalid_leaderboard")
            rows = []
            for index, item in enumerate(raw["leaderboard"]):
                row = {key: item.get(key) for key in ("id", "name", "score")}
                row["pos"] = index + 1
                if str(row["id"]) != str(team["id"]):
                    try:
                        public = self.client.request_json(
                            "/api/v1/users/"
                            + urllib.parse.quote(str(row["id"]), safe=""),
                            authenticated=False,
                        )
                        row["solved_challenge_ids"] = [
                            str(s["id"]) for s in public["solves"]
                        ]
                    except (ApiError, KeyError, TypeError):
                        row["solved_challenge_ids"] = None
                rows.append(row)
            self.board_cache = rows
            self.last_board_refresh = now
        rows = copy.deepcopy(self.board_cache)
        own = self.scoreboard_team(rows, team)
        if own is None:
            own = {"id": team["id"]}
            rows.append(own)
        own.update(
            {
                "name": team["name"],
                "score": team["score"],
                "pos": team.get("globalPlace"),
                "solved_challenge_ids": [str(s["id"]) for s in team["solves"]],
            }
        )
        return rows

    def fetch_snapshot(self):
        team = self.fetch_team()
        with self.state.lock:
            self.state.team_name = str(team["name"])
            self.state.account_id = team["id"]
            self.state.score = team.get("score") or 0
            self.state.rank = team.get("globalPlace") or "-"
        raw = self.client.request_json("/api/v1/challs")
        if not isinstance(raw, list):
            raise ApiError("invalid_challenges")
        solved = {str(item["id"]) for item in team["solves"]}
        result = {}
        for item in raw:
            if not isinstance(item, dict) or not item.get("id"):
                raise ApiError("invalid_challenge")
            identifier = str(item["id"])
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier)
                or identifier in result
            ):
                raise ApiError("invalid_challenge_id")
            title = str(item.get("name") or identifier)
            old = self.previous.get("challenges", {}).get(identifier, {})
            result[identifier] = {
                "id": identifier,
                "name": title,
                "category": str(item.get("category") or "unknown"),
                "value": item.get("points", 0),
                "solved": identifier in solved,
                "description": str(item.get("description") or ""),
                "files": self.normalize_files(item),
                "directory": old.get("directory")
                or identifier
                + "_"
                + safe_directory_name(title)
                .encode("utf-8")[:180]
                .decode("utf-8", errors="ignore"),
            }
        return {"team": team, "challenges": result}

    def process_snapshot(self, current):
        old = self.previous.get("challenges", {})
        new = current["challenges"]
        for identifier in old.keys() - new.keys():
            self.add_event("DELETED", f"{old[identifier]['name']} removed", identifier)
        for identifier, challenge in new.items():
            if self.stop.is_set():
                return
            prior = old.get(identifier)
            deploy = self.prepare_challenge(challenge)
            if not prior:
                self.add_event(
                    "NEW",
                    f"{challenge['name']} released",
                    identifier,
                    event_id="new:" + identifier,
                )
            else:
                if challenge["solved"] and not prior["solved"]:
                    self.add_event(
                        "SOLVED",
                        f"{challenge['category']} / {challenge['name']}",
                        identifier,
                    )
                if any(
                    prior.get(k) != challenge.get(k)
                    for k in ("name", "category", "description")
                ) or [(f["name"], f["path"]) for f in prior["files"]] != [
                    (f["name"], f["path"]) for f in challenge["files"]
                ]:
                    self.add_event(
                        "UPDATED", f"{challenge['name']} changed", identifier
                    )
            for file in challenge["files"]:
                destination = deploy / file["local_name"]
                if not self.local_file_is_current(identifier, file, destination):
                    self.schedule_file(challenge, file, deploy, destination.exists())
                elif (
                    zipfile.is_zipfile(destination) or tarfile.is_tarfile(destination)
                ) and not self.extraction_is_current(identifier, file, deploy):
                    self.schedule_extract(challenge, file, deploy)
        team = current["team"]
        try:
            scoreboard = self.fetch_scoreboard(team)
        except ApiError as error:
            scoreboard = [
                {
                    "id": team["id"],
                    "name": team["name"],
                    "score": team["score"],
                    "pos": team["globalPlace"],
                    "solved_challenge_ids": [str(s["id"]) for s in team["solves"]],
                }
            ]
            self.add_event("ERROR", "Leaderboard unavailable: " + str(error))
        display = copy.deepcopy(new)
        for identifier, challenge in display.items():
            marker = self.marker_for(identifier)
            challenge.update(
                marker=marker[0] if marker else None,
                marker_expires=marker[1] if marker else None,
            )
        with self.state.lock:
            ended = (
                self.config.get("endTime")
                and float(self.config["endTime"]) / 1000 <= time.time()
            )
            self.state.status = "ENDED" if ended else "LIVE"
            self.state.team_name = str(team["name"])
            self.state.account_id = team["id"]
            self.state.score = team.get("score") or 0
            self.state.rank = team.get("globalPlace") or "-"
            self.state.scoreboard = scoreboard
            self.state.challenges = display
        if current != self.previous:
            atomic_json(CURRENT_FILE, current)
        self.previous = copy.deepcopy(current)

    def run(self):
        failures = 0
        try:
            while not self.stop.is_set():
                try:
                    self.refresh_config()
                    current = self.fetch_snapshot()
                    self.process_snapshot(current)
                    failures = 0
                except ApiError as error:
                    failures = (
                        0
                        if error.code in ("badNotStarted", "badEnded")
                        else failures + 1
                    )
                    with self.state.lock:
                        self.state.status = {
                            "badNotStarted": "PRE-START",
                            "badEnded": "ENDED",
                        }.get(error.code, "STALE")
                    self.add_event("ERROR", str(error))
                except Exception as error:
                    failures += 1
                    with self.state.lock:
                        self.state.status = "STALE"
                    self.add_event(
                        "ERROR", f"Local processing failed: {type(error).__name__}"
                    )
                self.stop.wait(
                    max(
                        self.client.retry_until - time.monotonic(),
                        min(120, POLL_INTERVAL * 2 ** min(failures, 4)),
                    )
                )
        finally:
            self.executor.shutdown(wait=True, cancel_futures=True)


def marker_badge(challenge: dict[str, object]) -> tuple[str, str] | None:
    kind = challenge.get("marker")
    expires = float(challenge.get("marker_expires") or 0)
    remaining = max(0, int(math.ceil(expires - time.time())))
    if not kind or remaining <= 0:
        return None
    return str(kind), CYAN if kind == "NEW" else YELLOW


def problem_line(challenge: dict[str, object], width: int) -> str:
    marker = marker_badge(challenge)
    prefix = paint(f"[{marker[0]}]", marker[1], bold=True) + " " if marker else ""
    score = paint(str(challenge["value"]) + " pt", GRAY)
    score_width = display_width(score)
    left_width = max(1, width - score_width - 1)
    left = truncate(f"  {prefix}{challenge['name']}", left_width)
    return f"{pad(left, left_width)} {score}"


def challenge_lines(challenges: list[dict[str, object]], width: int) -> list[str]:
    groups: dict[str, list[dict[str, object]]] = {}
    for challenge in challenges:
        groups.setdefault(str(challenge.get("category") or "unknown"), []).append(
            challenge
        )
    lines: list[str] = []
    for group_index, group_name in enumerate(sorted(groups, key=str.casefold)):
        problems = sorted(
            groups[group_name], key=lambda value: str(value["name"]).casefold()
        )
        if group_index:
            lines.append("")
        lines.append(paint(f"{group_name.upper()} ({len(problems)})", WHITE, bold=True))
        lines.extend(problem_line(challenge, width) for challenge in problems)
    return lines


def render_challenges(
    view: dict[str, object],
    left_width: int,
    right_width: int,
    *,
    max_rows: int | None = None,
) -> list[str]:
    challenges = [challenge for challenge in view["challenges"].values()]
    unsolved = [challenge for challenge in challenges if not challenge.get("solved")]
    solved = [challenge for challenge in challenges if challenge.get("solved")]
    if view["status"] == "PRE-START":
        left = boxed(
            "UNSOLVED  0",
            ["", paint("Competition has not started.", YELLOW, bold=True), "", ""],
            left_width,
            RED,
        )
        right = boxed("SOLVED  0", ["", "", "", ""], right_width, GREEN)
    else:
        left = boxed(
            f"UNSOLVED  {len(unsolved)}",
            challenge_lines(unsolved, left_width - 4),
            left_width,
            RED,
        )
        right = boxed(
            f"SOLVED  {len(solved)}",
            challenge_lines(solved, right_width - 4),
            right_width,
            GREEN,
        )
    if max_rows is not None:
        left = fit_panel(left, max_rows, left_width, RED)
        right = fit_panel(right, max_rows, right_width, GREEN)
    rows = max(len(left), len(right))
    left += [" " * left_width] * (rows - len(left))
    right += [" " * right_width] * (rows - len(right))
    return [pad(left[index], left_width) + "  " + right[index] for index in range(rows)]


def comparison_position(row: dict[str, object], fallback: int) -> int:
    try:
        return int(row.get("pos") or fallback)
    except (TypeError, ValueError):
        return fallback


def comparison_status_cell(ours: bool, theirs: bool) -> str:
    if ours and theirs:
        return paint(f"{'✓':^5}", GRAY)
    if theirs:
        return f"{RED_BG}{COMPARISON_RED}{'✓':^5}{RESET}"
    if ours:
        return f"{GREEN_BG}{COMPARISON_GREEN}{'×':^5}{RESET}"
    return paint(f"{'.':^5}", GRAY)


def own_team_cell(text: str) -> str:
    return OWN_TEAM_BG + text.replace(RESET, RESET + OWN_TEAM_BG) + RESET


def render_team_comparison(view: dict[str, object], width: int) -> list[str]:
    rows = [row for row in view.get("scoreboard", []) if isinstance(row, dict)]
    own = Monitor.scoreboard_team(
        rows, {"id": view.get("account_id"), "name": view.get("team_name")}
    )
    title = "TEAM COMPARISON"
    if not own:
        return boxed(title, [paint("WAITING", YELLOW, bold=True)], width, BLUE)
    opponents = sorted(
        (row for row in rows if row is not own),
        key=lambda row: (
            comparison_position(row, len(rows) + 1),
            str(row.get("name") or "").casefold(),
        ),
    )[:4]
    if not opponents:
        return boxed(title, [paint("NO OPPONENTS", YELLOW, bold=True)], width, BLUE)
    displayed = sorted(
        [*opponents, own],
        key=lambda row: (
            comparison_position(row, len(rows) + 1),
            str(row.get("name") or "").casefold(),
        ),
    )

    challenges = sorted(
        (
            (
                str(challenge.get("id") or key),
                str(challenge.get("name") or f"Challenge {key}"),
            )
            for key, challenge in view.get("challenges", {}).items()
            if isinstance(challenge, dict)
        ),
        key=lambda item: item[1].casefold(),
    )
    own_total = float(own.get("score") or 0)
    left_lines = [f"{'RK TEAM':<16}{'ΔPT':>8}", paint("─" * 25, GRAY)]
    for row in displayed:
        total = float(row.get("score") or 0)
        rank = str(row["pos"]) if row.get("pos") else "-"
        line = f"#{rank:<2} {pad(truncate(str(row.get('name') or 'Team'), 12), 12)} {total - own_total:+7.0f}"
        left_lines.append(own_team_cell(line) if row is own else line)

    own_ids = own.get("solved_challenge_ids")
    own_solved = (
        {str(value) for value in own_ids}
        if isinstance(own_ids, list)
        else {
            str(challenge.get("id") or key)
            for key, challenge in view.get("challenges", {}).items()
            if isinstance(challenge, dict) and challenge.get("solved")
        }
    )
    solved_by_opponent = [
        {str(value) for value in row["solved_challenge_ids"]}
        if isinstance(row.get("solved_challenge_ids"), list)
        else None
        for row in opponents
    ]
    visible = [
        (number, name)
        for number, name in challenges
        if not (
            number in own_solved
            and all(ids is not None and number in ids for ids in solved_by_opponent)
        )
    ]
    headers = [
        "US" if row is own else f"#{comparison_position(row, len(rows) + 1)}"
        for row in displayed
    ]
    content_width = max(1, width - 4)
    preferred_left = 28
    minimum_right = 18 + 5 * len(headers)
    side_by_side = content_width >= preferred_left + 3 + minimum_right
    left_width = preferred_left if side_by_side else content_width
    right_width = content_width - left_width - 3 if side_by_side else content_width
    name_width = max(1, right_width - 5 * len(headers))
    right_lines = []
    heading = "JEOPARDY CHALLENGE"
    if name_width < display_width(heading):
        right_lines.append(heading)
        heading = "CHALLENGE"
    right_lines.extend(
        [
            pad(truncate(heading, name_width), name_width)
            + "".join(
                own_team_cell(f"{header:^5}") if header == "US" else f"{header:^5}"
                for header in headers
            ),
            paint("─" * right_width, GRAY),
        ]
    )
    for number, name in visible:
        cells = []
        for row in displayed:
            if row is own:
                cells.append(own_team_cell(f"{'✓' if number in own_solved else '':^5}"))
            else:
                ids = solved_by_opponent[
                    next(
                        index
                        for index, opponent in enumerate(opponents)
                        if opponent is row
                    )
                ]
                cells.append(
                    comparison_status_cell(number in own_solved, number in ids)
                    if ids is not None
                    else paint(f"{'?':^5}", YELLOW)
                )
        right_lines.append(pad(truncate(name, name_width), name_width) + "".join(cells))
    if any(ids is None for ids in solved_by_opponent):
        right_lines.append(paint("? = solves unavailable", YELLOW))
    if side_by_side:
        lines = []
        for index in range(max(len(left_lines), len(right_lines))):
            left = left_lines[index] if index < len(left_lines) else ""
            right = right_lines[index] if index < len(right_lines) else ""
            lines.append(
                pad(truncate(left, left_width), left_width)
                + paint(" │ ", GRAY)
                + pad(truncate(right, right_width), right_width)
            )
    else:
        lines = left_lines + [""] + right_lines
    return boxed(title, lines, width, BLUE)


def competition_countdown(view: dict[str, object]) -> tuple[str, str]:
    value = str(view.get("contest_end") or "")
    if not value:
        if view.get("contest_end_known"):
            return "NO LIMIT", GRAY
        return "WAITING", YELLOW
    try:
        end = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if end.tzinfo is None:
            return "UNAVAILABLE", RED
        remaining = max(
            0, math.ceil((end - datetime.now(timezone.utc)).total_seconds())
        )
    except ValueError:
        return "UNAVAILABLE", RED
    if remaining <= 0:
        return "ENDED", RED
    days, remainder = divmod(remaining, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
        if days
        else f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    ), GREEN


def transfer_line(job: TransferJob, width: int) -> str:
    if width < 80:
        content_width = width - 4
        bar_width = 8
        action = "EXTRACT" if job.action == "EXTRACT" else "DOWNLOAD"
        tail = (
            "ERROR"
            if job.state == "ERROR"
            else "DONE"
            if job.state == "DONE"
            else f"{job.percent:3d}%"
        )
        path_width = max(8, content_width - 1 - 8 - 1 - bar_width - 1 - 5)
        filled = int(bar_width * max(0, min(100, job.percent)) / 100)
        action_color = YELLOW if job.action == "EXTRACT" else CYAN
        bar_color = (
            RED
            if job.state == "ERROR"
            else GREEN
            if job.state == "DONE"
            else action_color
        )
        bar = paint("█" * filled, bar_color) + paint("░" * (bar_width - filled), GRAY)
        path = pad(
            truncate(f"{job.challenge} / {job.filename}", path_width), path_width
        )
        tail_color = (
            RED if job.state == "ERROR" else GREEN if job.state == "DONE" else WHITE
        )
        return f"{path} {paint(action.ljust(8), action_color, bold=True)} {bar} {paint(tail.rjust(5), tail_color, bold=job.state != 'ACTIVE')}"
    path_width = max(24, min(42, width - 55))
    bar_width = max(8, min(18, width - path_width - 47))
    filled = int(bar_width * max(0, min(100, job.percent)) / 100)
    action_color = YELLOW if job.action == "EXTRACT" else CYAN
    bar_color = GREEN if job.state == "DONE" else action_color
    bar = paint("█" * filled, bar_color) + paint("░" * (bar_width - filled), GRAY)
    path = pad(truncate(f"{job.challenge} / {job.filename}", path_width), path_width)
    if job.state == "DONE" and job.done_at is not None:
        remaining = max(1, int(math.ceil(DONE_TTL - (time.monotonic() - job.done_at))))
        state = paint(f"DONE {remaining}s", GREEN, bold=True)
    elif job.state == "ERROR":
        state = paint("ERROR", RED, bold=True)
    else:
        state = job.progress
    return f"{path} {paint(job.action.ljust(8), action_color, bold=True)} {bar} {job.percent:3d}%  {state}"


def render_transfers(view: dict[str, object], width: int) -> list[str]:
    jobs = sorted(
        view["jobs"],
        key=lambda job: (
            job.challenge.casefold(),
            job.filename.casefold(),
            0 if job.action == "DOWNLOAD" else 1,
            job.created_at,
        ),
    )
    if jobs:
        active = sum(job.state == "ACTIVE" for job in jobs)
        title = f"DOWNLOAD / EXTRACT · {len(jobs)} JOBS · {active} ACTIVE"
        lines = [transfer_line(job, width) for job in jobs]
    elif view["status"] == "PRE-START":
        title, lines = "DOWNLOAD / EXTRACT", [paint("WAITING", YELLOW)]
    else:
        return []
    return boxed(title, lines, width, BLUE)


def render_events(view: dict[str, object], width: int, *, limit: int = 4) -> list[str]:
    color_map = {
        "NEW": CYAN,
        "UPDATED": YELLOW,
        "SOLVED": GREEN,
        "DELETED": RED,
        "ERROR": RED,
    }
    lines: list[str] = []
    for event in reversed(view["events"][-max(0, limit) :] if limit else []):
        try:
            moment = datetime.fromisoformat(str(event["time"])).strftime("%H:%M:%S")
        except ValueError:
            moment = "--:--:--"
        kind = str(event.get("kind") or "INFO")
        lines.append(
            f"{moment}  {paint(f'[{kind}]', color_map.get(kind, WHITE), bold=True)} {event.get('message', '')}"
        )
    return boxed("RECENT EVENTS", lines, width, YELLOW)


def resource_series(
    resources: dict[str, object], value_index: int, width: int
) -> list[float | None]:
    if width <= 0:
        return []
    timestamp = resources.get("now")
    now = time.monotonic() if timestamp is None else float(timestamp)
    # Use absolute integer bucket IDs for both samples and frames. Subtracting
    # a moving floating-point start made boundary samples jump between cells.
    end = math.floor(now * width / RESOURCE_HISTORY_SECONDS)
    start = end - width
    values: list[float | None] = [None] * width
    first_new = 0
    cache = resources.get("plot_cache")
    key = (value_index, width)
    previous = cache.get(key) if isinstance(cache, dict) else None
    if previous is not None:
        previous_now, previous_end, previous_values = previous
        shift = end - previous_end
        if now >= previous_now and 0 <= shift < width:
            values = list(previous_values[shift:]) + [None] * shift
            first_new = width - shift
    cutoff = now - RESOURCE_RETENTION_SECONDS
    for sample in resources.get("samples", []):
        if not isinstance(sample, (tuple, list)) or len(sample) <= value_index:
            continue
        moment = float(sample[0])
        if moment < cutoff or moment > now:
            continue
        index = math.floor(moment * width / RESOURCE_HISTORY_SECONDS) - start
        # Only fill newly completed cells. In particular, never recalculate
        # the leftmost cell from the partially expired raw samples.
        if not first_new <= index < width:
            continue
        value = float(sample[value_index])
        values[index] = value if values[index] is None else max(values[index], value)
    if isinstance(cache, dict):
        cache.pop(key, None)
        cache[key] = (now, end, tuple(values))
        # Resizing and compact/full layouts must not grow the cache forever.
        while len(cache) > 32:
            del cache[next(iter(cache))]
    return values


def resource_chart(
    title: str,
    current: float,
    peak: float | None,
    values: list[float | None],
    width: int,
    color: str,
    detail: str = "",
) -> list[str]:
    height = 5
    plot_width = max(8, width - 6)
    values = values[-plot_width:]
    if len(values) < plot_width:
        values = [None] * (plot_width - len(values)) + values
    suffix = f"   {detail}" if detail else ""
    peak_text = f"{peak:>5.1f}%" if peak is not None else "    --"
    lines = [f"{title:<8}{current:>5.1f}%   Peak {peak_text}{suffix}"]
    # Each tick is at its terminal cell's centre: the 40% cell spans
    # 30..50%, so 40% is half a block and 50% fills that cell.
    for row in range(height):
        threshold = 100 - row * 20
        cells: list[str] = []
        for value in values:
            if value is None:
                cells.append(" ")
                continue
            value = max(0.0, min(100.0, value))
            units = round((value - (threshold - 10)) * 8 / 20)
            cells.append(PARTIAL_BLOCKS[max(0, min(8, units))])
        lines.append(f"{threshold:>3}% ┤" + paint("".join(cells), color))
    # The zero axis also runs through the cell centre. Only its upper half
    # may be filled; ordinary lower-block glyphs would extend below zero.
    baseline = []
    for value in values:
        if value is None or value <= 0:
            baseline.append(paint("─", GRAY))
        else:
            baseline.append(paint("⠒" if value < 7.5 else "▀", color))
    lines.append("  0% └" + "".join(baseline))
    left_label, right_label = "2m ago", "now"
    lines.append(
        "      "
        + left_label
        + " " * max(1, plot_width - len(left_label) - len(right_label))
        + right_label
    )
    return [pad(truncate(line, width), width) for line in lines]


def codex_usage_line(usage: dict[str, object], width: int) -> str:
    if usage.get("status") not in {"LIVE", "ERROR"} or not isinstance(
        usage.get("remaining_percent"), (int, float)
    ):
        if usage.get("status") == "ERROR":
            return f"CODEX WEEKLY  {paint('UNAVAILABLE', RED, bold=True)}"
        return f"CODEX WEEKLY  {paint('WAITING', YELLOW, bold=True)}"
    remaining = max(0.0, min(100.0, float(usage["remaining_percent"])))
    remaining_text = f"{remaining:.0f}%"
    show_credits = remaining_text == "0%"
    credits_text = ""
    if show_credits:
        if usage.get("credits_unlimited"):
            credits = "UNLIMITED"
        else:
            try:
                current_credits = (
                    f"{float(str(usage.get('credits_balance') or '0')):,.2f}"
                )
            except ValueError:
                current_credits = "--"
            try:
                start_credits = (
                    f"{float(str(usage.get('competition_start_credits') or '0')):,.2f}"
                )
            except ValueError:
                start_credits = "--"
            credits = f"{start_credits} → {current_credits}"
        credits_text = f"  CREDITS {credits}"
    suffix = "  STALE" if usage.get("status") == "ERROR" else ""
    fixed = f"CODEX WEEKLY  {remaining_text} REMAINING{credits_text}{suffix}"
    bar_width = max(10, min(30, width - display_width(fixed) - 5))
    filled = round(bar_width * remaining / 100)
    bar = paint("█" * filled, GREEN) + paint("░" * (bar_width - filled), GRAY)
    return f"CODEX WEEKLY  {remaining_text} REMAINING  {bar}{credits_text}{paint(suffix, RED, bold=True) if suffix else ''}"


def render_resources(
    resources: dict[str, object],
    width: int,
    codex_usage: dict[str, object] | None = None,
) -> list[str]:
    gap = 2
    body_width = width - 4
    left_width = (body_width - gap) // 2
    right_width = body_width - gap - left_width
    # An odd terminal width gives one column an extra cell. Use equal plot
    # widths, hence equal time buckets, and leave the spare cell as padding.
    chart_width = min(left_width, right_width)
    plot_width = max(8, chart_width - 6)
    cpu_values = resource_series(resources, 1, plot_width)
    memory_values = resource_series(resources, 2, plot_width)
    cpu = float(resources.get("cpu") or 0.0)
    memory = float(resources.get("memory") or 0.0)
    used_gib = float(resources.get("used_gib") or 0.0)
    total_gib = float(resources.get("total_gib") or 0.0)
    cpu_cores = int(resources.get("cpu_cores") or 1)
    left = resource_chart(
        "CPU",
        cpu,
        max((v for v in cpu_values if v is not None), default=None),
        cpu_values,
        chart_width,
        YELLOW,
        f"{cpu_cores} CORES",
    )
    right = resource_chart(
        "MEMORY",
        memory,
        max((v for v in memory_values if v is not None), default=None),
        memory_values,
        chart_width,
        CYAN,
        f"{used_gib:.2f} / {total_gib:.2f} GiB",
    )
    chart_lines = [
        pad(left[index], left_width) + " " * gap + pad(right[index], right_width)
        for index in range(len(left))
    ]
    return boxed(
        "SYSTEM RESOURCES",
        (
            [codex_usage_line(codex_usage or {}, width - 4), ""]
            if (codex_usage or {}).get("status") in {"WAITING", "LIVE", "ERROR"}
            else []
        )
        + chart_lines,
        width,
        YELLOW,
    )


def render_resources_compact(
    resources: dict[str, object],
    width: int,
    codex_usage: dict[str, object] | None = None,
) -> list[str]:
    lines = []
    for label, key, index, color in (
        ("CPU", "cpu", 1, YELLOW),
        ("MEMORY", "memory", 2, CYAN),
    ):
        prefix = f"{label:<6} {float(resources.get(key) or 0):5.1f}% "
        values = resource_series(
            resources, index, max(1, width - 4 - display_width(prefix))
        )
        graph = "".join(
            "·"
            if value is None
            else PARTIAL_BLOCKS[max(1, min(8, round(value * 8 / 100)))]
            for value in values
        )
        lines.append(prefix + paint(graph, color))
    return boxed(
        "SYSTEM RESOURCES · 2m",
        (
            [codex_usage_line(codex_usage or {}, width - 4)]
            if (codex_usage or {}).get("status") in {"WAITING", "LIVE", "ERROR"}
            else []
        )
        + lines,
        width,
        YELLOW,
    )


def render_screen(
    view: dict[str, object], resources: dict[str, object] | None = None
) -> str:
    terminal = shutil.get_terminal_size((120, 40))
    width = max(32, min(terminal.columns, 220))
    height = max(1, terminal.lines)
    left_width = (width - 2) // 2
    right_width = width - 2 - left_width
    status_color = (
        GREEN
        if view["status"] == "LIVE"
        else YELLOW
        if view["status"] == "PRE-START"
        else RED
    )
    challenges = {key: challenge for key, challenge in view["challenges"].items()}
    solved = sum(bool(challenge.get("solved")) for challenge in challenges.values())
    try:
        score_text = f"{float(view['score']):.2f}"
    except (TypeError, ValueError):
        score_text = str(view.get("score") or "-")
    remaining, remaining_color = competition_countdown(view)
    header = boxed(
        TITLE,
        [
            f"{paint('●', status_color, bold=True)} {paint(str(view['status']), status_color, bold=True)}    {paint(remaining, remaining_color, bold=True)}",
            f"Team  {view['team_name']}    Challenges  {solved}/{len(challenges)}    Score  {score_text} pt    Rank  {view['rank']}",
        ],
        width,
        CYAN,
    )
    resources = resources or {
        "now": time.monotonic(),
        "cpu": 0.0,
        "memory": 0.0,
        "cpu_cores": os.cpu_count() or 1,
        "used_gib": 0.0,
        "total_gib": 0.0,
        "samples": [],
    }
    transfers = render_transfers(view, width)
    comparison = render_team_comparison(view, width)

    def assemble(*, event_limit: int = 4, include_events: bool = True) -> list[str]:
        resource_lines = render_resources(resources, width, view.get("codex_usage"))
        event_lines = (
            render_events(view, width, limit=event_limit) if include_events else []
        )
        return (
            header
            + resource_lines
            + render_challenges(view, left_width, right_width)
            + transfers
            + event_lines
            + comparison
        )

    lines = assemble()
    if len(lines) > height:
        lines = assemble(event_limit=1)
    if len(lines) > height:
        lines = assemble(include_events=False)
    if len(lines) > height:
        # Reserve graphs and comparison first. A long problem list must not
        # silently remove CPU/RAM history or push all comparison rows offscreen.
        resource_lines = (
            render_resources if height >= 30 else render_resources_compact
        )(resources, width, view.get("codex_usage"))
        remaining = max(0, height - len(header) - len(resource_lines))
        column_minimum = min(
            len(render_challenges(view, left_width, right_width)),
            max(6, remaining // 2),
        )
        column_budget = min(
            column_minimum, max(0, remaining - min(10, len(comparison)))
        )
        transfer_budget = (
            min(len(transfers), 5)
            if remaining - column_budget >= min(10, len(comparison)) + 3
            else 0
        )
        comparison_budget = min(
            len(comparison), remaining - column_budget - transfer_budget
        )
        challenge_budget = remaining - transfer_budget - comparison_budget
        lines = (
            header
            + resource_lines
            + render_challenges(
                view, left_width, right_width, max_rows=challenge_budget
            )
            + fit_panel(transfers, transfer_budget, width, BLUE)
            + fit_panel(comparison, comparison_budget, width, BLUE)
        )
    if len(lines) > height:
        lines = lines[:height]
    screen_width = max(1, min(terminal.columns, 220))
    return "\n".join(
        SCREEN_STYLE
        + pad(truncate(terminal_text(line), screen_width), screen_width).replace(
            RESET, RESET + SCREEN_STYLE
        )
        + RESET
        for line in lines
    )


def solid_warning_box(message: str, terminal_columns: int) -> list[str]:
    box_width = min(len(message) + 4, max(4, terminal_columns))
    inner_width = max(0, box_width - 4)
    text = message[:inner_width]
    plain = [
        "┌" + "─" * max(0, box_width - 2) + "┐",
        "│ " + text.ljust(inner_width) + " │",
        "└" + "─" * max(0, box_width - 2) + "┘",
    ]
    return [f"{BOLD}{RED_BG}{WHITE}{line}{RESET}" for line in plain]


def terminal_size_warning_overlays(columns: int, rows: int) -> str:
    overlays: list[str] = []
    if columns < 1 or rows < 1:
        return ""
    if columns < 4 or rows < 3:
        message = "!" if columns < 9 else "TOO SMALL"
        return f"\033[1;1H{BOLD}{RED_BG}{WHITE}{message[:columns]}{RESET}"

    def place(box: list[str], row: int, column: int) -> None:
        for index, line in enumerate(box):
            overlays.append(f"\033[{row + index};{column}H{line}")

    if rows < MIN_TERMINAL_ROWS and rows >= 3 and columns >= 4:
        box = solid_warning_box("HEIGHT TOO SMALL", columns)
        box_width = display_width(box[0])
        place(box, rows - len(box) + 1, max(1, (columns - box_width) // 2 + 1))
    if columns < MIN_TERMINAL_COLUMNS and rows >= 3 and columns >= 4:
        box = solid_warning_box("WIDTH TOO SMALL", columns)
        box_width = display_width(box[0])
        place(box, max(1, (rows - len(box)) // 2 + 1), max(1, columns - box_width + 1))
    return "".join(overlays)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    instance_lock = InstanceLock(LOCK_FILE)
    if not instance_lock.acquire():
        print("rCTF monitor is already running.", file=sys.stderr)
        return 1
    try:
        state = SharedState()
        resources = ResourceSampler()
        stop = threading.Event()
        monitor = Monitor(state, stop)
        worker = threading.Thread(target=monitor.run, name="rctf-monitor")
        codex_usage_worker = threading.Thread(
            target=monitor.run_codex_usage, name="codex-usage"
        )
        previous_sigterm_handler = signal.signal(signal.SIGTERM, lambda *_: stop.set())
        worker.start()
        codex_usage_worker.start()
        deadline = (
            time.monotonic() + float(os.environ.get("RCTF_MONITOR_TEST_DURATION", "0"))
            if TEST_MODE and os.environ.get("RCTF_MONITOR_TEST_DURATION")
            else None
        )
        sys.stdout.write("\033[?25l")
        previous_frame: list[str] = []
        previous_terminal_size: tuple[int, int] | None = None
        resize_event = threading.Event()
        previous_sigwinch_handler = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, lambda _signum, _frame: resize_event.set())
        try:
            while not stop.is_set():
                resize_event.clear()
                terminal_size = shutil.get_terminal_size((120, 40))
                terminal_key = (terminal_size.columns, terminal_size.lines)
                current_frame = render_screen(
                    state.view(), resources.sample()
                ).splitlines()
                warning_overlays = terminal_size_warning_overlays(
                    terminal_size.columns, terminal_size.lines
                )
                if previous_terminal_size != terminal_key:
                    sys.stdout.write(
                        "\033[H\033[2J" + "\n".join(current_frame) + warning_overlays
                    )
                    sys.stdout.flush()
                else:
                    updates: list[str] = []
                    render_width = (
                        display_width(current_frame[0]) if current_frame else 0
                    )
                    width_changed = bool(
                        previous_frame
                        and current_frame
                        and display_width(previous_frame[0]) != render_width
                    )
                    for row in range(max(len(previous_frame), len(current_frame))):
                        old_line = (
                            previous_frame[row] if row < len(previous_frame) else None
                        )
                        new_line = (
                            current_frame[row] if row < len(current_frame) else None
                        )
                        if old_line == new_line:
                            continue
                        updates.append(f"\033[{row + 1};1H")
                        if width_changed or new_line is None:
                            updates.append("\033[2K")
                        if new_line is not None:
                            updates.append(new_line)
                            if render_width < terminal_size.columns:
                                updates.append("\033[K")
                    if (
                        len(current_frame) != len(previous_frame)
                        and len(current_frame) < terminal_size.lines
                    ):
                        updates.append(f"\033[{len(current_frame) + 1};1H\033[J")
                    if warning_overlays:
                        updates.append(warning_overlays)
                    if updates:
                        sys.stdout.write("".join(updates))
                        sys.stdout.flush()
                previous_frame = current_frame
                previous_terminal_size = terminal_key
                if deadline is not None and time.monotonic() >= deadline:
                    break
                resize_event.wait(UI_REFRESH_INTERVAL)
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)
            signal.signal(signal.SIGWINCH, previous_sigwinch_handler)
            stop.set()
            worker.join()
            codex_usage_worker.join()
            sys.stdout.write("\033[0m\033[2J\033[H\033[?25h")
            sys.stdout.flush()
    finally:
        instance_lock.release()
    return 0


def headless_main() -> int:
    lock = InstanceLock(LOCK_FILE)
    if not lock.acquire():
        raise RuntimeError("rCTF monitor is already running for this state directory")
    stop = threading.Event()
    old_handlers = {
        sig: signal.signal(sig, lambda *_: stop.set())
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    workers = []
    try:
        state = SharedState()
        monitor = Monitor(state, stop)
        for target in (monitor.run, monitor.run_codex_usage):
            worker = threading.Thread(target=target)
            worker.start()
            workers.append(worker)
        while not stop.wait(POLL_INTERVAL):
            view = state.view()
            print(
                json.dumps(
                    {
                        key: view[key]
                        for key in ("status", "team_name", "score", "rank")
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        stop.set()
        for worker in workers:
            worker.join()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        lock.release()
    return 0


def configure(path=None):
    global BASE_URL, TOKEN, TITLE, BASE_DIR, STATE_DIR, POLL_INTERVAL
    from .config import load_profile

    profile, config, TOKEN = load_profile(path)
    BASE_URL = config["url"]
    RCTFClient(BASE_URL, TOKEN)  # Validate before constructing local paths.
    TITLE = str(config.get("name") or "rCTF Monitor")
    BASE_DIR = (profile.parent / config.get("root", "../workspace")).resolve()
    identity = hashlib.sha256((BASE_URL + "\0" + TOKEN).encode()).hexdigest()[:16]
    STATE_DIR = BASE_DIR / ".rctf-monitor" / identity
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_DIR.chmod(0o700)
    POLL_INTERVAL = 10.0
    for name, relative in {
        "CURRENT_FILE": "current.json",
        "EVENT_FILE": "events.jsonl",
        "SNAPSHOT_DIR": "snapshots",
        "ARCHIVE_DIR": "archive",
        "MANIFEST_FILE": "manifest.json",
        "CODEX_USAGE_FILE": "codex-usage.json",
        "LOCK_FILE": "monitor.lock",
        "STAGING_DIR": "staging",
    }.items():
        globals()[name] = STATE_DIR / relative


def cli():
    parser = argparse.ArgumentParser(
        description="rCTF team monitor and attachment downloader"
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--setup", action="store_true")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Synchronize once, wait for downloads, then exit",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Read-only authentication/API check; no downloads",
    )
    args = parser.parse_args()
    if args.setup:
        from .installer import main as setup

        return setup(["--profile", str(args.config)] if args.config else [])
    configure(args.config)
    if args.check:
        client = RCTFClient(BASE_URL, TOKEN)
        team = client.request_json("/api/v1/users/me")
        config = client.request_json(
            "/api/v1/integrations/client/config", authenticated=False
        )
        Monitor.find_helpers()
        print(
            json.dumps(
                {
                    "ok": True,
                    "team": team["name"],
                    "competition": config.get("ctfName"),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.once:
        lock = InstanceLock(LOCK_FILE)
        if not lock.acquire():
            raise RuntimeError("Monitor already running")
        monitor = None
        stop = threading.Event()
        try:
            state = SharedState()
            monitor = Monitor(state, stop)
            initial_event_count = len(state.events)
            monitor.refresh_config()
            monitor.process_snapshot(monitor.fetch_snapshot())
            monitor.executor.shutdown(wait=True)
            view = state.view()
            errors = [
                event
                for event in state.events[initial_event_count:]
                if event["kind"] == "ERROR"
            ]
            unresolved = 0
            for challenge in monitor.previous.get("challenges", {}).values():
                for file in challenge.get("files", []):
                    entry = monitor.manifest.get(
                        monitor.manifest_key(challenge["id"], file["local_name"])
                    )
                    extraction = (
                        entry.get("extraction") if isinstance(entry, dict) else None
                    )
                    if not entry or (
                        isinstance(extraction, dict)
                        and extraction.get("status") != "complete"
                    ):
                        unresolved += 1
            print(
                json.dumps(
                    {
                        "status": view["status"],
                        "team": view["team_name"],
                        "challenges": len(view["challenges"]),
                        "score": view["score"],
                        "rank": view["rank"],
                        "errors": len(errors),
                        "unresolved_attachments": unresolved,
                    },
                    ensure_ascii=False,
                )
            )
            return 1 if errors or unresolved else 0
        finally:
            stop.set()
            if monitor:
                monitor.executor.shutdown(wait=True, cancel_futures=True)
            lock.release()
    return headless_main() if args.headless or not sys.stdout.isatty() else main()


def entrypoint():
    try:
        return cli()
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError:
        print(
            "설정 또는 필수 파일이 없습니다. python3 install.py 를 실행하세요.",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
