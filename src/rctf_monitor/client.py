"""rCTF v1 transport. Team tokens are exchanged, never sent as Bearer tokens."""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime


class ApiError(RuntimeError):
    def __init__(self, code, status=0):
        self.code = code
        self.status = status
        super().__init__(f"rCTF: {code}" + (f" (HTTP {status})" if status else ""))


def origin(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("Invalid HTTP(S) URL")
    return p.scheme, p.hostname.lower(), p.port or (443 if p.scheme == "https" else 80)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(req.full_url)[0] == "https" and origin(newurl)[0] != "https":
            raise ApiError("insecure_redirect")
        # Authenticated API requests never follow redirects. File redirects carry no auth.
        if req.has_header("Authorization") or req.data is not None:
            raise ApiError("api_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class RCTFClient:
    def __init__(self, url, team_token):
        origin(url)
        p = urllib.parse.urlsplit(url)
        if p.query or p.fragment:
            raise ValueError("Base URL must not contain a token or query")
        self.url = url.rstrip("/")
        self.team_token = team_token
        self.auth_token = ""
        self.retry_until = 0.0
        self.opener = urllib.request.build_opener(SafeRedirect())

    @property
    def logged_in(self):
        return bool(self.auth_token)

    def request_json(self, path, data=None, *, authenticated=True):
        if not path.startswith("/api/v1/"):
            raise ValueError("Only rCTF API routes are accepted")
        if time.monotonic() < self.retry_until:
            raise ApiError("rate_limited", 429)
        headers = {"Accept": "application/json", "User-Agent": "rctf-monitor/0.1.0"}
        if authenticated:
            if not self.auth_token:
                self.login()
            headers["Authorization"] = "Bearer " + self.auth_token
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode()
        try:
            with self.opener.open(
                urllib.request.Request(self.url + path, data=body, headers=headers),
                timeout=15,
            ) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ApiError("response_too_large")
            result = json.loads(raw)
        except urllib.error.HTTPError as error:
            code = "request_failed"
            try:
                payload = json.loads(error.read(65536))
                candidate = payload.get("kind") if isinstance(payload, dict) else None
                if candidate in {
                    "badNotStarted",
                    "badEnded",
                    "badTokenVerification",
                    "badToken",
                    "badAuthentication",
                    "badRateLimit",
                    "badUnknownUser",
                }:
                    code = candidate
            except (ValueError, OSError):
                pass
            if error.code == 401 and code not in ("badNotStarted", "badEnded"):
                self.auth_token = ""
            if error.code == 429:
                delay = 30
                retry = error.headers.get("Retry-After", "")
                try:
                    delay = float(retry)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(retry).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        pass
                self.retry_until = time.monotonic() + max(1, min(3600, delay))
            error.close()
            raise ApiError(code, error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ApiError("connection_failed") from None
        except (ValueError, UnicodeError):
            raise ApiError("invalid_json") from None
        if not isinstance(result, dict) or not isinstance(result.get("kind"), str):
            raise ApiError("invalid_response")
        if not result["kind"].startswith("good"):
            code = result["kind"]
            if not re.fullmatch(r"[A-Za-z]{1,64}", code):
                code = "invalid_response"
            if code in ("badTokenVerification", "badToken", "badAuthentication"):
                self.auth_token = ""
            raise ApiError(code)
        return result.get("data")

    def login(self):
        data = self.request_json(
            "/api/v1/auth/login", {"teamToken": self.team_token}, authenticated=False
        )
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("authToken"), str)
            or not data["authToken"]
        ):
            raise ApiError("missing_auth_token")
        self.auth_token = data["authToken"]

    def open_file(self, url):
        # rCTF's supplied files are public/signed URLs. Never disclose team auth to storage.
        url = urllib.parse.urljoin(self.url + "/", url)
        origin(url)
        if origin(self.url)[0] == "https" and origin(url)[0] != "https":
            raise ApiError("insecure_attachment")
        try:
            return self.opener.open(
                urllib.request.Request(
                    url, headers={"User-Agent": "rctf-monitor/0.1.0"}
                ),
                timeout=15,
            )
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ApiError("attachment_download_failed") from None
