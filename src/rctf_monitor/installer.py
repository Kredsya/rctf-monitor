"""Interactive, standard-library-only configuration for checkout and wheel users."""

import argparse
import json
import getpass
from pathlib import Path
from .assets import find_helpers, REQUIRED_HELPERS, safe_directory_name
from .client import RCTFClient
from .config import default_profile, parse_target, private_json


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="rCTF monitor setup (Python 3.12+, Linux)"
    )
    parser.add_argument(
        "--url", help="Base URL only; paste login URLs at the hidden prompt"
    )
    parser.add_argument("--profile", type=Path, default=default_profile())
    args = parser.parse_args(argv)
    value = args.url or getpass.getpass("대회 URL 또는 토큰 로그인 URL (입력 숨김): ")
    url, token = parse_target(value)
    token = token or getpass.getpass("팀 토큰 (입력 숨김): ").strip()
    if not token:
        raise ValueError("팀 토큰이 비었습니다.")
    find_helpers(
        REQUIRED_HELPERS,
        args.profile.resolve().parent.parent / "workspace",
        Path(__file__).resolve().parent,
    )
    client = RCTFClient(url, token)
    me = client.request_json("/api/v1/users/me")
    config = client.request_json(
        "/api/v1/integrations/client/config", authenticated=False
    )
    if not isinstance(me, dict) or not me.get("id") or not isinstance(config, dict):
        raise ValueError("지원되지 않는 rCTF 응답입니다.")
    name = str(config.get("ctfName") or "rCTF Competition")
    root = "../workspace/" + safe_directory_name(name)
    if args.profile.exists():
        previous = json.loads(args.profile.read_text())
        if previous.get("url") == url and previous.get("root"):
            root = previous["root"]
    # Validate network and assets before touching an existing configuration.
    private_json(args.profile.parent / "credentials.json", {"team_token": token})
    private_json(
        args.profile,
        {
            "url": url,
            "name": name,
            "root": root,
            "credentials_file": "credentials.json",
        },
    )
    print("설정 완료. python3 rctf-monitor.py 로 실행하세요.")
    return 0
