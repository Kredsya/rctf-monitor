"""Minimal private profile; no credentials in command-line arguments."""

import json
import os
import tempfile
import urllib.parse
from pathlib import Path
from .client import origin


def parse_target(value):
    parsed = urllib.parse.urlsplit(value.strip())
    origin(value.strip())
    query = urllib.parse.parse_qs(parsed.query)
    token = query.get("token", [""])[0]
    path = parsed.path.rstrip("/")
    if path.endswith("/login"):
        path = path[:-6]
    if parsed.query and set(query) != {"token"}:
        raise ValueError("Unsupported URL query")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")), token


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def default_profile():
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "rctf-monitor.py").exists():
        return checkout / ".rctf-monitor-local/profile.json"
    return Path.cwd() / ".rctf-monitor-local/profile.json"


def load_profile(path):
    path = Path(path or default_profile()).resolve()
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Invalid profile")
    token = os.environ.get("RCTF_MONITOR_TOKEN")
    if token is None:
        credentials = path.parent / data.get("credentials_file", "credentials.json")
        token = json.loads(credentials.read_text()).get("team_token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Missing team token; run install.py")
    return path, data, token.strip()
